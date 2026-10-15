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
- The price hint (``price_hint_*``) and the default channel are commercial data: only staff with ``pricing.read``
  see them (content creators read the catalog without them).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import asyncpg

from app import audit, paging
from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound, Unavailable
from app.trip_cases import service as trip_cases
from app.trip_cases import sql, versions

SOURCE_SYSTEM = "cis"
SORTS = {
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
)
# Commercial fields hidden from staff without pricing.read.
PRICING_FIELDS = ("default_channel", "price_hint")

PLAN_COLUMNS = """
    p.id::text AS plan_id, coalesce(p.sellable, false) AS sellable, p.trip_style, p.default_channel,
    p.price_hint_amount::text AS price_hint_amount, p.price_hint_currency::text AS price_hint_currency,
    p.price_hint_basis
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


@asynccontextmanager
async def catalog_reads() -> AsyncIterator[None]:
    """A missing catalog view or grant (CIS 225 not applied, grant revoked) is a 503, not a 500."""
    try:
        yield
    except (asyncpg.UndefinedTableError, asyncpg.InvalidSchemaNameError, asyncpg.InsufficientPrivilegeError) as exc:
        raise Unavailable("catalog_unavailable", "the CIS catalog is not readable right now") from exc


def _price_hint(row: dict[str, Any]) -> dict[str, Any] | None:
    amount = row.pop("price_hint_amount", None)
    currency = row.pop("price_hint_currency", None)
    basis = row.pop("price_hint_basis", None)
    if amount is None:
        return None
    return {"amount": amount, "currency": currency, "basis": basis}


def _shape(row: Any, staff: Staff) -> dict[str, Any]:
    out = dict(row)
    out["price_hint"] = _price_hint(out)
    out["has_commercial_row"] = out["plan_id"] is not None
    if not staff.can("pricing.read"):
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
    raw = (sort or "title").strip()
    column = SORTS.get(raw.lstrip("-+"))
    if column is None:
        raise Invalid("unknown_sort", f"sort must be one of {', '.join(SORTS)} (prefix - for descending)")
    order = f"{column} {'DESC' if raw.startswith('-') else 'ASC'} NULLS LAST, t.source_id"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = max(1, min(limit, MAX_PAGE))
    async with catalog_reads():
        total = await conn.fetchval(f"SELECT count(*) {JOIN} {clause}", *args)  # noqa: S608 — fixed fragments
        rows = await conn.fetch(
            f"SELECT {LIST_COLUMNS} {JOIN} {clause}"  # noqa: S608 — fixed fragments, values are bound
            f" ORDER BY {order} LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
            *args,
            limit,
            offset,
        )
    return {"items": [_shape(r, staff) for r in rows], "total": total, "limit": limit, "offset": offset}


async def facets(conn: asyncpg.Connection) -> dict[str, Any]:
    """Filter options and counts for the list page, computed on the server (not from the current page)."""
    async with catalog_reads():
        countries = await conn.fetch(
            f"SELECT t.country, count(*) AS tours, count(*) FILTER (WHERE p.sellable) AS sellable {JOIN}"  # noqa: S608
            " WHERE t.country IS NOT NULL GROUP BY t.country ORDER BY t.country"
        )
        styles = await conn.fetch(
            f"SELECT DISTINCT p.trip_style {JOIN} WHERE p.trip_style IS NOT NULL ORDER BY p.trip_style"  # noqa: S608
        )
    return {
        "total": sum(r["tours"] for r in countries),
        "sellable": sum(r["sellable"] for r in countries),
        "countries": [dict(r) for r in countries],
        "trip_styles": [r["trip_style"] for r in styles],
    }


async def _tour(conn: asyncpg.Connection, source_id: str) -> dict[str, Any]:
    async with catalog_reads():
        row = await conn.fetchrow(
            f"SELECT {DETAIL_COLUMNS} {JOIN}"  # noqa: S608 — fixed fragments
            " LEFT JOIN booking.staff_users cb ON cb.cis_user_id = p.created_by"
            " LEFT JOIN booking.staff_users ub ON ub.cis_user_id = p.updated_by"
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
    return value


async def save_commercial(
    conn: asyncpg.Connection, staff: Staff, source_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """PUT semantics: ``data`` holds every commercial field (``price_hint`` = {amount, currency, basis} or None).

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
    }
    before_row = await conn.fetchrow(
        f"SELECT id::text AS id, {', '.join(COMMERCIAL_FIELDS)} FROM booking.trip_plans"  # noqa: S608 — constants
        " WHERE source_system = $1 AND source_id = $2 FOR UPDATE",
        SOURCE_SYSTEM,
        source_id,
    )
    before = {k: _norm(k, before_row[k]) for k in COMMERCIAL_FIELDS} if before_row else None
    changed = sorted(k for k in COMMERCIAL_FIELDS if before is None or before[k] != new[k])
    if before is not None and not changed:
        return {**await get_plan(conn, staff, source_id), "changed": []}
    try:
        saved = await conn.fetchrow(
            """
            INSERT INTO booking.trip_plans AS p
                (source_system, source_id, sellable, trip_style, default_channel, price_hint_amount,
                 price_hint_currency, price_hint_basis, commercial_notes, created_by, updated_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10)
            ON CONFLICT (source_system, source_id) DO UPDATE
                SET sellable = EXCLUDED.sellable, trip_style = EXCLUDED.trip_style,
                    default_channel = EXCLUDED.default_channel, price_hint_amount = EXCLUDED.price_hint_amount,
                    price_hint_currency = EXCLUDED.price_hint_currency, price_hint_basis = EXCLUDED.price_hint_basis,
                    commercial_notes = EXCLUDED.commercial_notes, updated_by = EXCLUDED.updated_by
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
