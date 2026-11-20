"""Supply restrictions and the one availability engine (AA-1145, bk-060; advisories T12, B8, T7, T13 stop-sale).

One table (``booking.supply_restrictions``) for every component, a hotel room type (``rt:``) or a service (``sv:``),
scoped to an AA supplier contract and an inclusive date range. The terms are the supplier's (DMC's) terms to AA,
never a hotel's own:

- ``allotment``: the supplier holds ``units`` per date for AA (rooms per night, seats per day); units not sold
  ``release_days`` before the date go back to the supplier;
- ``free_sale``: AA may confirm without asking, up to ``max_pax`` travellers per booking (empty = no limit);
- ``on_request``: every booking is asked of the supplier (also the default when no row covers a date);
- ``stop_sale``: the supplier closed the dates.

allotment / free_sale / on_request rows are **contract terms** (status ``contract``): written while the contract is a
draft by ``supply.write`` (the writer becomes the draft's maker, so a second person publishes them with the contract,
bk-008), fixed once published, counted only while the contract is published. A **stop-sale** is an event on a
published contract (``stop_sale.write``: contracting and admin; sales 403). Per the T13 review
(``advisory-r3-t13-roles.md`` 2.4) it takes effect the moment it is entered (status ``pending``); a second person
approves or rejects it later, and its maker may withdraw it (rejected or withdrawn = no effect). An approved
stop-sale ends only when a second person approves the end request (maker-checker, 403 ``maker_checker`` + CHECKs).

**Engine** (:func:`evaluate`, one precedence order for every caller): on a date, a stop-sale in effect wins
(``on_request``, reason ``stop_sale``); else an allotment not yet released with units left for the ask
(``available``), else a free-sale row the party fits (``available``); else ``on_request`` (an ``on_request`` row, an
exhausted or released allotment, a party over the free-sale limit, or no row at all). A stop-sale changes
availability, never the price.

**Allotment use** (:data:`USED_ROOMS_SQL`, :data:`USED_SEATS_SQL`): the supplier bookings of the component that are
requested or confirmed: one room per booking and night of a room type, the booking's pax per day of a service.
Holds do not count (the quote reserves nothing; the supplier booking does). Counting the rooms of a booking and the
contract it was bought under comes with the hotel slice.

The B2B quote (:mod:`app.b2b.quotes`) asks the engine for every night and component of every line (a ``tp:`` line
for each of its plan's components on their dates) and a stop-sale puts the line on request. Until the hold can
confirm a line with no departure at once (the supplier booking slice), a B2B line without a departure stays on
request even when the engine says available; the staff check (``GET /booking/supply/availability``) shows the
engine's full answer.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import asyncpg

from app import audit
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.suppliers import service as suppliers
from app.suppliers.service import clean_text
from app.supply.records import as_uuid

AVAILABLE, ON_REQUEST = "available", "on_request"
ALLOTMENT, FREE_SALE, ON_REQUEST_KIND, STOP_SALE = "allotment", "free_sale", "on_request", "stop_sale"
TERM_KINDS = (ALLOTMENT, FREE_SALE, ON_REQUEST_KIND)
KINDS = (*TERM_KINDS, STOP_SALE)
CONTRACT_STATUS = "contract"
STOP_IN_EFFECT = ("pending", "approved")
MAX_NOTE = 300  # technical-constant: the bk-060 CHECK length of a note or reason
MAX_SPAN_DAYS = 731  # technical-constant: the bk-060 CHECK span of one row (two years)
MAX_CHECK_DAYS = 62  # technical-constant: page size of one staff availability check (two months)

# Reasons of a verdict (the staff check shows them; the quote stores ``stop_sale`` in the line's price note).
R_STOP, R_ALLOTMENT, R_FREE = "stop_sale", "allotment", "free_sale"
R_FULL, R_RELEASED, R_OVER, R_ASK, R_NONE = "allotment_full", "released", "over_max_pax", "on_request", "no_terms"


# ── Engine (pure) ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Restriction:
    id: str
    kind: str
    date_from: date
    date_to: date  # inclusive
    status: str
    units: int | None = None
    release_days: int | None = None
    max_pax: int | None = None
    ended: bool = False

    def covers(self, day: date) -> bool:
        return self.date_from <= day <= self.date_to

    @property
    def in_effect(self) -> bool:
        """A stop-sale counts while pending or approved and not ended (T13); a contract term while it exists (the
        reader keeps only the terms of published contracts)."""
        if self.kind == STOP_SALE:
            return self.status in STOP_IN_EFFECT and not self.ended
        return self.status == CONTRACT_STATUS


@dataclass(frozen=True)
class Verdict:
    status: str  # available | on_request
    reason: str
    restriction_id: str | None = None

    def out(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "restriction_id": self.restriction_id}


def evaluate(
    rows: Iterable[Restriction], day: date, *, pax: int, units: int = 1, used: int = 0, today: date
) -> Verdict:
    """The availability of one component on ``day`` for a party of ``pax`` needing ``units`` (rooms or seats), with
    ``used`` units of the allotment already booked. Precedence: stop_sale > allotment / free_sale > on_request."""
    live = sorted((r for r in rows if r.in_effect and r.covers(day)), key=lambda r: r.id)
    stop = next((r for r in live if r.kind == STOP_SALE), None)
    if stop is not None:
        return Verdict(ON_REQUEST, R_STOP, stop.id)
    reason = R_NONE
    allotments = [r for r in live if r.kind == ALLOTMENT]
    if allotments:
        open_rows = [r for r in allotments if today <= day - timedelta(days=int(r.release_days or 0))]
        if not open_rows:
            reason = R_RELEASED
        elif sum(int(r.units or 0) for r in open_rows) - used >= units:
            return Verdict(AVAILABLE, R_ALLOTMENT, open_rows[0].id)
        else:
            reason = R_FULL
    for row in (r for r in live if r.kind == FREE_SALE):
        if row.max_pax is not None and pax > row.max_pax:
            reason = R_OVER if reason == R_NONE else reason
            continue
        return Verdict(AVAILABLE, R_FREE, row.id)
    if reason == R_NONE and any(r.kind == ON_REQUEST_KIND for r in live):
        reason = R_ASK
    return Verdict(ON_REQUEST, reason)


# ── Reads ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SupplyAsk:
    """One engine question: a room type or a service, a date, the party and the units it needs."""

    room_type_id: str | None
    service_id: str | None
    on: date
    pax: int
    units: int = 1

    @property
    def component(self) -> str:
        return self.room_type_id or self.service_id or ""


# Stop-sales in effect (whatever the contract's state now) and the terms of published contracts, over a date range.
RESTRICTIONS_SQL = """
SELECT r.id::text AS id, r.hotel_room_type_id::text AS room_type_id, r.service_id::text AS service_id,
       r.inventory_kind AS kind, r.date_from, r.date_to, r.units, r.release_days, r.max_pax, r.status,
       r.ended_at IS NOT NULL AS ended
FROM booking.supply_restrictions r
JOIN booking.supplier_contracts k ON k.id = r.supplier_contract_id
WHERE (r.hotel_room_type_id = ANY($1::uuid[]) OR r.service_id = ANY($2::uuid[]))
  AND r.date_from <= $4::date AND r.date_to >= $3::date
  AND ((r.inventory_kind = 'stop_sale' AND r.status IN ('pending', 'approved') AND r.ended_at IS NULL)
       OR (r.inventory_kind <> 'stop_sale' AND k.status = 'published'))
"""
# Rooms booked per room type and night (one room per supplier booking, requested or confirmed).
USED_ROOMS_SQL = """
SELECT b.hotel_room_type_id::text AS id, d.night::date AS day, count(*)::int AS used
FROM booking.supplier_bookings b
CROSS JOIN LATERAL generate_series(
    b.service_date::timestamp, (b.service_date + greatest(coalesce(b.nights, 1), 1) - 1)::timestamp, interval '1 day'
) AS d(night)
WHERE b.hotel_room_type_id = ANY($1::uuid[]) AND b.status IN ('requested', 'confirmed')
  AND b.service_date <= $3::date AND b.service_date + greatest(coalesce(b.nights, 1), 1) - 1 >= $2::date
GROUP BY 1, 2
"""
# Seats booked per service and day (the booking's pax, requested or confirmed).
USED_SEATS_SQL = """
SELECT b.service_id::text AS id, b.service_date AS day, sum(greatest(coalesce(b.pax, 1), 1))::int AS used
FROM booking.supplier_bookings b
WHERE b.service_id = ANY($1::uuid[]) AND b.status IN ('requested', 'confirmed')
  AND b.service_date BETWEEN $2::date AND $3::date
GROUP BY 1, 2
"""


def _row(r: Any) -> Restriction:
    return Restriction(
        id=r["id"],
        kind=r["kind"],
        date_from=r["date_from"],
        date_to=r["date_to"],
        status=r["status"],
        units=r["units"],
        release_days=r["release_days"],
        max_pax=r["max_pax"],
        ended=bool(r["ended"]),
    )


async def availability_many(conn: asyncpg.Connection, asks: Sequence[SupplyAsk], *, today: date) -> list[Verdict]:
    """The engine's verdict for every ask (same order), with one restrictions query and, only when an allotment
    applies, one usage query per component kind."""
    if not asks:
        return []
    rooms = sorted({a.room_type_id for a in asks if a.room_type_id})
    services = sorted({a.service_id for a in asks if a.service_id})
    start, stop = min(a.on for a in asks), max(a.on for a in asks)
    by_component: dict[str, list[Restriction]] = {}
    for r in await conn.fetch(RESTRICTIONS_SQL, rooms, services, start, stop):
        by_component.setdefault(r["room_type_id"] or r["service_id"], []).append(_row(r))
    used: dict[tuple[str, date], int] = {}
    allot = {c for c, rows in by_component.items() if any(r.kind == ALLOTMENT for r in rows)}
    if allot:
        usage = (
            (USED_ROOMS_SQL, [c for c in rooms if c in allot]),
            (USED_SEATS_SQL, [c for c in services if c in allot]),
        )
        for sql, ids in usage:
            if ids:
                for r in await conn.fetch(sql, ids, start, stop):
                    used[(r["id"], r["day"])] = int(r["used"])
    return [
        evaluate(
            by_component.get(a.component, ()),
            a.on,
            pax=a.pax,
            units=a.units,
            used=used.get((a.component, a.on), 0),
            today=today,
        )
        for a in asks
    ]


ROW_COLUMNS = """
    r.id::text AS id, r.supplier_contract_id::text AS contract_id, k.title AS contract_title,
    k.status AS contract_status, k.supplier_id::text AS supplier_id,
    r.hotel_room_type_id::text AS hotel_room_type_id, t.name AS room_type_name, h.code AS hotel_code,
    h.name AS hotel_name, r.service_id::text AS service_id, s.code AS service_code, s.title AS service_title,
    r.inventory_kind, r.date_from, r.date_to, r.units, r.release_days, r.max_pax, r.note, r.status,
    r.created_by, r.created_at, r.updated_by, r.updated_at, r.decided_by, r.decided_at, r.reject_reason,
    r.withdraw_reason, r.end_requested_by, r.end_requested_at, r.end_reason, r.ended_by, r.ended_at,
    sp.code AS supplier_code, sp.company_name AS supplier_name, cr.name AS created_by_name,
    er.name AS end_requested_by_name
"""
ROW_FROM = """
    booking.supply_restrictions r
    JOIN booking.supplier_contracts k ON k.id = r.supplier_contract_id
    JOIN booking.suppliers sp ON sp.id = k.supplier_id
    LEFT JOIN booking.staff_users cr ON cr.cis_user_id = r.created_by
    LEFT JOIN booking.staff_users er ON er.cis_user_id = r.end_requested_by
    LEFT JOIN booking.hotel_room_types t ON t.id = r.hotel_room_type_id
    LEFT JOIN booking.hotels h ON h.id = t.hotel_id
    LEFT JOIN booking.services s ON s.id = r.service_id
"""


def in_effect(row: dict[str, Any]) -> bool:
    if row["inventory_kind"] == STOP_SALE:
        return row["status"] in STOP_IN_EFFECT and row["ended_at"] is None
    return row["contract_status"] == "published"


def _out(row: Any) -> dict[str, Any]:
    out = dict(row)
    prefix, ident = ("rt", out["hotel_room_type_id"]) if out["hotel_room_type_id"] else ("sv", out["service_id"])
    out["component_id"] = f"{prefix}:{ident}"
    out["in_effect"] = in_effect(out)
    return out


async def get_restriction(conn: asyncpg.Connection, restriction_id: str, *, lock: bool = False) -> dict[str, Any]:
    rid = as_uuid(restriction_id, "restriction_not_found", "restriction")
    sql = f"SELECT {ROW_COLUMNS} FROM {ROW_FROM} WHERE r.id = $1::uuid"  # noqa: S608 — constants
    row = await conn.fetchrow(sql + (" FOR UPDATE OF r" if lock else ""), rid)
    if row is None:
        raise NotFound("restriction_not_found", f"restriction {restriction_id} not found")
    return _out(row)


# AA-1159: a stop-sale waits for a second person while pending (approve / reject) or while its end is asked and not
# decided yet (approve the end). Since when it waits: entered (pending) or end asked.
WAITING = (
    "r.inventory_kind = 'stop_sale' AND (r.status = 'pending'"
    " OR (r.status = 'approved' AND r.end_requested_by IS NOT NULL AND r.ended_at IS NULL))"
)
WAITING_SINCE = "CASE WHEN r.status = 'pending' THEN r.created_at ELSE r.end_requested_at END"
REVIEWS = ("waiting", "me")


def _review(add: Any, where: list[str], review: str, reviewer: str | None) -> None:
    """``waiting``: every stop-sale waiting for a second person; ``me``: those ``reviewer`` may decide (never one they
    entered or asked to end, the maker-checker rule of :func:`decide_stop_sale` and :func:`approve_end`)."""
    if review not in REVIEWS:
        raise Invalid("invalid_filter", "review is waiting or me")
    where.append(f"({WAITING})")
    if review == "me":
        add(
            "(CASE WHEN r.status = 'pending' THEN r.created_by ELSE r.end_requested_by END) <> {p}",
            reviewer or "",
        )


def restriction_filter(
    *,
    contract_id: str | None = None,
    component_id: str | None = None,
    kind: str | None = None,
    active_only: bool = False,
    review: str | None = None,
    reviewer: str | None = None,
) -> tuple[str, list[Any]]:
    """The WHERE text and arguments over :data:`ROW_FROM`: the restriction list (``GET /supply/restrictions``) and the
    My work stop-sale tile (AA-1159, :func:`review_queue`) both use it, so the tile equals the list total."""
    where: list[str] = ["true"]
    args: list[Any] = []

    def add(sql: str, value: Any) -> None:
        args.append(value)
        where.append(sql.format(p=f"${len(args)}"))

    if contract_id:
        add("r.supplier_contract_id = {p}::uuid", as_uuid(contract_id, "contract_not_found", "contract"))
    if component_id:
        room, service = parse_component(component_id)
        add(f"{'r.hotel_room_type_id' if room else 'r.service_id'} = {{p}}::uuid", room or service)
    if kind:
        if kind not in KINDS:
            raise Invalid("unknown_inventory_kind", f"inventory kind must be one of {', '.join(KINDS)}")
        add("r.inventory_kind = {p}", kind)
    if active_only:
        where.append(
            "((r.inventory_kind = 'stop_sale' AND r.status IN ('pending', 'approved') AND r.ended_at IS NULL)"
            " OR (r.inventory_kind <> 'stop_sale' AND k.status = 'published'))"
        )
    if review:
        _review(add, where, review, reviewer)
    return " AND ".join(where), args


async def list_restrictions(
    conn: asyncpg.Connection,
    *,
    contract_id: str | None = None,
    component_id: str | None = None,
    kind: str | None = None,
    active_only: bool = False,
    review: str | None = None,
    reviewer: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Restrictions of one contract or one component, newest dates first; ``kind`` filters, ``active_only`` keeps the
    rows in effect (stop-sales pending or approved and not ended, terms of published contracts). ``review``
    (AA-1159): the stop-sales waiting for a second person across every contract (``waiting``), or only those
    ``reviewer`` may decide (``me``), the longest waiting first. ``total`` counts every match (the page shows
    ``limit``)."""
    clause, args = restriction_filter(
        contract_id=contract_id,
        component_id=component_id,
        kind=kind,
        active_only=active_only,
        review=review,
        reviewer=reviewer,
    )
    total = await conn.fetchval(f"SELECT count(*) FROM {ROW_FROM} WHERE {clause}", *args)  # noqa: S608
    order = f"{WAITING_SINCE} ASC, r.id" if review else "r.date_from DESC, r.created_at DESC"
    args.append(limit)
    rows = await conn.fetch(
        f"SELECT {ROW_COLUMNS} FROM {ROW_FROM} WHERE {clause}"  # noqa: S608 — constants + placeholders
        f" ORDER BY {order} LIMIT ${len(args)}",
        *args,
    )
    return {"items": [_out(r) for r in rows], "total": int(total or 0)}


async def review_queue(conn: asyncpg.Connection, *, reviewable_by: str) -> dict[str, Any]:
    """AA-1159 My work: how many stop-sales ``reviewable_by`` may decide (the list's ``review=me``) and since when the
    longest of them waits."""
    clause, args = restriction_filter(review="me", reviewer=reviewable_by)
    row = await conn.fetchrow(
        f"SELECT count(*)::int AS count, min({WAITING_SINCE}) AS oldest_at FROM {ROW_FROM} WHERE {clause}",  # noqa: S608
        *args,
    )
    return {**dict(row), "earliest_due": None}


def parse_component(value: str) -> tuple[str | None, str | None]:
    """``rt:<uuid>`` -> (room type id, None); ``sv:<uuid>`` -> (None, service id); else 422 ``unknown_component``."""
    prefix, _, rest = str(value or "").partition(":")
    if prefix not in ("rt", "sv"):
        raise Invalid("unknown_component", "a component is rt:<room type id> or sv:<service id>")
    try:
        ident = str(uuid.UUID(rest))
    except ValueError:
        raise Invalid("unknown_component", "a component is rt:<room type id> or sv:<service id>") from None
    return (ident, None) if prefix == "rt" else (None, ident)


async def check(
    conn: asyncpg.Connection, *, component_id: str, date_from: date, date_to: date, pax: int, units: int, today: date
) -> dict[str, Any]:
    """The engine's verdict per date for one component (staff ``GET /booking/supply/availability``)."""
    if date_to < date_from or (date_to - date_from).days >= MAX_CHECK_DAYS:
        raise Invalid("invalid_range", f"check 1 to {MAX_CHECK_DAYS} dates, date_to on or after date_from")
    room, service = parse_component(component_id)
    days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]
    verdicts = await availability_many(conn, [SupplyAsk(room, service, d, pax, units) for d in days], today=today)
    return {
        "component_id": component_id,
        "pax": pax,
        "units": units,
        "days": [{"date": d.isoformat(), **v.out()} for d, v in zip(days, verdicts, strict=True)],
    }


# ── Writes ───────────────────────────────────────────────────────────────────


def _label(row: dict[str, Any]) -> str:
    if row.get("hotel_room_type_id"):
        return f"{row.get('hotel_code')} {row.get('hotel_name')} · {row.get('room_type_name')}"
    return f"{row.get('service_code')} {row.get('service_title')}"


async def _audit(conn: asyncpg.Connection, staff: Staff, action: str, row: dict[str, Any], **kw: Any) -> None:
    await audit.record(
        conn,
        actor_type="staff",
        actor_ref=staff.cis_user_id,
        action=f"supply_restriction.{action}",
        entity_type="supply_restriction",
        entity_id=row["id"],
        entity_label=f"{row['contract_title']} · {_label(row)}",
        **kw,
    )


def _numbers(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    units, release, max_pax = data.get("units"), data.get("release_days"), data.get("max_pax")
    if kind == ALLOTMENT:
        if units is None or release is None:
            raise Invalid("allotment_numbers", "an allotment needs units and release days")
        if max_pax is not None:
            raise Invalid("allotment_numbers", "max_pax belongs to a free-sale row")
    elif kind == FREE_SALE:
        if units is not None or release is not None:
            raise Invalid("free_sale_numbers", "a free-sale row has no units or release days (max_pax only)")
    elif any(v is not None for v in (units, release, max_pax)):
        raise Invalid("on_request_numbers", "an on-request row carries no numbers")
    return {"units": units, "release_days": release, "max_pax": max_pax}


def _dates(data: dict[str, Any], contract: dict[str, Any]) -> tuple[date, date]:
    start, end = data.get("date_from"), data.get("date_to")
    if start is None or end is None:
        raise Invalid("dates_required", "a restriction needs date_from and date_to")
    if end < start or (end - start).days > MAX_SPAN_DAYS:
        raise Invalid("invalid_range", "date_to is on or after date_from, at most two years later")
    k_from, k_to = contract["valid_from"], contract["valid_to"]
    if start < k_from or (k_to is not None and end > k_to):
        raise Invalid(
            "restriction_outside_contract",
            f"a restriction stays inside its contract validity ({k_from} to {k_to or 'open-ended'})",
        )
    return start, end


async def _component(
    conn: asyncpg.Connection, component_id: str, contract: dict[str, Any]
) -> tuple[str | None, str | None]:
    room, service = parse_component(component_id)
    if room:
        owner = await conn.fetchval(
            "SELECT h.supplier_id::text FROM booking.hotel_room_types t JOIN booking.hotels h ON h.id = t.hotel_id"
            " WHERE t.id = $1::uuid",
            room,
        )
    else:
        owner = await conn.fetchval("SELECT supplier_id::text FROM booking.services WHERE id = $1::uuid", service)
    if owner is None:
        raise Invalid("unknown_component", "the room type or service does not exist")
    if owner != contract["supplier_id"]:
        raise Invalid("restriction_supplier_mismatch", "the component belongs to another supplier than the contract")
    return room, service


async def _insert(
    conn: asyncpg.Connection,
    staff: Staff,
    contract: dict[str, Any],
    component: tuple[str | None, str | None],
    kind: str,
    span: tuple[date, date],
    numbers: dict[str, Any],
    note: str | None,
    status: str,
) -> str:
    try:
        return await conn.fetchval(
            "INSERT INTO booking.supply_restrictions (supplier_contract_id, hotel_room_type_id, service_id,"
            " inventory_kind, date_from, date_to, units, release_days, max_pax, note, status, created_by)"
            " VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING id::text",
            contract["id"],
            component[0],
            component[1],
            kind,
            span[0],
            span[1],
            numbers["units"],
            numbers["release_days"],
            numbers["max_pax"],
            note,
            status,
            staff.cis_user_id,
        )
    except (asyncpg.CheckViolationError, asyncpg.ObjectNotInPrerequisiteStateError) as exc:  # the DB backstop
        raise Invalid("invalid_restriction", str(exc)) from exc


async def _touch_contract(conn: asyncpg.Connection, staff: Staff, contract_id: str) -> None:
    """The term writer becomes the draft's maker (bk-008 maker-checker covers the terms too, like rates)."""
    await conn.execute(
        "UPDATE booking.supplier_contracts SET updated_by = $2 WHERE id = $1::uuid", contract_id, staff.cis_user_id
    )


async def _draft(conn: asyncpg.Connection, contract_id: str) -> dict[str, Any]:
    contract = await suppliers.get_contract(conn, contract_id, lock=True)
    if contract["status"] != "draft":
        raise Conflict(
            "contract_locked", f"the terms of a {contract['status']} contract are fixed; create a new contract instead"
        )
    return contract


async def create_term(conn: asyncpg.Connection, staff: Staff, contract_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """An allotment, free-sale or on-request row under a draft contract (``supply.write``)."""
    kind = data.get("inventory_kind")
    if kind not in TERM_KINDS:
        raise Invalid("unknown_inventory_kind", f"a contract term is one of {', '.join(TERM_KINDS)}")
    contract = await _draft(conn, contract_id)
    component = await _component(conn, str(data.get("component_id") or ""), contract)
    span = _dates(data, contract)
    numbers = _numbers(kind, data)
    rid = await _insert(conn, staff, contract, component, kind, span, numbers, clean_text(data.get("note")), "contract")
    await _touch_contract(conn, staff, contract["id"])
    row = await get_restriction(conn, rid)
    await _audit(
        conn,
        staff,
        "created",
        row,
        after={k: row[k] for k in ("component_id", "inventory_kind", "date_from", "date_to", *numbers, "note")},
        summary=f"Added {kind.replace('_', ' ')} {span[0]} to {span[1]}",
    )
    return row


TERM_EDITABLE = ("date_from", "date_to", "units", "release_days", "max_pax", "note")


async def update_term(
    conn: asyncpg.Connection, staff: Staff, restriction_id: str, changes: dict[str, Any]
) -> dict[str, Any]:
    unknown = sorted(set(changes) - set(TERM_EDITABLE))
    if unknown:
        raise Invalid("not_editable", f"not editable: {', '.join(unknown)}")
    current = await get_restriction(conn, restriction_id, lock=True)
    if current["inventory_kind"] == STOP_SALE:
        raise Conflict("stop_sale_fixed", "a stop-sale keeps its facts; withdraw it or request its end")
    contract = await _draft(conn, current["contract_id"])
    merged = {**current, **changes}
    if "note" in changes:
        merged["note"] = clean_text(changes["note"])
    span = _dates(merged, contract)
    numbers = _numbers(current["inventory_kind"], merged)
    values = {"date_from": span[0], "date_to": span[1], **numbers, "note": merged["note"]}
    changed = {k: v for k, v in values.items() if current[k] != v}
    if not changed:
        return {**current, "changed": []}
    sets = ", ".join(f"{column} = ${i + 2}" for i, column in enumerate(changed))
    try:
        await conn.execute(
            f"UPDATE booking.supply_restrictions SET {sets}, updated_by = ${len(changed) + 2}"  # noqa: S608
            " WHERE id = $1::uuid",
            current["id"],
            *changed.values(),
            staff.cis_user_id,
        )
    except (asyncpg.CheckViolationError, asyncpg.ObjectNotInPrerequisiteStateError) as exc:
        raise Invalid("invalid_restriction", str(exc)) from exc
    await _touch_contract(conn, staff, contract["id"])
    await _audit(
        conn,
        staff,
        "updated",
        current,
        before={k: current[k] for k in changed},
        after=changed,
        summary=f"Updated {current['inventory_kind'].replace('_', ' ')} {', '.join(sorted(changed))}",
    )
    return {**(await get_restriction(conn, current["id"])), "changed": sorted(changed)}


async def delete_term(conn: asyncpg.Connection, staff: Staff, restriction_id: str) -> None:
    current = await get_restriction(conn, restriction_id, lock=True)
    if current["inventory_kind"] == STOP_SALE:
        raise Conflict("stop_sale_fixed", "a stop-sale is never deleted; withdraw it or request its end")
    contract = await _draft(conn, current["contract_id"])
    await conn.execute("DELETE FROM booking.supply_restrictions WHERE id = $1::uuid", current["id"])
    await _touch_contract(conn, staff, contract["id"])
    await _audit(
        conn,
        staff,
        "deleted",
        current,
        before={k: current[k] for k in ("component_id", "inventory_kind", "date_from", "date_to")},
        summary=f"Removed {current['inventory_kind'].replace('_', ' ')} {current['date_from']} to {current['date_to']}",
    )


# ── Stop-sale (T13: in effect at once, a second person reviews; ending an approved one needs a second person) ──


def _reason(text: Any, code: str) -> str:
    value = clean_text(text)
    if not value:
        raise Invalid(code, "a reason is required")
    if len(value) > MAX_NOTE:
        raise Invalid(code, f"at most {MAX_NOTE} characters")
    return value


async def create_stop_sale(
    conn: asyncpg.Connection, staff: Staff, contract_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """A stop-sale on a published contract (``stop_sale.write``): in effect at once (status pending)."""
    contract = await suppliers.get_contract(conn, contract_id, lock=True)
    if contract["status"] != "published":
        raise Conflict("contract_not_published", "a stop-sale applies to a published contract")
    component = await _component(conn, str(data.get("component_id") or ""), contract)
    span = _dates(data, contract)
    note = _reason(data.get("note"), "note_required")
    numbers = {"units": None, "release_days": None, "max_pax": None}
    rid = await _insert(conn, staff, contract, component, STOP_SALE, span, numbers, note, "pending")
    row = await get_restriction(conn, rid)
    await _audit(
        conn,
        staff,
        "stop_sale_set",
        row,
        after={k: row[k] for k in ("component_id", "date_from", "date_to", "note", "status")},
        summary=f"Stop-sale {span[0]} to {span[1]} in effect, waiting for a second person",
    )
    return row


async def _stop_sale(conn: asyncpg.Connection, restriction_id: str) -> dict[str, Any]:
    row = await get_restriction(conn, restriction_id, lock=True)
    if row["inventory_kind"] != STOP_SALE:
        raise NotFound("restriction_not_found", "no stop-sale with this id")
    return row


def require_approve(staff: Staff) -> None:
    """AA-1150: reviewing a stop-sale or approving its end is an approval (``stop_sale.approve``), not a write."""
    if not staff.can("stop_sale.approve"):
        raise Forbidden("forbidden", "reviewing or ending a stop-sale needs stop_sale.approve")


async def decide_stop_sale(
    conn: asyncpg.Connection, staff: Staff, restriction_id: str, action: str, reason: str | None = None
) -> dict[str, Any]:
    """``approve`` / ``reject`` (a second person, never the maker) or ``withdraw`` (the maker) a pending stop-sale.
    AA-1150: approve / reject is a review and needs ``stop_sale.approve`` (checked before any read); withdraw stays
    with the route's ``stop_sale.write``."""
    if action in ("approve", "reject"):
        require_approve(staff)
    row = await _stop_sale(conn, restriction_id)
    if row["status"] != "pending":
        raise Conflict("stop_sale_not_pending", f"the stop-sale is {row['status']}")
    me = staff.cis_user_id
    if action in ("approve", "reject") and me == row["created_by"]:
        raise Forbidden("maker_checker", "a second person reviews a stop-sale, never the one who entered it")
    if action == "withdraw" and me != row["created_by"]:
        raise Forbidden("not_maker", "only the person who entered a stop-sale withdraws it; others reject it")
    status = {"approve": "approved", "reject": "rejected", "withdraw": "withdrawn"}.get(action)
    if status is None:
        raise Invalid("unknown_action", "approve, reject or withdraw")
    extra = {
        "approved": ("", None),
        "rejected": (", reject_reason = $4", _reason(reason, "reason_required") if action == "reject" else None),
        "withdrawn": (", withdraw_reason = $4", _reason(reason, "reason_required") if action == "withdraw" else None),
    }[status]
    args: list[Any] = [row["id"], status, me]
    if extra[1] is not None:
        args.append(extra[1])
    await conn.execute(
        "UPDATE booking.supply_restrictions SET status = $2, decided_by = $3, decided_at = now(), updated_by = $3"  # noqa: S608
        f"{extra[0]} WHERE id = $1::uuid",
        *args,
    )
    out = await get_restriction(conn, row["id"])
    await _audit(
        conn,
        staff,
        f"stop_sale_{status}",
        out,
        before={"status": "pending"},
        after={"status": status, **({"reason": extra[1]} if extra[1] else {})},
        summary=f"Stop-sale {status}" + ("" if status == "approved" else " (no longer in effect)"),
    )
    return out


async def request_end(conn: asyncpg.Connection, staff: Staff, restriction_id: str, reason: Any) -> dict[str, Any]:
    """Ask to lift an approved stop-sale; it stays in effect until a second person approves the end."""
    row = await _stop_sale(conn, restriction_id)
    if row["status"] != "approved" or row["ended_at"] is not None:
        raise Conflict("stop_sale_not_approved", "only an approved stop-sale in effect is ended this way")
    if row["end_requested_by"] is not None:
        raise Conflict("end_already_requested", "an end request is already waiting")
    text = _reason(reason, "reason_required")
    await conn.execute(
        "UPDATE booking.supply_restrictions SET end_requested_by = $2, end_requested_at = now(), end_reason = $3,"
        " updated_by = $2 WHERE id = $1::uuid",
        row["id"],
        staff.cis_user_id,
        text,
    )
    out = await get_restriction(conn, row["id"])
    await _audit(
        conn, staff, "stop_sale_end_requested", out, after={"end_reason": text}, summary="End of stop-sale requested"
    )
    return out


async def approve_end(conn: asyncpg.Connection, staff: Staff, restriction_id: str) -> dict[str, Any]:
    require_approve(staff)
    row = await _stop_sale(conn, restriction_id)
    if row["end_requested_by"] is None or row["ended_at"] is not None:
        raise Conflict("no_end_request", "no end request is waiting")
    if staff.cis_user_id == row["end_requested_by"]:
        raise Forbidden("maker_checker", "a second person approves the end of a stop-sale")
    await conn.execute(
        "UPDATE booking.supply_restrictions SET ended_by = $2, ended_at = now(), updated_by = $2 WHERE id = $1::uuid",
        row["id"],
        staff.cis_user_id,
    )
    out = await get_restriction(conn, row["id"])
    await _audit(conn, staff, "stop_sale_ended", out, after={"ended": True}, summary="Stop-sale lifted")
    return out


async def cancel_end_request(conn: asyncpg.Connection, staff: Staff, restriction_id: str) -> dict[str, Any]:
    """The requester takes an end request back (the stop-sale stays in effect)."""
    row = await _stop_sale(conn, restriction_id)
    if row["end_requested_by"] is None or row["ended_at"] is not None:
        raise Conflict("no_end_request", "no end request is waiting")
    if staff.cis_user_id != row["end_requested_by"]:
        raise Forbidden("not_maker", "only the requester takes an end request back")
    await conn.execute(
        "UPDATE booking.supply_restrictions SET end_requested_by = NULL, end_requested_at = NULL, end_reason = NULL,"
        " updated_by = $2 WHERE id = $1::uuid",
        row["id"],
        staff.cis_user_id,
    )
    out = await get_restriction(conn, row["id"])
    await _audit(conn, staff, "stop_sale_end_withdrawn", out, summary="End request of stop-sale taken back")
    return out
