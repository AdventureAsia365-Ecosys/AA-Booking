"""Bookable components of B2B products and the net cost of quote lines (AA-972, bk-019).

Component ids are typed and opaque to the CRM (contract ``Component.component_id``):

- ``rt:<hotel_room_types.id>`` kind ``hotel_room``, priced per room night;
- ``sv:<services.id>`` kind = the service type (``transfer``, ``guide``, ``activity``, ``entrance``, ``meal``), priced
  per the unit of its rate;
- ``tp:<trip_plans.id>`` kind ``trip_plan``: the whole plan = the sum of its ``booking.trip_plan_components``.

Net cost = the supplier rate in force (bk-010: published contract, both validities cover the day; the newest
``valid_from`` wins, then a pax band, then the lowest net; ``app.supply.rates``):

- room: one rate per night (``service_date`` + i for each of ``nights``, default 1) **per room of the mix**, the pax
  band matched against that room's occupancy (single 1, double / twin 2, triple 3, family = the travellers not in the
  other rooms shared over the family rooms, at most the room type's ``max_occupancy``), times the rooms of that
  occupancy. No ``rooms`` = ``party // 2`` doubles plus one single when the party is odd. The rooms must hold the
  adults (children may share), and no room may exceed its room type's ``max_occupancy``;
- service: rate for the party size (adults + children; infants are never counted), ``person`` = x party,
  ``group`` / ``vehicle`` = x 1, ``day`` = x ``nights`` (default 1);
- trip plan: each component on ``service_date + day_no - 1``, a room component = one night of the line's room mix, a
  ``day`` service = one day.

Bounded work (AA-972 review): items, plans and their components are read with one query each for the whole quote,
every rate with one batched query (``rates_in_force_many``), and a quote needing more than :data:`MAX_RATE_LOOKUPS`
rate lookups is refused before any is run (422 ``invalid``).

A line with no rate in force (or an inactive item) is refused with 422 ``invalid`` naming the line; nothing is ever
priced at a guessed or zero net. Only ids, statuses, units and net amounts are read here: no content (O7).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app.b2b.errors import B2BError
from app.supply.rates import RateAsk, rates_in_force_many

PREFIX_ROOM = "rt"
PREFIX_SERVICE = "sv"
PREFIX_PLAN = "tp"
KIND_ROOM = "hotel_room"
KIND_PLAN = "trip_plan"
SERVICE_KINDS = frozenset({"transfer", "guide", "activity", "entrance", "meal"})
OCCUPANCY_PAX = {"single": 1, "double": 2, "twin": 2, "triple": 3}
FAMILY = "family"
MAX_RATE_LOOKUPS = 1000  # one quote's budget: 200 lines x 5 nights, or a 5-line quote of 200-component plans


def component_id(prefix: str, ident: str) -> str:
    return f"{prefix}:{ident}"


def parse_component(value: str) -> tuple[str, str]:
    prefix, _, rest = value.partition(":")
    if prefix not in (PREFIX_ROOM, PREFIX_SERVICE, PREFIX_PLAN):
        raise ValueError(value)
    return prefix, str(uuid.UUID(rest))


@dataclass(frozen=True)
class RoomAsk:
    """One ``rooms[]`` entry of a request line (contract ``Room``)."""

    occupancy: str
    count: int = 1
    room_type_id: str | None = None  # as sent: ``rt:<uuid>`` or ``<uuid>``


@dataclass(frozen=True)
class LineAsk:
    line_ref: str
    component_id: str
    service_date: date
    nights: int | None
    adult: int
    child: int
    infant: int
    rooms: tuple[RoomAsk, ...] = ()

    @property
    def party(self) -> int:
        return self.adult + self.child


@dataclass
class LineCost:
    kind: str
    costs: list[tuple[Decimal, str]] = field(default_factory=list)  # (net amount, currency)
    rate_ids: list[str] = field(default_factory=list)


PLANS_SQL = "SELECT id::text AS id, sellable FROM booking.trip_plans WHERE id = ANY($1::uuid[])"
PLAN_COMPONENTS_SQL = """
SELECT trip_plan_id::text AS trip_plan_id, day_no, hotel_room_type_id::text AS room_type_id,
       service_id::text AS service_id
FROM booking.trip_plan_components WHERE trip_plan_id = ANY($1::uuid[])
ORDER BY trip_plan_id, day_no, coalesce(hotel_room_type_id, service_id)
"""
ROOMS_SQL = """
SELECT rt.id::text AS id, rt.status, rt.max_occupancy, h.status AS hotel_status
FROM booking.hotel_room_types rt JOIN booking.hotels h ON h.id = rt.hotel_id
WHERE rt.id = ANY($1::uuid[])
"""
SERVICES_SQL = "SELECT id::text AS id, status, service_type FROM booking.services WHERE id = ANY($1::uuid[])"


def _no_price(line_ref: str, why: str) -> B2BError:
    return B2BError("invalid", f"line {line_ref}: {why}")


def _service_quantity(unit: str, ask: LineAsk, *, days: int) -> int:
    if unit == "person":
        return ask.party
    if unit == "day":
        return days
    return 1  # group, vehicle


def _room_ref(ask: LineAsk, value: str) -> str:
    raw = value[len(PREFIX_ROOM) + 1 :] if value.startswith(f"{PREFIX_ROOM}:") else value
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise _no_price(ask.line_ref, "rooms[].room_type_id is not a room type id") from None


def check_room_types(ask: LineAsk, allowed: set[str]) -> None:
    """A ``rooms[].room_type_id`` must name the line's room type (``rt:``) or the plan's single room type (``tp:``)."""
    for room in ask.rooms:
        if room.room_type_id is not None and (len(allowed) != 1 or _room_ref(ask, room.room_type_id) not in allowed):
            raise _no_price(
                ask.line_ref, "rooms[].room_type_id does not match the component's room type (leave it empty)"
            )


def room_mix(ask: LineAsk, max_occupancy: int | None) -> list[tuple[int, int]]:
    """``[(occupancy pax, room count)]`` of one line for one room type, or B2BError 422."""
    if not ask.rooms:
        mix = [(2, ask.party // 2), (1, ask.party % 2)]
        mix = [(pax, n) for pax, n in mix if n]
    else:
        fixed = sum(OCCUPANCY_PAX[r.occupancy] * r.count for r in ask.rooms if r.occupancy != FAMILY)
        families = sum(r.count for r in ask.rooms if r.occupancy == FAMILY)
        counts: dict[int, int] = {}
        for room in ask.rooms:
            if room.occupancy != FAMILY:
                pax = OCCUPANCY_PAX[room.occupancy]
                counts[pax] = counts.get(pax, 0) + room.count
        if families:
            share = max(1, math.ceil(max(ask.party - fixed, 0) / families))
            if max_occupancy is not None:
                share = min(share, max_occupancy)
            counts[share] = counts.get(share, 0) + families
        mix = sorted(counts.items())
    if max_occupancy is not None and any(pax > max_occupancy for pax, _ in mix):
        raise _no_price(ask.line_ref, f"a room holds at most {max_occupancy} on this room type")
    capacity = sum(pax * n for pax, n in mix)
    if capacity < ask.adult:
        raise _no_price(ask.line_ref, f"the rooms hold {capacity} travellers, fewer than {ask.adult} adults")
    return mix


@dataclass(frozen=True)
class _Unit:
    """One rate lookup of one line: a room night of one occupancy (x rooms), or one service."""

    line: int
    room_type_id: str | None
    service_id: str | None
    on: date
    pax: int
    rooms: int = 0  # room count (rooms only)
    days: int = 1  # days of a ``day`` service


class _Budget:
    def __init__(self) -> None:
        self.used = 0

    def take(self, n: int) -> None:
        self.used += n
        if self.used > MAX_RATE_LOOKUPS:
            raise B2BError(
                "invalid",
                f"quote too large: more than {MAX_RATE_LOOKUPS} rate lookups (nights x room mixes x components);"
                " split it into several quotes",
            )


async def price_lines(conn: asyncpg.Connection, asks: list[LineAsk]) -> list[LineCost]:
    """The kind and net cost lines of every quote line (same order), or B2BError 422.

    A fixed number of queries whatever the request: plans, plan components, room types, services, then one batched
    rate query. The lookup budget is checked while the lookups are planned, before the rate query runs.
    """
    parsed: list[tuple[str, str]] = []
    for ask in asks:
        try:
            parsed.append(parse_component(ask.component_id))
        except ValueError:
            raise _no_price(ask.line_ref, "unknown component_id") from None
    plan_ids = sorted({ident for prefix, ident in parsed if prefix == PREFIX_PLAN})
    plans = {r["id"]: r for r in await conn.fetch(PLANS_SQL, plan_ids)} if plan_ids else {}
    parts: dict[str, list[Any]] = {pid: [] for pid in plan_ids}
    if plan_ids:
        for row in await conn.fetch(PLAN_COMPONENTS_SQL, plan_ids):
            parts[row["trip_plan_id"]].append(row)
    room_ids = {ident for prefix, ident in parsed if prefix == PREFIX_ROOM}
    service_ids = {ident for prefix, ident in parsed if prefix == PREFIX_SERVICE}
    for rows in parts.values():
        room_ids |= {r["room_type_id"] for r in rows if r["room_type_id"]}
        service_ids |= {r["service_id"] for r in rows if r["service_id"]}
    rooms = {r["id"]: r for r in await conn.fetch(ROOMS_SQL, sorted(room_ids))} if room_ids else {}
    services = {r["id"]: r for r in await conn.fetch(SERVICES_SQL, sorted(service_ids))} if service_ids else {}

    budget = _Budget()
    out: list[LineCost] = []
    units: list[_Unit] = []

    def room_units(no: int, ask: LineAsk, room_type_id: str, *, start: date, nights: int) -> None:
        row = rooms.get(room_type_id)
        if row is None or row["status"] != "active" or row["hotel_status"] != "active":
            raise _no_price(ask.line_ref, "unknown or inactive room type")
        mix = room_mix(ask, row["max_occupancy"])
        budget.take(nights * len(mix))
        for i in range(nights):
            for pax, count in mix:
                units.append(_Unit(no, room_type_id, None, start + timedelta(days=i), pax, rooms=count))

    def service_unit(no: int, ask: LineAsk, service_id: str, *, on: date, days: int) -> str:
        row = services.get(service_id)
        if row is None or row["status"] != "active" or row["service_type"] not in SERVICE_KINDS:
            raise _no_price(ask.line_ref, "unknown or inactive service")
        budget.take(1)
        units.append(_Unit(no, None, service_id, on, ask.party, days=days))
        return str(row["service_type"])

    for no, (ask, (prefix, ident)) in enumerate(zip(asks, parsed, strict=True)):
        nights = ask.nights or 1
        if prefix == PREFIX_ROOM:
            check_room_types(ask, {ident})
            out.append(LineCost(kind=KIND_ROOM))
            room_units(no, ask, ident, start=ask.service_date, nights=nights)
        elif prefix == PREFIX_SERVICE:
            check_room_types(ask, set())
            out.append(LineCost(kind=service_unit(no, ask, ident, on=ask.service_date, days=nights)))
        else:
            plan = plans.get(ident)
            if plan is None or not plan["sellable"]:
                raise _no_price(ask.line_ref, "unknown or unsellable trip plan")
            if not parts[ident]:
                raise _no_price(ask.line_ref, "the trip plan has no bookable components")
            check_room_types(ask, {r["room_type_id"] for r in parts[ident] if r["room_type_id"]})
            out.append(LineCost(kind=KIND_PLAN))
            for part in parts[ident]:
                on = ask.service_date + timedelta(days=int(part["day_no"]) - 1)
                if part["room_type_id"]:
                    room_units(no, ask, part["room_type_id"], start=on, nights=1)
                else:
                    service_unit(no, ask, part["service_id"], on=on, days=1)

    rates = await rates_in_force_many(conn, [RateAsk(u.room_type_id, u.service_id, u.on, u.pax) for u in units])
    for unit, rate in zip(units, rates, strict=True):
        ask = asks[unit.line]
        if rate is None:
            who = f" for {unit.pax} per room" if unit.room_type_id else ""
            raise _no_price(ask.line_ref, f"no rate in force on {unit.on.isoformat()}{who}")
        qty = unit.rooms if unit.room_type_id else _service_quantity(rate["unit"], ask, days=unit.days)
        out[unit.line].costs.append((Decimal(str(rate["net_amount"])) * qty, rate["currency"]))
        out[unit.line].rate_ids.append(rate["id"])
    return out


# ── Product listing ──────────────────────────────────────────────────────────

LIST_COMPONENTS_SQL = """
SELECT c.trip_plan_id::text AS trip_plan_id, c.day_no, c.hotel_room_type_id::text AS room_type_id,
       c.service_id::text AS service_id, s.service_type,
       coalesce(h.destination_id, s.destination_id)::text AS destination_id,
       c.created_at
FROM booking.trip_plan_components c
LEFT JOIN booking.hotel_room_types rt ON rt.id = c.hotel_room_type_id
LEFT JOIN booking.hotels h ON h.id = rt.hotel_id
LEFT JOIN booking.services s ON s.id = c.service_id
WHERE c.trip_plan_id = ANY($1::uuid[])
  AND coalesce(rt.status, s.status) = 'active' AND coalesce(h.status, 'active') = 'active'
ORDER BY c.trip_plan_id, c.day_no, coalesce(c.hotel_room_type_id, c.service_id)
"""


def component_out(row: Any) -> dict[str, Any]:
    if row["room_type_id"]:
        cid, kind = component_id(PREFIX_ROOM, row["room_type_id"]), KIND_ROOM
    else:
        cid, kind = component_id(PREFIX_SERVICE, row["service_id"]), row["service_type"]
    return {"component_id": cid, "kind": kind, "day_no": row["day_no"], "destination_id": row["destination_id"]}


async def components_by_plan(conn: asyncpg.Connection, plan_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {pid: [] for pid in plan_ids}
    if not plan_ids:
        return out
    for row in await conn.fetch(LIST_COMPONENTS_SQL, plan_ids):
        out[row["trip_plan_id"]].append(component_out(row))
    for pid, parts in out.items():
        if parts:
            whole = {"component_id": component_id(PREFIX_PLAN, pid), "kind": KIND_PLAN}
            parts.insert(0, {**whole, "day_no": None, "destination_id": None})
    return out
