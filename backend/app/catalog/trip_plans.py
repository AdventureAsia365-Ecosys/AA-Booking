"""Sellable trip plans: CIS catalog tours joined live to Booking's commercial row (AA-679, S237 decision D4, bk-009).

- **Content is CIS's** (ADR 0002, O7): every title, day, place and photo is read from the ``catalog`` views at request
  time (``catalog.v_tour``, ``v_tour_day``, ``v_tour_photo``; CIS migration 225 grants SELECT to role ``booking``).
  Nothing is copied into Booking, so a tour that leaves the catalog (inactive, trashed, superseded) drops out of
  every list on the next read. ``booking.trip_plans`` (bk-009) holds only the commercial fields.
- **Key** = ``(source_system, source_id)`` of contract catalog.tour.v1: ``source_id`` is stable across re-uploads;
  ``tour_id`` (the current CIS version) is returned for display and never stored.
- **Commercial edit** (``PUT …/commercial``) upserts the one row of a tour (unique key, so re-saving never creates a
  second row) and writes one audit row when something changed. A tour must be in ``catalog.v_tour`` to get a row.
- **Use for trip case** starts a proposal from a sellable plan: either a new case (``source_system = 'cis'``,
  ``source_id`` = the plan) whose V1 items are the plan's CIS days, or a new version of an existing case with the
  same items. Item titles are the CIS day titles (the UI spec's "+ Add from CIS tour"); items carry the plan's
  ``source_id`` as ``source_tour_id``. Prices are never pre-filled: a version is priced from its cost lines (ADR 0005).
- **Orphaned plans** (AA-986 L5, root ADR 0015): ``list_orphaned`` lists the plans whose tour left
  ``catalog.v_tour`` (not a paused one: no staff flag for a pause), with the reason from ``catalog.v_tour_retired``
  (CIS 274) and the open cases, future departures and confirmed holds still attached; nothing is deleted or
  cancelled.
- The price hint (``price_hint_*``) and the default channel are commercial data: only staff with ``pricing.read``
  see them (content creators read the catalog without them).
- AA-996 (Figma audit slice 2, bk-032): the commercial row names the plan's **supplier** (``supplier_id``, an active
  ``booking.suppliers`` row when it is set or changed; commercial data, ``pricing.read``) and the plan has a **price
  matrix** (``app.catalog.trip_plan_prices``). List rows carry the ``destinations`` (the CIS day places, first
  appearance first), a cover photo that falls back to the first tour photo, and the ``from_price`` derived from the
  matrix. The list opens **sellable first** (``sort=sellable``, then title), so the default page shows the plans
  Booking sells.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.catalog import liveness
from app.catalog import trip_plan_prices as prices
from app.catalog.destinations import CATALOG_ERRORS
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.trip_cases import service as trip_cases
from app.trip_cases import sql, versions

SOURCE_SYSTEM = "cis"
DEFAULT_SORT = "sellable"
SORTS = {
    "sellable": "coalesce(p.sellable, false)",
    "title": "t.title",
    "country": "t.country",
    "duration_days": "t.duration_days",
    "updated_at": "t.updated_at",
}
MAX_PAGE = 100
MAX_PHOTOS = 60
MAX_DAY = 366
COMMERCIAL_FIELDS = (
    "sellable",
    "trip_style",
    "default_channel",
    "price_hint_amount",
    "price_hint_currency",
    "price_hint_basis",
    "commercial_notes",
    "supplier_id",
)
# Commercial fields hidden from staff without pricing.read.
PRICING_FIELDS = ("default_channel", "price_hint", "from_price", "supplier")
MAX_DESTINATIONS = 8

PLAN_COLUMNS = """
    p.id::text AS plan_id, coalesce(p.sellable, false) AS sellable, p.trip_style, p.default_channel,
    p.price_hint_amount::text AS price_hint_amount, p.price_hint_currency::text AS price_hint_currency,
    p.price_hint_basis, p.supplier_id::text AS supplier_id
"""
LIST_COLUMNS = f"""
    t.source_system, t.source_id, t.tour_id::text AS tour_id, t.sku, t.title, t.country, t.duration_days,
    t.cover_photo_path, t.photo_count, t.lifecycle_stage, t.updated_at, {PLAN_COLUMNS}
"""
DETAIL_COLUMNS = f"""
    t.source_system, t.source_id, t.tour_id::text AS tour_id, t.source_version, t.sku, t.slug, t.title, t.subtitle,
    t.summary, t.description, t.highlights::text AS highlights, t.country, t.duration_raw, t.duration_days,
    t.inclusions, t.exclusions, t.best_time_to_go, t.group_size, t.lifecycle_stage, t.itinerary_text,
    t.cover_photo_path, t.photo_count, t.published_at, t.updated_at, t.content_hash, {PLAN_COLUMNS},
    p.commercial_notes, p.created_by, cb.name AS created_by_name, p.created_at AS commercial_created_at,
    p.updated_by, ub.name AS updated_by_name, p.updated_at AS commercial_updated_at
"""
JOIN = (
    "FROM catalog.v_tour t LEFT JOIN booking.trip_plans p"
    " ON p.source_system = t.source_system AND p.source_id = t.source_id"
)
# Per-row extras of a list PAGE (AA-996), joined LATERAL on equality keys after the LIMIT, so they run for the rows of
# the page only (an ``= ANY($1)`` filter is not pushed through the catalog views' joins: lesson S238, AA-712).
PAGE_EXTRAS = f"""
LEFT JOIN LATERAL (
    SELECT array_agg(x.place ORDER BY x.first_seen) AS places FROM (
        SELECT btrim(u.place) AS place, min(d.day_number * 1000 + u.ord) AS first_seen
        FROM catalog.v_tour_day d CROSS JOIN LATERAL unnest(d.places) WITH ORDINALITY AS u(place, ord)
        WHERE d.source_system = pg.source_system AND d.source_id = pg.source_id AND btrim(u.place) <> ''
        GROUP BY btrim(u.place)
    ) x
) dst ON true
LEFT JOIN LATERAL (
    SELECT ph.path FROM catalog.v_tour_photo ph
    WHERE pg.cover_photo_path IS NULL AND ph.source_system = pg.source_system AND ph.source_id = pg.source_id
    ORDER BY ph.position LIMIT 1
) ph ON true
LEFT JOIN LATERAL ({prices.FROM_PRICE_SQL.format(plan="pg.plan_id::uuid")}) fp ON true
"""  # noqa: S608 — constants only, no input
SUPPLIER_JOIN = "LEFT JOIN booking.suppliers sp ON sp.id = p.supplier_id"
SUPPLIER_COLUMNS = "sp.code AS supplier_code, sp.company_name AS supplier_name, sp.status AS supplier_status"


@asynccontextmanager
async def catalog_reads() -> AsyncIterator[None]:
    """A missing catalog view or grant (CIS 225 not applied, grant revoked) is a 503, not a 500. So is a catalog read
    that runs past ``statement_timeout`` or the pool's command timeout (see ``CATALOG_ERRORS``)."""
    try:
        yield
    except CATALOG_ERRORS as exc:
        raise Unavailable("catalog_unavailable", "the CIS catalog is not readable right now") from exc


def _price_hint(row: dict[str, Any]) -> dict[str, Any] | None:
    amount = row.pop("price_hint_amount", None)
    currency = row.pop("price_hint_currency", None)
    basis = row.pop("price_hint_basis", None)
    if amount is None:
        return None
    return {"amount": amount, "currency": currency, "basis": basis}


def _supplier(row: dict[str, Any]) -> dict[str, Any] | None:
    code, name, status = (
        row.pop("supplier_code", None),
        row.pop("supplier_name", None),
        row.pop("supplier_status", None),
    )
    sid = row.get("supplier_id")
    if sid is None:
        return None
    return {"id": sid, "code": code, "company_name": name, "status": status}


def _shape(row: Any, staff: Staff) -> dict[str, Any]:
    out = dict(row)
    out["price_hint"] = _price_hint(out)
    out["has_commercial_row"] = out["plan_id"] is not None
    out["supplier"] = _supplier(out)
    out["from_price"] = prices.from_price(
        {"amount": out.pop("from_amount", None), "currency": out.pop("from_currency", None),
         "season": out.pop("from_season", None)}
    )  # fmt: skip
    if not staff.can("pricing.read"):
        out["supplier_id"] = None
        for key in PRICING_FIELDS:
            out[key] = None
    return out


async def list_plans(
    conn: asyncpg.Connection,
    staff: Staff,
    *,
    q: str | None = None,
    country: str | None = None,
    sellable: bool | None = None,
    trip_style: str | None = None,
    sort: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Every catalog tour with its commercial state. ``q`` matches the title, the SKU or the exact ``source_id``;
    ``country`` and ``trip_style`` match case-insensitively; ``sellable=false`` includes tours without a row."""
    where: list[str] = []
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        where.append(fragment.replace("{p}", f"${len(args)}"))

    if q and q.strip():
        args.append(q.strip())
        exact = f"${len(args)}"
        add(f"(t.title ILIKE {{p}} OR t.sku ILIKE {{p}} OR t.source_id = {exact})", paging.contains(q))
    if country and country.strip():
        add("lower(t.country) = lower({p})", country.strip())
    if sellable is not None:
        add("coalesce(p.sellable, false) = {p}", sellable)
    if trip_style and trip_style.strip():
        add("lower(p.trip_style) = lower({p})", trip_style.strip())
    raw = (sort or DEFAULT_SORT).strip()
    key = raw.lstrip("-+")
    column = SORTS.get(key)
    if column is None:
        raise Invalid("unknown_sort", f"sort must be one of {', '.join(SORTS)} (prefix - for descending)")
    if key == DEFAULT_SORT:
        # Sellable first (``-sellable`` = not for sale first), then by title.
        order = f"{column} {'ASC' if raw.startswith('-') else 'DESC'}, t.title ASC NULLS LAST, t.source_id"
    else:
        order = f"{column} {'DESC' if raw.startswith('-') else 'ASC'} NULLS LAST, t.source_id"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = max(1, min(limit, MAX_PAGE))
    async with catalog_reads():
        total = await conn.fetchval(f"SELECT count(*) {JOIN} {clause}", *args)  # noqa: S608 — fixed fragments
        rows = await conn.fetch(
            f"""
            SELECT pg.*, ph.path AS first_photo_path, dst.places AS destinations,
                   fp.amount AS from_amount, fp.currency AS from_currency, fp.season AS from_season,
                   {SUPPLIER_COLUMNS}
            FROM (
                SELECT {LIST_COLUMNS}, row_number() OVER (ORDER BY {order}) AS rn {JOIN} {clause}
                ORDER BY {order} LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
            ) pg
            {PAGE_EXTRAS}
            LEFT JOIN booking.suppliers sp ON sp.id = pg.supplier_id::uuid
            ORDER BY pg.rn
            """,  # noqa: S608 — fixed fragments, values are bound
            *args,
            limit,
            offset,
        )
    return {"items": [_list_item(r, staff) for r in rows], "total": total, "limit": limit, "offset": offset}


def _list_item(row: Any, staff: Staff) -> dict[str, Any]:
    raw = dict(row)
    raw.pop("rn", None)
    places = raw.pop("destinations", None) or []
    first_photo = raw.pop("first_photo_path", None)
    raw["cover_photo_path"] = raw["cover_photo_path"] or first_photo
    out = _shape(raw, staff)
    out["destinations"] = list(places)[:MAX_DESTINATIONS]
    return out


# AA-986 L5 (root ADR 0015 decision 5): "orphaned plans" = Booking trip plans whose tour is no longer in
# catalog.v_tour (retired, or gone before CIS 274 could say why). No catalog content is left to show, so the row
# carries the key, the commercial fields, the reason from catalog.v_tour_retired (CIS 274, when readable) and what is
# still attached to it: open trip cases, future departures and confirmed B2B holds on them (all kept, Q4).
# A PAUSED key is not orphaned: ADR 0015 decision 5 (paused table, AA-Booking) says "confirmed work kept, no staff
# flag" — a pause is temporary, so it is left out of the list and the facet count (sales are still refused, 409).
ORPHAN_WHERE = (
    "p.source_system = 'cis' AND NOT EXISTS (SELECT 1 FROM catalog.v_tour t"
    " WHERE t.source_system = p.source_system AND t.source_id = p.source_id)"
)
NOT_PAUSED = (
    " AND NOT EXISTS (SELECT 1 FROM catalog.v_tour_retired pr"
    " WHERE pr.source_system = p.source_system AND pr.source_id = p.source_id AND pr.state = 'paused')"
)


def orphan_where(with_retired: bool) -> str:
    """The orphaned predicate; with CIS 274 readable, paused keys are left out (without it they cannot be told apart
    from a retirement and stay listed as ``not_in_catalog``)."""
    return ORPHAN_WHERE + (NOT_PAUSED if with_retired else "")


ORPHAN_COUNTS = """
    (SELECT count(*) FROM booking.trip_cases tc
     WHERE tc.deleted_at IS NULL AND tc.status NOT IN ('completed', 'cancelled')
       AND ((tc.source_system = 'cis' AND tc.source_id = p.source_id)
            OR EXISTS (SELECT 1 FROM booking.trip_case_legs l
                       WHERE l.trip_case_id = tc.id AND l.source_system = 'cis' AND l.source_id = p.source_id)))
        AS open_cases,
    (SELECT count(*) FROM booking.departures d
     WHERE d.trip_plan_id = p.id AND d.status <> 'cancelled' AND d.start_date >= (now() AT TIME ZONE 'UTC')::date)
        AS future_departures,
    (SELECT count(DISTINCT h.id) FROM booking.b2b_holds h
     JOIN booking.b2b_hold_lines l ON l.hold_id = h.id JOIN booking.departures d ON d.id = l.departure_id
     WHERE d.trip_plan_id = p.id AND h.status = 'confirmed' AND d.start_date >= (now() AT TIME ZONE 'UTC')::date)
        AS confirmed_holds
"""
ORPHAN_TOMBSTONE = """
LEFT JOIN LATERAL (
    SELECT r.state, r.reason, r.changed_at FROM catalog.v_tour_retired r
    WHERE r.source_system = p.source_system AND r.source_id = p.source_id
    ORDER BY r.changed_at DESC NULLS LAST LIMIT 1
) r ON true
"""
NO_TOMBSTONE = (
    "LEFT JOIN LATERAL (SELECT NULL::text AS state, NULL::text AS reason, NULL::timestamptz AS changed_at) r ON true"
)
TOMB_KEYS = ("tomb_state", "tomb_reason", "tomb_changed_at")


def _orphan_shape(row: Any, staff: Staff) -> dict[str, Any]:
    out = _shape({k: v for k, v in dict(row).items() if k not in TOMB_KEYS}, staff)
    state = liveness.key_state(
        {
            "source_id": row["source_id"],
            "in_catalog": False,
            "tomb_state": row["tomb_state"],
            "tomb_reason": row["tomb_reason"],
            "tomb_changed_at": row["tomb_changed_at"],
        }
    )
    out.update(
        {
            "tour_id": None,
            "sku": None,
            "title": None,
            "country": None,
            "duration_days": None,
            "cover_photo_path": None,
            "photo_count": 0,
            "lifecycle_stage": None,
            "orphaned": True,
            "catalog": state.out(),
        }
    )
    return out


async def list_orphaned(
    conn: asyncpg.Connection, staff: Staff, *, q: str | None = None, limit: int = 25, offset: int = 0
) -> dict[str, Any]:
    """Trip plans whose tour left the catalog, newest departure from it first. ``q`` = the exact ``source_id``."""
    args: list[Any] = []
    limit = max(1, min(limit, MAX_PAGE))
    async with catalog_reads():
        with_retired = await liveness.view_ready(conn, liveness.RETIRED_VIEW)
        join = ORPHAN_TOMBSTONE if with_retired else NO_TOMBSTONE
        where = orphan_where(with_retired)
        if q and q.strip():
            args.append(q.strip())
            where += f" AND p.source_id = ${len(args)}"
        total = await conn.fetchval(f"SELECT count(*) FROM booking.trip_plans p WHERE {where}", *args)  # noqa: S608
        rows = await conn.fetch(
            f"""
            SELECT p.source_system, p.source_id, p.updated_at, {PLAN_COLUMNS}, {ORPHAN_COUNTS},
                   r.state AS tomb_state, r.reason AS tomb_reason, r.changed_at AS tomb_changed_at
            FROM booking.trip_plans p {join}
            WHERE {where}
            ORDER BY coalesce(r.changed_at, p.updated_at) DESC, p.source_id
            LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
            """,  # noqa: S608 — fixed fragments, values are bound
            *args,
            limit,
            offset,
        )
    return {"items": [_orphan_shape(r, staff) for r in rows], "total": total, "limit": limit, "offset": offset}


async def facets(conn: asyncpg.Connection) -> dict[str, Any]:
    """Filter options and counts for the list page, computed on the server (not from the current page).

    ``total`` / ``sellable`` count every catalog tour, a tour without a country included, so the KPI matches the
    list ``total``; only the country options leave the country-less group out (there is nothing to filter on)."""
    async with catalog_reads():
        groups = await conn.fetch(
            f"SELECT t.country, count(*) AS tours, count(*) FILTER (WHERE p.sellable) AS sellable {JOIN}"  # noqa: S608
            " GROUP BY t.country ORDER BY t.country NULLS LAST"
        )
        styles = await conn.fetch(
            f"SELECT DISTINCT p.trip_style {JOIN} WHERE p.trip_style IS NOT NULL ORDER BY p.trip_style"  # noqa: S608
        )
        where = orphan_where(await liveness.view_ready(conn, liveness.RETIRED_VIEW))
        orphaned = await conn.fetchval(f"SELECT count(*) FROM booking.trip_plans p WHERE {where}")  # noqa: S608
    return {
        "total": sum(r["tours"] for r in groups),
        "sellable": sum(r["sellable"] for r in groups),
        "orphaned": orphaned or 0,
        "countries": [dict(r) for r in groups if r["country"] is not None],
        "trip_styles": [r["trip_style"] for r in styles],
    }


async def _tour(conn: asyncpg.Connection, source_id: str) -> dict[str, Any]:
    async with catalog_reads():
        row = await conn.fetchrow(
            f"SELECT {DETAIL_COLUMNS}, {SUPPLIER_COLUMNS},"  # noqa: S608 — fixed fragments
            " fp.amount AS from_amount, fp.currency AS from_currency, fp.season AS from_season"
            f" {JOIN} {SUPPLIER_JOIN}"
            " LEFT JOIN booking.staff_users cb ON cb.cis_user_id = p.created_by"
            " LEFT JOIN booking.staff_users ub ON ub.cis_user_id = p.updated_by"
            f" LEFT JOIN LATERAL ({prices.FROM_PRICE_SQL.format(plan='p.id')}) fp ON true"
            " WHERE t.source_system = $1 AND t.source_id = $2",
            SOURCE_SYSTEM,
            source_id,
        )
    if row is None:
        raise NotFound("tour_not_in_catalog", f"tour {source_id} is not in the CIS catalog")
    return dict(row)


async def _days(conn: asyncpg.Connection, source_id: str) -> list[dict[str, Any]]:
    async with catalog_reads():
        rows = await conn.fetch(
            "SELECT day_number, title, body, places FROM catalog.v_tour_day"
            " WHERE source_system = $1 AND source_id = $2 ORDER BY day_number",
            SOURCE_SYSTEM,
            source_id,
        )
    return [{**dict(r), "places": list(r["places"] or [])} for r in rows]


async def get_plan(conn: asyncpg.Connection, staff: Staff, source_id: str) -> dict[str, Any]:
    tour = await _tour(conn, source_id)
    days = await _days(conn, source_id)
    async with catalog_reads():
        photos = await conn.fetch(
            "SELECT photo_id::text AS photo_id, path, place_label, position FROM catalog.v_tour_photo"
            " WHERE source_system = $1 AND source_id = $2 ORDER BY position LIMIT $3",
            SOURCE_SYSTEM,
            source_id,
            MAX_PHOTOS,
        )
    cases = await conn.fetchval(
        "SELECT count(*) FROM booking.trip_cases WHERE source_system = $1 AND source_id = $2 AND deleted_at IS NULL",
        SOURCE_SYSTEM,
        source_id,
    )
    out = _shape(tour, staff)
    out["highlights"] = sql.loads(out.pop("highlights"))
    out["days"] = days
    out["photos"] = [dict(p) for p in photos]
    out["cases_started"] = cases
    # Who / when of the commercial row (None for a tour Booking has not touched yet).
    created_by, created_by_name = out.pop("created_by"), out.pop("created_by_name")
    updated_by, updated_by_name = out.pop("updated_by"), out.pop("updated_by_name")
    created_at, updated_at = out.pop("commercial_created_at"), out.pop("commercial_updated_at")
    out["commercial"] = (
        {
            "created_at": created_at,
            "updated_at": updated_at,
            "created_by": {"id": created_by, "name": created_by_name} if created_by else None,
            "updated_by": {"id": updated_by, "name": updated_by_name} if updated_by else None,
        }
        if out["has_commercial_row"]
        else None
    )
    if not staff.can("pricing.read"):
        out["commercial_notes"] = None
    return out


def _norm(field: str, value: Any) -> Any:
    """Comparable form of a commercial field: the amount as Decimal (2450.0 == 2450.00), blank text as None."""
    if isinstance(value, str):
        value = value.strip() or None
    if field == "price_hint_amount" and value is not None:
        return Decimal(str(value))
    if field == "supplier_id" and value is not None:
        return str(value)
    return value


async def _check_supplier(conn: asyncpg.Connection, supplier_id: str) -> None:
    """A supplier set or changed on a plan must exist and be active (an unchanged one may have been deactivated)."""
    status = await conn.fetchval("SELECT status FROM booking.suppliers WHERE id = $1::uuid", supplier_id)
    if status is None:
        raise Invalid("supplier_not_found", "the supplier does not exist")
    if status != "active":
        raise Invalid("supplier_inactive", "pick an active supplier")


async def save_commercial(
    conn: asyncpg.Connection, staff: Staff, source_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """PUT semantics: ``data`` holds every commercial field (``price_hint`` = {amount, currency, basis} or None),
    except ``supplier_id``: left out, the plan keeps its supplier; ``None`` clears it.

    Upsert on the catalog key; one audit row when something changed (``trip_plan.created`` / ``.updated``)."""
    tour = await _tour(conn, source_id)
    hint = data.get("price_hint")
    new = {
        "sellable": bool(data.get("sellable")),
        "trip_style": _norm("trip_style", data.get("trip_style")),
        "default_channel": data.get("default_channel"),
        "price_hint_amount": _norm("price_hint_amount", hint["amount"]) if hint else None,
        "price_hint_currency": hint["currency"] if hint else None,
        "price_hint_basis": hint["basis"] if hint else None,
        "commercial_notes": _norm("commercial_notes", data.get("commercial_notes")),
        "supplier_id": _norm("supplier_id", data.get("supplier_id")),
    }
    keep_supplier = "supplier_id" not in data
    before_row = await conn.fetchrow(
        f"SELECT id::text AS id, {', '.join(COMMERCIAL_FIELDS)} FROM booking.trip_plans"  # noqa: S608 — constants
        " WHERE source_system = $1 AND source_id = $2 FOR UPDATE",
        SOURCE_SYSTEM,
        source_id,
    )
    before = {k: _norm(k, before_row[k]) for k in COMMERCIAL_FIELDS} if before_row else None
    if keep_supplier:
        # AA-996: a client that does not send supplier_id (older UI, the sellable toggle) keeps the plan's supplier.
        new["supplier_id"] = before["supplier_id"] if before else None
    changed = sorted(k for k in COMMERCIAL_FIELDS if before is None or before[k] != new[k])
    if before is not None and not changed:
        return {**await get_plan(conn, staff, source_id), "changed": []}
    if new["supplier_id"] is not None and (before is None or before["supplier_id"] != new["supplier_id"]):
        await _check_supplier(conn, new["supplier_id"])
    try:
        saved = await conn.fetchrow(
            """
            INSERT INTO booking.trip_plans AS p
                (source_system, source_id, sellable, trip_style, default_channel, price_hint_amount,
                 price_hint_currency, price_hint_basis, commercial_notes, supplier_id, created_by, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::uuid, $11, $11)
            ON CONFLICT (source_system, source_id) DO UPDATE
                SET sellable = EXCLUDED.sellable, trip_style = EXCLUDED.trip_style,
                    default_channel = EXCLUDED.default_channel, price_hint_amount = EXCLUDED.price_hint_amount,
                    price_hint_currency = EXCLUDED.price_hint_currency, price_hint_basis = EXCLUDED.price_hint_basis,
                    commercial_notes = EXCLUDED.commercial_notes, supplier_id = EXCLUDED.supplier_id,
                    updated_by = EXCLUDED.updated_by
            RETURNING id::text AS id, (xmax = 0) AS inserted
            """,
            SOURCE_SYSTEM,
            source_id,
            *new.values(),
            staff.cis_user_id,
        )
    except asyncpg.CheckViolationError as exc:
        raise Invalid("invalid_value", exc.constraint_name or "check violation") from exc
    # Two first saves at once both see no row (FOR UPDATE locks nothing yet); the loser updates the winner's row,
    # so "created" comes from the upsert itself, not from the earlier read.
    plan_id, created = saved["id"], bool(saved["inserted"])
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="trip_plan.created" if created else "trip_plan.updated",
        entity_type="trip_plan",
        entity_id=plan_id,
        before=None if created or before is None else {k: before[k] for k in changed},
        after={"source_id": source_id, **{k: new[k] for k in changed}},
        entity_label=tour["sku"] or (tour["title"] or source_id)[:80],
        summary=_summary(created, new, changed),
    )
    return {**await get_plan(conn, staff, source_id), "changed": changed}


def _summary(created: bool, new: dict[str, Any], changed: list[str]) -> str:
    if created:
        return "Marked sellable" if new["sellable"] else "Commercial details saved (not for sale)"
    rest = [k for k in changed if k != "sellable"]
    if "sellable" in changed:
        head = "Marked sellable" if new["sellable"] else "Marked not for sale"
        return head + (f"; updated {', '.join(rest)}" if rest else "")
    return f"Updated {', '.join(rest)}"


def items_from_days(days: list[dict[str, Any]], tour: dict[str, Any]) -> list[versions.Item]:
    """One item per CIS day (title = the day title, else ``Day N``), in day order; a tour without canonical days
    (legacy itinerary text) gives one item over its duration titled with the tour title."""
    source_id = tour["source_id"]
    items = [
        versions.Item(
            title=((d.get("title") or "").strip() or f"Day {d['day_number']}")[:500],
            day_from=d["day_number"],
            day_to=d["day_number"],
            source_tour_id=source_id,
        )
        for d in sorted(days, key=lambda d: d["day_number"])
        if 1 <= d["day_number"] <= MAX_DAY
    ][: versions.MAX_ITEMS]
    if items:
        return items
    duration = min(max(tour.get("duration_days") or 1, 1), MAX_DAY)
    title = (tour["title"] or source_id)[:500]
    return [versions.Item(title=title, day_from=1, day_to=duration, source_tour_id=source_id)]


async def _country_code(conn: asyncpg.Connection, country: str | None) -> list[str]:
    """The ISO-2 of the tour's CIS country name when the bk-005 list knows it (the catalog has names, not codes)."""
    if not country:
        return []
    code = await conn.fetchval("SELECT iso2::text FROM booking.countries WHERE lower(name) = lower($1)", country)
    return [code] if code else []


async def use_for_trip_case(
    conn: asyncpg.Connection, staff: Staff, source_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Start a proposal from a sellable plan. ``data["trip_case"]`` (a case ref) adds a version to that case;
    otherwise a new case is created from ``data`` (the ``POST /booking/trip-cases`` fields) with V1 from the plan."""
    tour = await _tour(conn, source_id)
    if not tour["sellable"]:
        raise Conflict("trip_plan_not_sellable", "mark the trip plan sellable before using it for a trip case")
    seed = versions.Seed(
        title=(tour["title"] or source_id)[:200],
        channel=tour["default_channel"],
        items=items_from_days(await _days(conn, source_id), tour),
        source_id=source_id,
        label=tour["sku"] or (tour["title"] or source_id)[:80],
    )
    if data.get("trip_case"):
        ref = data["trip_case"]
        version = await versions.create_version(conn, staff, ref, data.get("notes"), seed=seed)
        return {"mode": "new_version", "trip_case": await trip_cases.get_case(conn, ref), "version": version}
    case_data = {**data}
    case_data["trip_style"] = case_data.get("trip_style") or tour["trip_style"]
    if not case_data.get("countries"):
        case_data["countries"] = await _country_code(conn, tour["country"])
    case = await trip_cases.create_case(conn, staff, case_data, source=(SOURCE_SYSTEM, source_id))
    version = await versions.create_version(conn, staff, case["id"], None, seed=seed)
    return {"mode": "new_case", "trip_case": await trip_cases.get_case(conn, case["id"]), "version": version}
