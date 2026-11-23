"""Add-on offers: optional activities AA sells per place and day (AA-1151, bk-068; S285 advisories PR-29 B1 / B3 / B4,
T7; slice L8 of the root ``docs/audits/2026-11-19-S285-session/advisory-r2-plan.md``).

An offer wraps one Booking service of an AA supplier (``booking.services``, always ``supplier_id``: never an agency's
own supplier) with:

- **places** (``booking.addon_offer_places``): the CIS places (``catalog.v_destination`` ids, checked like a
  supplier's place) where it is sold, ``operates`` or ``pickup``;
- **sale rules** (T7): ``min_pax``, ``cutoff_hours`` (before the earliest departure time of the day, or the day start
  when it has none), ISO ``operating_weekdays`` (1 = Monday) and local ``start_times``;
- **overrides** of the contract terms (``app.supply.contract_terms.effective``): ``child_pct`` (B4, wins over the
  contract's child % when the supplier gives no child rate) and ``cancellation_tiers``.

Inventory is not on the offer: on request by default, free sale up to N, allotment and stop-sale are the rows of the
shared ``booking.supply_restrictions`` of the service (bk-060, ``app.supply.restrictions``), entered on the contract.

**Who does what (B1).** AA content staff (``content_creator``) and contracting create and edit a draft offer
(``addon.write``); the customer text and photos are written in CIS (``/admin/supply-content``, drafted from the
service facts the CIS supply feed pulls); contracting enters the contract and the net price (``supply.write``,
published by a second person, bk-008 / bk-064). A second person publishes the offer (``addon.approve``: admin,
contracting), never one of its writers (DB CHECK + 403 ``maker_checker``), except the business owner, flagged
``owner_self_approved`` (:mod:`app.auth.self_approval`). Publishing needs an active service and supplier, at least one
place and an adult rate in force today or later under a published contract (409 ``no_price``). A published offer is
fixed: reopen it as a draft to change it (the person who reopens becomes its only writer), or retire it.

**What travellers see (B3).** ``booking.v_place_addons`` (contract ``supply.place_addons.v1``) lists an offer at a
place only on the days it runs and has an in-force adult rate under a published contract, without a stop-sale;
:func:`place_days` reads it for the staff preview. A B2B quote of an ``sv:`` line whose service has a published offer
checks the sale rules (:func:`app.supply.addon_rules.line_problem`): below the minimum, not a running day or past the
cut-off is 422.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg

from app import audit, paging
from app.auth import self_approval
from app.auth.staff import Staff
from app.b2b import cancellation
from app.catalog import destinations as catalog
from app.catalog.liveness import view_ready
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.pricing import policy_values
from app.supply import content

STATUSES = ("draft", "published", "retired")
ROLES = ("operates", "pickup")
RULE_FIELDS = (
    "min_pax",
    "cutoff_hours",
    "operating_weekdays",
    "start_times",
    "child_pct",
    "cancellation_tiers",
    "sort_order",
    "featured",
)
DISPLAY_FIELDS = ("sort_order", "featured")  # the only fields a published offer may change
# technical-constant: the bk-068 CHECK bounds (addon_offers_rules_chk / _child_chk), not business values.
MIN_PAX_MAX = 999  # technical-constant: the bk-068 CHECK bound of min_pax
CUTOFF_HOURS_MAX = 720  # technical-constant: the bk-068 CHECK bound of cutoff_hours (30 days)
MAX_START_TIMES = 24  # technical-constant: the bk-068 CHECK bound of start_times
SORT_BOUND = 999
MAX_PLACES = 50  # technical-constant: one request's place list (the UI picks a handful)
PCT_PLACES = Decimal("0.01")  # technical-constant: numeric(5,2) precision
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
WEEKDAYS = (1, 2, 3, 4, 5, 6, 7)
ENTITY = "addon_offer"
# Pricing > Policy (bk-068): how many days ahead booking.v_place_addons lists; the view reads the value in force itself,
# the staff preview reports it.
HORIZON_KEY = "addon_days_ahead"

OFFER_SQL = """
SELECT o.id::text AS id, o.service_id::text AS service_id, o.status, o.min_pax, o.cutoff_hours,
       o.operating_weekdays, o.start_times, o.child_pct, o.cancellation_tiers::text AS cancellation_tiers,
       o.sort_order, o.featured, o.created_by, o.updated_by, o.writers, o.published_by, o.published_at,
       o.owner_self_approved, o.retired_by, o.retired_at, o.created_at, o.updated_at,
       s.code AS service_code, s.title AS service_title, s.service_type, s.unit, s.status AS service_status,
       s.duration_minutes, s.destination_id::text AS service_destination_id, s.country_code::text AS country_code,
       sp.id::text AS supplier_id, sp.code AS supplier_code, sp.company_name AS supplier_name,
       sp.status AS supplier_status,
       EXISTS (SELECT 1 FROM booking.supplier_rates x
                 JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
                WHERE x.service_id = s.id AND x.pax_type = 'adult' AND k.status = 'published'
                  AND k.supplier_id = s.supplier_id
                  AND (k.valid_to IS NULL OR k.valid_to >= (now() AT TIME ZONE 'UTC')::date)
                  AND (x.valid_to IS NULL OR x.valid_to >= (now() AT TIME ZONE 'UTC')::date)) AS has_price,
       (SELECT count(*) FROM booking.addon_offer_places p WHERE p.offer_id = o.id) AS place_count
FROM booking.addon_offers o
JOIN booking.services s ON s.id = o.service_id
JOIN booking.suppliers sp ON sp.id = s.supplier_id
"""
PLACES_SQL = """
SELECT offer_id::text AS offer_id, destination_id::text AS destination_id, role, created_by, created_at
FROM booking.addon_offer_places WHERE offer_id = ANY($1::uuid[]) ORDER BY offer_id, role, created_at, destination_id
"""
PUBLISH_SQL = (
    "UPDATE booking.addon_offers SET status = 'published', published_by = $2, published_at = now(),"
    " owner_self_approved = $3, retired_by = NULL, retired_at = NULL WHERE id = $1::uuid"
)
REOPEN_SQL = (
    "UPDATE booking.addon_offers SET status = 'draft', published_by = NULL, published_at = NULL,"
    " owner_self_approved = false, retired_by = NULL, retired_at = NULL, updated_by = $2 WHERE id = $1::uuid"
)
RETIRE_SQL = (
    "UPDATE booking.addon_offers SET status = 'retired', retired_by = $2, retired_at = now(), updated_by = $2"
    " WHERE id = $1::uuid"
)


# ── Rules (no database) ──────────────────────────────────────────────────────


def _int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Invalid(f"invalid_{name}", f"{name} is a whole number from {low} to {high}")
    return value


def parse_weekdays(value: Any) -> list[int]:
    """ISO weekdays (1 = Monday .. 7 = Sunday): a non-empty list, or a text such as ``"Mon, Wed"`` / ``"1,3"``;
    ``"daily"`` is every day. Sorted, unique."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("daily", "every day", "all"):
            return list(WEEKDAYS)
        names = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
        out: list[Any] = []
        for token in re.split(r"[,;/\s]+", text):
            if not token:
                continue
            out.append(int(token) if token.isdigit() else names.get(token[:3], 0))
        value = out
    if not isinstance(value, list | tuple) or not value:
        raise Invalid("invalid_operating_weekdays", "operating_weekdays lists at least one weekday (1 = Monday)")
    days = set()
    for day in value:
        if isinstance(day, bool) or not isinstance(day, int) or day not in WEEKDAYS:
            raise Invalid("invalid_operating_weekdays", "a weekday is 1 (Monday) to 7 (Sunday), or Mon .. Sun")
        days.add(day)
    return sorted(days)


def parse_start_times(value: Any) -> list[time]:
    """Local departure times ``HH:MM`` (a list, or a text such as ``"08:00, 13:30"``); empty = flexible."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [t for t in re.split(r"[,;\s]+", value.strip()) if t]
    if not isinstance(value, list | tuple) or len(value) > MAX_START_TIMES:
        raise Invalid("invalid_start_times", f"start_times lists at most {MAX_START_TIMES} times HH:MM")
    out: set[time] = set()
    for item in value:
        if isinstance(item, time):
            out.add(item.replace(second=0, microsecond=0))
            continue
        match = TIME_RE.match(str(item).strip())
        if not match:
            raise Invalid("invalid_start_times", "a start time is HH:MM (24-hour clock)")
        out.add(time(int(match[1]), int(match[2])))
    return sorted(out)


def _pct(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, bool) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or not 0 <= number <= 100 or number != number.quantize(PCT_PLACES):
        raise Invalid("invalid_child_pct", "child_pct is a number from 0 to 100 with at most 2 decimals")
    return number.quantize(PCT_PLACES)


def normalize_rules(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the rule fields present in ``data`` (422 naming the field); unknown fields are 422 ``not_editable``."""
    unknown = sorted(set(data) - set(RULE_FIELDS))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "min_pax":
            out[key] = _int(value, key, 1, MIN_PAX_MAX)
        elif key == "cutoff_hours":
            out[key] = _int(value, key, 0, CUTOFF_HOURS_MAX)
        elif key == "operating_weekdays":
            out[key] = parse_weekdays(value)
        elif key == "start_times":
            out[key] = parse_start_times(value)
        elif key == "child_pct":
            out[key] = _pct(value)
        elif key == "cancellation_tiers":
            if value is None:
                out[key] = None
            else:
                try:
                    tiers = cancellation.parse_tiers(value)
                except cancellation.TierError as exc:
                    raise Invalid("invalid_cancellation_tiers", str(exc)) from None
                out[key] = json.dumps(cancellation.tiers_out(tiers))
        elif key == "sort_order":
            out[key] = _int(value, key, -SORT_BOUND, SORT_BOUND)
        elif key == "featured":
            if not isinstance(value, bool):
                raise Invalid("invalid_featured", "featured is true or false")
            out[key] = value
    return out


def normalize_places(places: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    out: dict[str, str] = {}
    for place in places:
        destination = str(place.get("destination_id") or "").strip()
        role = str(place.get("role") or "operates")
        if not destination:
            raise Invalid("destination_required", "each place needs a destination_id")
        if role not in ROLES:
            raise Invalid("invalid_role", f"a place role is one of {', '.join(ROLES)}")
        out[destination] = role
    if len(out) > MAX_PLACES:
        raise Invalid("too_many_places", f"an offer lists at most {MAX_PLACES} places")
    return [{"destination_id": d, "role": r} for d, r in out.items()]


# ── Reads ────────────────────────────────────────────────────────────────────


def _row(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["operating_weekdays"] = [int(d) for d in out["operating_weekdays"]]
    out["start_times"] = [t.strftime("%H:%M") for t in out["start_times"] or ()]
    out["child_pct"] = None if out["child_pct"] is None else format(out["child_pct"], "f")
    tiers = out.pop("cancellation_tiers")
    out["cancellation_tiers"] = json.loads(tiers) if tiers else None
    out["writers"] = list(out["writers"] or [])
    out["component_id"] = f"sv:{out['service_id']}"
    return out


def missing(offer: Mapping[str, Any]) -> list[str]:
    """What still stops the offer from selling (the list badges): no place, no in-force price, an inactive service
    or supplier, no published customer text (``content`` is None while the CIS read-back is off: not counted)."""
    out = []
    if not offer.get("place_count"):
        out.append("no_place")
    if not offer.get("has_price"):
        out.append("no_price")
    if offer.get("service_status") != "active":
        out.append("service_inactive")
    if offer.get("supplier_status") != "active":
        out.append("supplier_inactive")
    if offer.get("content_published") is False:
        out.append("no_content")
    return out


async def _attach(conn: asyncpg.Connection, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not offers:
        return offers
    places: dict[str, list[dict[str, Any]]] = {o["id"]: [] for o in offers}
    rows = await conn.fetch(PLACES_SQL, list(places))
    names = await catalog.destinations_by_id(conn, [r["destination_id"] for r in rows])
    for r in rows:
        place = names.get(r["destination_id"])
        places[r["offer_id"]].append(
            {
                "destination_id": r["destination_id"],
                "role": r["role"],
                "name": place["name"] if place else None,
                "country": place["country"] if place else None,
            }
        )
    texts = await content.published_texts(conn, {("service", o["service_id"]) for o in offers})
    readback = await view_ready(conn, content.CONTENT_VIEW)
    for offer in offers:
        offer["places"] = places[offer["id"]]
        offer["content_published"] = (("service", offer["service_id"]) in texts) if readback else None
        offer["missing"] = missing(offer)
    return offers


async def list_offers(
    conn: asyncpg.Connection,
    *,
    status: str | None = None,
    destination_id: str | None = None,
    supplier_id: str | None = None,
    q: str | None = None,
    approvable_by: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Offers, newest change first. ``approvable_by``: drafts with a place that this staff key wrote none of."""
    where = ["true"]
    args: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        args.append(value)
        where.append(fragment.format(n=len(args)))

    if status:
        if status not in STATUSES:
            raise Invalid("invalid_status", f"status is one of {', '.join(STATUSES)}")
        add("o.status = ${n}", status)
    if destination_id:
        add(
            "EXISTS (SELECT 1 FROM booking.addon_offer_places p WHERE p.offer_id = o.id"
            " AND p.destination_id = ${n}::uuid)",
            _uuid(destination_id, "destination_id"),
        )
    if supplier_id:
        add("sp.id = ${n}::uuid", _uuid(supplier_id, "supplier_id"))
    if q and q.strip():
        add("(s.title ILIKE ${n} OR s.code ILIKE ${n} OR sp.company_name ILIKE ${n})", paging.contains(q))
    if approvable_by:
        add("o.status = 'draft' AND NOT (${n} = ANY (o.writers))", approvable_by)
    sql = f"{OFFER_SQL} WHERE {' AND '.join(where)}"  # noqa: S608 — fragments are constants
    total = await conn.fetchval(f"SELECT count(*) FROM ({sql}) t", *args)  # noqa: S608
    rows = await conn.fetch(
        f"{sql} ORDER BY o.updated_at DESC, o.id LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",  # noqa: S608
        *args,
        limit,
        offset,
    )
    items = await _attach(conn, [_row(r) for r in rows])
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _uuid(value: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise Invalid(f"invalid_{name}", f"{name} is not a UUID") from None


async def _get(conn: asyncpg.Connection, offer_id: str, *, lock: bool = False) -> dict[str, Any]:
    row = await conn.fetchrow(
        f"{OFFER_SQL} WHERE o.id = $1::uuid" + (" FOR UPDATE OF o" if lock else ""),  # noqa: S608
        _uuid(offer_id, "offer_id"),
    )
    if row is None:
        raise NotFound("offer_not_found", "no add-on offer with this id")
    return _row(row)


async def publication(conn: asyncpg.Connection, staff: Staff | None, offer: Mapping[str, Any]) -> dict[str, Any]:
    """What the caller may do about publishing ``offer`` (the page follows it; the write re-checks)."""
    blockers: list[str] = []
    self_needed = False
    if offer.get("status") != "draft":
        blockers.append("not_draft")
    blockers += [m for m in missing(offer) if m != "no_content"]
    if staff is None or not staff.can("addon.approve"):
        blockers.append("needs_addon_approve")
    elif staff.cis_user_id in offer.get("writers", []):
        self_needed = True
        if not await self_approval.may_self_approve(conn, staff):
            blockers.append("maker_checker")
    return {"allowed": not blockers, "self_approval": self_needed and not blockers, "blockers": blockers}


async def get_offer(conn: asyncpg.Connection, offer_id: str, staff: Staff | None = None) -> dict[str, Any]:
    (offer,) = await _attach(conn, [await _get(conn, offer_id)])
    offer["actions"] = {"publish": await publication(conn, staff, offer)}
    offer["audit"] = [
        dict(r)
        for r in await conn.fetch(
            "SELECT action, actor_ref, summary, created_at FROM booking.audit_logs"
            " WHERE entity_type = $1 AND entity_id = $2::uuid ORDER BY created_at DESC LIMIT 20",
            ENTITY,
            offer["id"],
        )
    ]
    return offer


ACTIVITY_SERVICES_SQL = """
SELECT s.id::text AS id, s.code, s.title, s.service_type, s.unit, s.status, s.destination_id::text AS destination_id,
       sp.code AS supplier_code, sp.company_name AS supplier_name,
       (SELECT o.id::text FROM booking.addon_offers o WHERE o.service_id = s.id) AS offer_id
FROM booking.services s JOIN booking.suppliers sp ON sp.id = s.supplier_id
WHERE ($1::text IS NULL OR s.title ILIKE $1 OR s.code ILIKE $1 OR sp.company_name ILIKE $1)
ORDER BY (SELECT o.id FROM booking.addon_offers o WHERE o.service_id = s.id) IS NOT NULL, lower(s.title), s.id
LIMIT $2
"""


async def service_options(conn: asyncpg.Connection, q: str | None, limit: int) -> dict[str, Any]:
    """Services an offer can wrap (no net, no contract): the picker of the add-on page; ``offer_id`` when one exists."""
    pattern = paging.contains(q) if q and q.strip() else None
    return {"items": [dict(r) for r in await conn.fetch(ACTIVITY_SERVICES_SQL, pattern, limit)]}


PLACE_DAYS_SQL = """
SELECT v.component_id, v.offer_id::text AS offer_id, v.destination_id::text AS destination_id, v.place_role,
       v.service_date, v.inventory_kind, v.free_sale_max_pax, v.min_pax, v.cutoff_hours, v.start_times,
       s.code AS service_code, s.title AS service_title
FROM booking.v_place_addons v JOIN booking.services s ON s.id = v.service_id
WHERE v.destination_id = $1::uuid AND v.service_date BETWEEN $2 AND $3
ORDER BY v.service_date, v.featured DESC, v.sort_order, s.title
LIMIT $4
"""


async def place_days(
    conn: asyncpg.Connection, destination_id: str, date_from: date, date_to: date, limit: int
) -> dict[str, Any]:
    """The staff preview of the read model: what a traveller at ``destination_id`` would be offered each day."""
    if date_to < date_from:
        raise Invalid("invalid_dates", "date_to must be on or after date_from")
    rows = await conn.fetch(PLACE_DAYS_SQL, _uuid(destination_id, "destination_id"), date_from, date_to, limit)
    policy = await policy_values.load_today(conn)
    days = policy.integer(HORIZON_KEY) if HORIZON_KEY in policy.values else None
    return {"items": [dict(r) for r in rows], "days_ahead": days}


# ── Writes ───────────────────────────────────────────────────────────────────


def _label(offer: Mapping[str, Any]) -> str:
    return f"{offer['service_code']} {offer['service_title']}"[:200]


async def _check_places(conn: asyncpg.Connection, places: list[dict[str, str]]) -> None:
    for place in places:
        await catalog.require_destination(conn, _uuid(place["destination_id"], "destination_id"))


async def _write_places(conn: asyncpg.Connection, staff: Staff, offer_id: str, places: list[dict[str, str]]) -> None:
    keep = {p["destination_id"] for p in places}
    await conn.execute(
        "DELETE FROM booking.addon_offer_places WHERE offer_id = $1::uuid AND NOT (destination_id = ANY($2::uuid[]))",
        offer_id,
        sorted(keep),
    )
    for place in places:
        await conn.execute(
            "INSERT INTO booking.addon_offer_places (offer_id, destination_id, role, created_by)"
            " VALUES ($1::uuid, $2::uuid, $3, $4)"
            " ON CONFLICT (offer_id, destination_id) DO UPDATE SET role = EXCLUDED.role",
            offer_id,
            place["destination_id"],
            place["role"],
            staff.cis_user_id,
        )
    # A removal or a role change is a write too: the person becomes a maker of the draft (bk-068 writers trigger).
    await conn.execute(
        "UPDATE booking.addon_offers SET updated_by = $2 WHERE id = $1::uuid", offer_id, staff.cis_user_id
    )


async def create_offer(
    conn: asyncpg.Connection, staff: Staff, service_ref: str, rules: Mapping[str, Any], places: list[dict[str, Any]]
) -> dict[str, Any]:
    """A draft offer for one service (409 ``offer_exists`` when the service already has one)."""
    service = await conn.fetchrow(
        "SELECT id::text AS id, code, title FROM booking.services WHERE id::text = $1 OR upper(code) = upper($1)",
        str(service_ref).strip(),
    )
    if service is None:
        raise NotFound("service_not_found", "no service with this id or code")
    values = normalize_rules(rules)
    if "cutoff_hours" not in values:
        raise Invalid("cutoff_hours_required", "an add-on needs its cut-off in hours (0 = until the start)")
    clean = normalize_places(places)
    await _check_places(conn, clean)
    columns = list(values)
    marks = ", ".join(f"${i + 3}{'::jsonb' if c == 'cancellation_tiers' else ''}" for i, c in enumerate(columns))
    try:
        offer_id = await conn.fetchval(
            f"INSERT INTO booking.addon_offers (service_id, created_by, {', '.join(columns)})"  # noqa: S608
            f" VALUES ($1::uuid, $2, {marks}) RETURNING id::text",
            service["id"],
            staff.cis_user_id,
            *values.values(),
        )
    except asyncpg.UniqueViolationError as exc:
        raise Conflict("offer_exists", f"service {service['code']} already has an add-on offer") from exc
    if clean:
        await _write_places(conn, staff, offer_id, clean)
    offer = await _get(conn, offer_id)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="addon_offer.created",
        entity_type=ENTITY,
        entity_id=offer_id,
        after={"service_id": service["id"], **_audit_values(values), "places": clean},
        entity_label=_label(offer),
        summary=f"Created add-on draft {service['code']} {service['title']}"[:300],
    )
    return await get_offer(conn, offer_id, staff)


def _audit_values(values: Mapping[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in values.items():
        if isinstance(value, list) and value and isinstance(value[0], time):
            out[key] = [t.strftime("%H:%M") for t in value]
        elif isinstance(value, Decimal):
            out[key] = format(value, "f")
        elif key == "cancellation_tiers" and isinstance(value, str):
            out[key] = json.loads(value)
        else:
            out[key] = value
    return out


async def update_offer(
    conn: asyncpg.Connection, staff: Staff, offer_id: str, changes: Mapping[str, Any]
) -> dict[str, Any]:
    """Change the rules of a draft; a published offer only moves its display order (``sort_order``, ``featured``)."""
    current = await _get(conn, offer_id, lock=True)
    values = normalize_rules(changes)
    if current["status"] == "retired":
        raise Conflict("offer_retired", "a retired offer is reopened as a draft before it changes")
    if current["status"] == "published" and set(values) - set(DISPLAY_FIELDS):
        raise Conflict("offer_published", "a published offer is fixed; reopen it as a draft to change its rules")
    before = {k: current[k] for k in values}
    shown = _audit_values(values)
    changed = {k: v for k, v in values.items() if shown[k] != before[k]}
    if not changed:
        return {**(await get_offer(conn, offer_id, staff)), "changed": []}
    sets = ", ".join(f"{c} = ${i + 2}{'::jsonb' if c == 'cancellation_tiers' else ''}" for i, c in enumerate(changed))
    by = ", updated_by = $" + str(len(changed) + 2) if current["status"] == "draft" else ""
    await conn.execute(
        f"UPDATE booking.addon_offers SET {sets}{by} WHERE id = $1::uuid",  # noqa: S608 — whitelisted columns
        current["id"],
        *changed.values(),
        *([staff.cis_user_id] if by else []),
    )
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="addon_offer.updated",
        entity_type=ENTITY,
        entity_id=current["id"],
        before={k: before[k] for k in changed},
        after=_audit_values(changed),
        entity_label=_label(current),
        summary=f"Changed {', '.join(changed)} of add-on {current['service_code']}"[:300],
    )
    return {**(await get_offer(conn, offer_id, staff)), "changed": sorted(changed)}


async def set_places(
    conn: asyncpg.Connection, staff: Staff, offer_id: str, places: list[dict[str, Any]]
) -> dict[str, Any]:
    """Replace the places of a draft (the caller becomes one of its makers)."""
    current = await _get(conn, offer_id, lock=True)
    if current["status"] != "draft":
        raise Conflict("offer_not_draft", "the places of a published or retired offer are fixed; reopen it first")
    clean = normalize_places(places)
    await _check_places(conn, clean)
    (before,) = await _attach(conn, [dict(current)])
    await _write_places(conn, staff, current["id"], clean)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="addon_offer.places_set",
        entity_type=ENTITY,
        entity_id=current["id"],
        before=[{"destination_id": p["destination_id"], "role": p["role"]} for p in before["places"]],
        after=clean,
        entity_label=_label(current),
        summary=f"Set {len(clean)} place(s) of add-on {current['service_code']}",
    )
    return await get_offer(conn, offer_id, staff)


async def publish(conn: asyncpg.Connection, staff: Staff, offer_id: str) -> dict[str, Any]:
    """A second person publishes a draft (B1). 409 when something still stops it from selling."""
    if not staff.can("addon.approve"):
        raise Forbidden("forbidden", "publishing an add-on needs addon.approve (an approval, not a write)")
    current = await _get(conn, offer_id, lock=True)
    if current["status"] != "draft":
        raise Conflict("offer_not_draft", f"only a draft is published (this one is {current['status']})")
    (current,) = await _attach(conn, [current])
    blocking = [m for m in current["missing"] if m != "no_content"]
    if blocking:
        raise Conflict(
            blocking[0],
            {
                "no_place": "add at least one place first",
                "no_price": "the service needs an adult rate under a published contract, in force today or later",
                "service_inactive": "the service is inactive",
                "supplier_inactive": "the supplier is inactive",
            }[blocking[0]],
            missing=blocking,
        )
    flagged = await self_approval.check(
        conn, staff, current["writers"], message="an add-on is published by someone who wrote none of it"
    )
    try:
        await conn.execute(PUBLISH_SQL, current["id"], staff.cis_user_id, flagged)
    except asyncpg.CheckViolationError as exc:  # the DB backstop of the maker-checker rule
        raise Forbidden(self_approval.MAKER_CHECKER, "an add-on is published by a second person") from exc
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="addon_offer.published",
        entity_type=ENTITY,
        entity_id=current["id"],
        before={"status": "draft"},
        after={"status": "published", **self_approval.flag(flagged)},
        entity_label=_label(current),
        summary=f"Published add-on {current['service_code']}" + (" (business-owner self-approval)" if flagged else ""),
    )
    return await get_offer(conn, offer_id, staff)


async def reopen(conn: asyncpg.Connection, staff: Staff, offer_id: str) -> dict[str, Any]:
    """Back to a draft (stops selling at once); the caller becomes its only maker, so another person republishes."""
    current = await _get(conn, offer_id, lock=True)
    if current["status"] == "draft":
        raise Conflict("offer_not_published", "the offer is already a draft")
    await conn.execute(REOPEN_SQL, current["id"], staff.cis_user_id)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="addon_offer.reopened",
        entity_type=ENTITY,
        entity_id=current["id"],
        before={"status": current["status"]},
        after={"status": "draft"},
        entity_label=_label(current),
        summary=f"Reopened add-on {current['service_code']} as a draft",
    )
    return await get_offer(conn, offer_id, staff)


async def retire(conn: asyncpg.Connection, staff: Staff, offer_id: str) -> dict[str, Any]:
    """Stop selling for good (kept for history; reopen to sell again)."""
    current = await _get(conn, offer_id, lock=True)
    if current["status"] == "retired":
        return {**(await get_offer(conn, offer_id, staff)), "changed": []}
    await conn.execute(RETIRE_SQL, current["id"], staff.cis_user_id)
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action="addon_offer.retired",
        entity_type=ENTITY,
        entity_id=current["id"],
        before={"status": current["status"]},
        after={"status": "retired"},
        entity_label=_label(current),
        summary=f"Retired add-on {current['service_code']}",
    )
    return {**(await get_offer(conn, offer_id, staff)), "changed": ["status"]}
