"""The availability engine's reads: restrictions, allotment use, and the B2B room line answer (AA-1145 bk-060; AA-1153
part 2, S285 advisory r2 hotels B8).

:func:`many` asks :func:`app.supply.restrictions.evaluate` (one precedence for every caller: stop_sale > allotment /
free_sale > on_request) for a list of :class:`~app.supply.restrictions.SupplyAsk`, with one restrictions query and,
only when an allotment applies, the usage queries.

**Per DMC** (AA-1153 part 2): an ask that names a ``supplier_id`` (the DMC whose rate priced it; a hotel may be sold by
several DMCs, each under its own AA contract) reads only that supplier's restrictions (its allotment, free-sale and
on-request terms and its stop-sales) and counts only that supplier's use. An ask without one reads every supplier's
rows and use (the staff check of a component).

**One contract per DMC and date** (:func:`app.supply.restrictions.current_terms`): when two published contracts of
one supplier carry terms on a date (an exported, edited and re-imported contract), only the newest published one's
terms count, so a round trip never doubles an allotment.

**Allotment use** = units already taken for the component, supplier and date:

- supplier bookings ``requested`` or ``confirmed`` (rooms: ``quantity`` rooms, at least 1, per night of the stay;
  services: the booking's pax per day), except the booking of a B2B line held ``available`` (the line counts below,
  with its real room count);
- rooms of B2B ``rt:`` lines held ``available`` (from an allotment or free-sale term) on a live hold (``active`` and
  not expired, or ``confirmed`` and the line not cancelled or declined): the quote line's room mix (the rooms asked,
  else the default mix ``party // 2`` doubles plus one single), per night, under the DMC of its first rate;
- inside one call, every earlier ask an allotment answered (two lines of one quote share the allotment).

``exclude_hold``: a hold left out of the count (a new quote of the same proposal version replaces it, as the
departure seats of ``app.b2b.departures`` do).

**B2B room lines** (:func:`room_line`): an ``rt:`` line has no departure, so the departure check answers it
``on_request``; it is ``available`` when the engine answers ``available`` for every night (an allotment with rooms
left that is not released yet, or a free-sale term the party fits), else ``on_request`` with the nights and reasons in
``price_note.supply``. With an allotment of 2 rooms and 7 release days: rooms 1 and 2 are available, a 3rd room (in
the same quote or after two rooms were held) or any room inside the 7 days before the night is on request. The hold
re-checks it under a per-room-type lock (:mod:`app.b2b.room_hold`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

import asyncpg

from app.errors import Invalid
from app.supply.restrictions import (
    AVAILABLE,
    ON_REQUEST,
    R_FULL,
    R_OVER,
    R_RELEASED,
    R_STOP,
    Restriction,
    SupplyAsk,
    Verdict,
    evaluate,
    parse_component,
)

MAX_CHECK_DAYS = 62  # technical-constant: page size of one staff availability check (two months)
MAX_NOTE_DATES = 10  # technical-constant: dates listed per reason in a quote line note (bk-019 note size CHECK)

# Stop-sales in effect (whatever the contract's state now) and the terms of published contracts, over a date range.
RESTRICTIONS_SQL = """
SELECT r.id::text AS id, r.hotel_room_type_id::text AS room_type_id, r.service_id::text AS service_id,
       k.supplier_id::text AS supplier_id, k.id::text AS contract_id, k.published_at, r.inventory_kind AS kind,
       r.date_from, r.date_to, r.units, r.release_days, r.max_pax, r.status, r.ended_at IS NOT NULL AS ended
FROM booking.supply_restrictions r
JOIN booking.supplier_contracts k ON k.id = r.supplier_contract_id
WHERE (r.hotel_room_type_id = ANY($1::uuid[]) OR r.service_id = ANY($2::uuid[]))
  AND r.date_from <= $4::date AND r.date_to >= $3::date
  AND ((r.inventory_kind = 'stop_sale' AND r.status IN ('pending', 'approved') AND r.ended_at IS NULL)
       OR (r.inventory_kind <> 'stop_sale' AND k.status = 'published'))
"""
# A supplier booking of a B2B line held from an allotment: the hold line counts it (HELD_ROOMS_SQL), not the booking.
_HELD_LINE_BOOKING = """
EXISTS (SELECT 1 FROM booking.trip_case_version_itinerary i
        JOIN booking.trip_case_versions v ON v.id = i.trip_case_version_id
        JOIN booking.b2b_holds h ON h.trip_case_id = v.trip_case_id
        JOIN booking.b2b_hold_lines l ON l.hold_id = h.id AND l.line_no = i.position
        WHERE i.id = b.version_item_id AND l.held_as = 'available' AND l.status = 'confirmed')
"""
# Rooms booked per room type, supplier and night (quantity rooms per supplier booking, requested or confirmed).
USED_ROOMS_SQL = f"""
SELECT b.hotel_room_type_id::text AS id, b.supplier_id::text AS supplier_id, d.night::date AS day,
       sum(greatest(coalesce(b.quantity, 1), 1))::int AS used
FROM booking.supplier_bookings b
CROSS JOIN LATERAL generate_series(
    b.service_date::timestamp, (b.service_date + greatest(coalesce(b.nights, 1), 1) - 1)::timestamp, interval '1 day'
) AS d(night)
WHERE b.hotel_room_type_id = ANY($1::uuid[]) AND b.status IN ('requested', 'confirmed')
  AND b.service_date <= $3::date AND b.service_date + greatest(coalesce(b.nights, 1), 1) - 1 >= $2::date
  AND NOT {_HELD_LINE_BOOKING}
GROUP BY 1, 2, 3
"""  # noqa: S608 — constant fragment
# Seats booked per service, supplier and day (the booking's pax, requested or confirmed).
USED_SEATS_SQL = """
SELECT b.service_id::text AS id, b.supplier_id::text AS supplier_id, b.service_date AS day,
       sum(greatest(coalesce(b.pax, 1), 1))::int AS used
FROM booking.supplier_bookings b
WHERE b.service_id = ANY($1::uuid[]) AND b.status IN ('requested', 'confirmed')
  AND b.service_date BETWEEN $2::date AND $3::date
GROUP BY 1, 2, 3
"""
# The rooms of a B2B quote line q: the room mix asked, else the default mix (party // 2 doubles + one single when odd,
# app.b2b.components.room_mix).
ROOMS_OF_LINE = """CASE WHEN jsonb_array_length(q.rooms) > 0
    THEN (SELECT sum(greatest(coalesce((e ->> 'count')::int, 1), 1)) FROM jsonb_array_elements(q.rooms) e)
    ELSE (q.pax_adult + q.pax_child + 1) / 2 END"""
# Rooms of live B2B rt: lines held available, per room type, DMC (the supplier of the line's first rate) and night.
HELD_ROOMS_SQL = f"""
WITH held AS (
    SELECT substr(q.component_id, 4)::uuid AS room_type_id, k.supplier_id, q.service_date,
           greatest(coalesce(q.nights, 1), 1) AS nights,
           {ROOMS_OF_LINE} AS rooms
    FROM booking.b2b_hold_lines l
    JOIN booking.b2b_holds h ON h.id = l.hold_id
    JOIN booking.b2b_quote_lines q ON q.quote_id = h.quote_id AND q.line_no = l.line_no
    JOIN booking.supplier_rates x ON x.id = q.supplier_rate_ids[1]
    JOIN booking.supplier_contracts k ON k.id = x.supplier_contract_id
    WHERE l.held_as = 'available' AND q.component_id LIKE 'rt:%'
      AND ((h.status = 'active' AND h.expires_at > now() AND l.status = 'available')
           OR (h.status = 'confirmed' AND l.status = 'confirmed'))
      AND h.id IS DISTINCT FROM $4::uuid
      AND q.service_date <= $3::date AND q.service_date + greatest(coalesce(q.nights, 1), 1) - 1 >= $2::date
)
SELECT held.room_type_id::text AS id, held.supplier_id::text AS supplier_id, d.night::date AS day,
       sum(held.rooms)::int AS used
FROM held
CROSS JOIN LATERAL generate_series(
    held.service_date::timestamp, (held.service_date + held.nights - 1)::timestamp, interval '1 day'
) AS d(night)
WHERE held.room_type_id = ANY($1::uuid[])
GROUP BY 1, 2, 3
"""  # noqa: S608 — constant fragment

Key = tuple[str, str | None, date]


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
        supplier_id=r["supplier_id"],
        contract_id=r["contract_id"],
        published_at=r["published_at"],
    )


def _used(used: dict[Key, int], component: str, supplier: str | None, day: date) -> int:
    """Units taken for the ask: that supplier's, or every supplier's when the ask names none."""
    if supplier is not None:
        return used.get((component, supplier, day), 0)
    return sum(n for (c, _, d), n in used.items() if c == component and d == day)


async def _usage(
    conn: asyncpg.Connection, rooms: list[str], services: list[str], span: tuple[date, date], exclude_hold: str | None
) -> dict[Key, int]:
    used: dict[Key, int] = {}
    queries: list[tuple[str, list[str], tuple[Any, ...]]] = [
        (USED_ROOMS_SQL, rooms, ()),
        (HELD_ROOMS_SQL, rooms, (exclude_hold,)),
        (USED_SEATS_SQL, services, ()),
    ]
    for sql, ids, extra in queries:
        if not ids:
            continue
        for r in await conn.fetch(sql, ids, span[0], span[1], *extra):
            key = (r["id"], r["supplier_id"], r["day"])
            used[key] = used.get(key, 0) + int(r["used"])
    return used


async def many(
    conn: asyncpg.Connection, asks: Sequence[SupplyAsk], *, today: date, exclude_hold: str | None = None
) -> list[Verdict]:
    """The engine's verdict for every ask (same order). An allotment answer takes its units, so a later ask of the same
    component, supplier and date sees fewer left."""
    if not asks:
        return []
    rooms = sorted({a.room_type_id for a in asks if a.room_type_id})
    services = sorted({a.service_id for a in asks if a.service_id})
    span = (min(a.on for a in asks), max(a.on for a in asks))
    by_component: dict[str, list[Restriction]] = {}
    for r in await conn.fetch(RESTRICTIONS_SQL, rooms, services, span[0], span[1]):
        by_component.setdefault(r["room_type_id"] or r["service_id"], []).append(_row(r))
    allot = {c for c, rows in by_component.items() if any(r.kind == "allotment" for r in rows)}
    used: dict[Key, int] = {}
    if allot:
        used = await _usage(
            conn, [c for c in rooms if c in allot], [c for c in services if c in allot], span, exclude_hold
        )
    taken: dict[Key, int] = {}
    out: list[Verdict] = []
    for a in asks:
        rows = [r for r in by_component.get(a.component, ()) if a.supplier_id is None or r.supplier_id == a.supplier_id]
        key = (a.component, a.supplier_id, a.on)
        prior = _used(used, a.component, a.supplier_id, a.on) + taken.get(key, 0)
        verdict = evaluate(rows, a.on, pax=a.pax, units=a.units, used=prior, today=today)
        if verdict.reason == "allotment":
            taken[key] = taken.get(key, 0) + a.units
        out.append(verdict)
    return out


async def check(
    conn: asyncpg.Connection,
    *,
    component_id: str,
    date_from: date,
    date_to: date,
    pax: int,
    units: int,
    today: date,
    supplier_id: str | None = None,
) -> dict[str, Any]:
    """The engine's verdict per date for one component (staff ``GET /booking/supply/availability``); ``supplier_id``
    reads one DMC's terms and use only (a hotel sold by several DMCs)."""
    if date_to < date_from or (date_to - date_from).days >= MAX_CHECK_DAYS:
        raise Invalid("invalid_range", f"check 1 to {MAX_CHECK_DAYS} dates, date_to on or after date_from")
    room, service = parse_component(component_id)
    if supplier_id is not None:
        try:
            supplier_id = str(uuid.UUID(supplier_id))
        except ValueError:
            raise Invalid("unknown_supplier", "supplier_id is a supplier UUID") from None
    days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]
    verdicts = await many(conn, [SupplyAsk(room, service, d, pax, units, supplier_id) for d in days], today=today)
    return {
        "component_id": component_id,
        "supplier_id": supplier_id,
        "pax": pax,
        "units": units,
        "days": [{"date": d.isoformat(), **v.out()} for d, v in zip(days, verdicts, strict=True)],
    }


# ── B2B quote lines ──────────────────────────────────────────────────────────


def _dates(days: list[date], verdicts: list[Verdict], reasons: tuple[str, ...]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for d, v in zip(days, verdicts, strict=True):
        if v.reason in reasons:
            listed = out.setdefault(v.reason, [])
            if d.isoformat() not in listed:
                listed.append(d.isoformat())
    return {k: sorted(v)[:MAX_NOTE_DATES] if k != R_STOP else sorted(v) for k, v in out.items()}


def line_availability(
    availability: str, component_id: str, verdicts: list[Verdict], days: list[date]
) -> tuple[str, dict[str, Any] | None]:
    """A quote line's availability after the engine. A stop-sale in effect on any night or component puts an available
    line on request (``price_note.supply.stop_sale`` names the dates; AA-1145). AA-1153 B8: an ``rt:`` line is
    :func:`room_line`. A sold-out line stays sold out."""
    if component_id.startswith("rt:"):
        return room_line(availability, verdicts, days)
    stops = _dates(days, verdicts, (R_STOP,)).get(R_STOP)
    if not stops:
        return availability, None
    return (ON_REQUEST if availability == AVAILABLE else availability), {"stop_sale": stops}


def room_line(availability: str, verdicts: list[Verdict], days: list[date]) -> tuple[str, dict[str, Any] | None]:
    """An ``rt:`` line (no departure: the departure check answered ``on_request``) is ``available`` when every night
    and room unit has an allotment with rooms left (not released) or a free-sale term the party fits, else
    ``on_request``. The note says which term made it available (``inventory``) or why a night is on request
    (``stop_sale`` every date; ``allotment_full`` / ``released`` / ``over_max_pax`` up to :data:`MAX_NOTE_DATES`)."""
    if not verdicts or availability == "sold_out":
        return availability, None
    if all(v.status == AVAILABLE for v in verdicts):
        kinds = sorted({v.reason for v in verdicts})
        return AVAILABLE, {"inventory": kinds[0] if len(kinds) == 1 else "mixed"}
    reasons = _dates(days, verdicts, (R_STOP, R_FULL, R_RELEASED, R_OVER))
    return ON_REQUEST, (reasons or None)
