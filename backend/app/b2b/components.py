"""Bookable components of B2B products and the net cost of one quote line (AA-972, bk-019).

Component ids are typed and opaque to the CRM (contract ``Component.component_id``):

- ``rt:<hotel_room_types.id>`` kind ``hotel_room``, priced per room night;
- ``sv:<services.id>`` kind = the service type (``transfer``, ``guide``, ``activity``, ``entrance``, ``meal``), priced
  per the unit of its rate;
- ``tp:<trip_plans.id>`` kind ``trip_plan``: the whole plan = the sum of its ``booking.trip_plan_components``.

Net cost = the supplier rate in force (bk-010: published contract, both validities cover the day; the newest
``valid_from`` wins, then a pax band, then the lowest net, ``app.supply.rates.rate_in_force``) for the party size
(adults + children; infants are neither counted against pax bands nor charged per person):

- room: one rate per night (``service_date`` + i for each of ``nights``, default 1), times the number of rooms (sum of
  ``rooms[].count``; default ``ceil(party / 2)``);
- service: ``person`` = x party, ``group`` / ``vehicle`` = x 1, ``day`` = x ``nights`` (default 1);
- trip plan: each component on ``service_date + day_no - 1``, a room component = one night for the line's rooms, a
  ``day`` service = one day.

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
from app.supply.rates import rate_in_force

PREFIX_ROOM = "rt"
PREFIX_SERVICE = "sv"
PREFIX_PLAN = "tp"
KIND_ROOM = "hotel_room"
KIND_PLAN = "trip_plan"
SERVICE_KINDS = frozenset({"transfer", "guide", "activity", "entrance", "meal"})


def component_id(prefix: str, ident: str) -> str:
    return f"{prefix}:{ident}"


def parse_component(value: str) -> tuple[str, str]:
    prefix, _, rest = value.partition(":")
    if prefix not in (PREFIX_ROOM, PREFIX_SERVICE, PREFIX_PLAN):
        raise ValueError(value)
    return prefix, str(uuid.UUID(rest))


@dataclass(frozen=True)
class LineAsk:
    line_ref: str
    component_id: str
    service_date: date
    nights: int | None
    adult: int
    child: int
    infant: int
    room_count: int | None  # None = default

    @property
    def party(self) -> int:
        return self.adult + self.child

    @property
    def rooms(self) -> int:
        return self.room_count if self.room_count else max(1, math.ceil(self.party / 2))


@dataclass
class LineCost:
    kind: str
    costs: list[tuple[Decimal, str]] = field(default_factory=list)  # (net amount, currency)
    rate_ids: list[str] = field(default_factory=list)


ROOM_SQL = """
SELECT rt.id::text AS id, rt.status, h.status AS hotel_status
FROM booking.hotel_room_types rt JOIN booking.hotels h ON h.id = rt.hotel_id
WHERE rt.id = $1::uuid
"""
SERVICE_SQL = "SELECT id::text AS id, status, service_type, unit FROM booking.services WHERE id = $1::uuid"
PLAN_SQL = "SELECT id::text AS id, sellable FROM booking.trip_plans WHERE id = $1::uuid"
PLAN_COMPONENTS_SQL = """
SELECT day_no, hotel_room_type_id::text AS room_type_id, service_id::text AS service_id
FROM booking.trip_plan_components WHERE trip_plan_id = $1::uuid
ORDER BY day_no, coalesce(hotel_room_type_id, service_id)
"""


def _no_price(line_ref: str, why: str) -> B2BError:
    return B2BError("invalid", f"line {line_ref}: {why}")


def _service_quantity(unit: str, ask: LineAsk, *, days: int) -> int:
    if unit == "person":
        return ask.party
    if unit == "day":
        return days
    return 1  # group, vehicle


async def _room(
    conn: asyncpg.Connection, ask: LineAsk, room_type_id: str, out: LineCost, *, nights: int, start: date
) -> None:
    row = await conn.fetchrow(ROOM_SQL, room_type_id)
    if row is None or row["status"] != "active" or row["hotel_status"] != "active":
        raise _no_price(ask.line_ref, "unknown or inactive room type")
    for i in range(nights):
        day = start + timedelta(days=i)
        rate = await rate_in_force(conn, room_type_id=room_type_id, on=day, pax=ask.party)
        if rate is None:
            raise _no_price(ask.line_ref, f"no rate in force on {day.isoformat()}")
        out.costs.append((Decimal(str(rate["net_amount"])) * ask.rooms, str(rate["currency"]).strip()))
        out.rate_ids.append(rate["id"])


async def _service(
    conn: asyncpg.Connection, ask: LineAsk, service_id: str, out: LineCost, *, days: int, on: date
) -> str:
    row = await conn.fetchrow(SERVICE_SQL, service_id)
    if row is None or row["status"] != "active" or row["service_type"] not in SERVICE_KINDS:
        raise _no_price(ask.line_ref, "unknown or inactive service")
    rate = await rate_in_force(conn, service_id=service_id, on=on, pax=ask.party)
    if rate is None:
        raise _no_price(ask.line_ref, f"no rate in force on {on.isoformat()}")
    qty = _service_quantity(rate["unit"], ask, days=days)
    out.costs.append((Decimal(str(rate["net_amount"])) * qty, str(rate["currency"]).strip()))
    out.rate_ids.append(rate["id"])
    return str(row["service_type"])


async def line_cost(conn: asyncpg.Connection, ask: LineAsk) -> LineCost:
    """The kind and net cost lines of one quote line, or B2BError 422."""
    try:
        prefix, ident = parse_component(ask.component_id)
    except ValueError:
        raise _no_price(ask.line_ref, "unknown component_id") from None
    nights = ask.nights or 1
    if prefix == PREFIX_ROOM:
        out = LineCost(kind=KIND_ROOM)
        await _room(conn, ask, ident, out, nights=nights, start=ask.service_date)
        return out
    if prefix == PREFIX_SERVICE:
        out = LineCost(kind="")
        out.kind = await _service(conn, ask, ident, out, days=nights, on=ask.service_date)
        return out
    plan = await conn.fetchrow(PLAN_SQL, ident)
    if plan is None or not plan["sellable"]:
        raise _no_price(ask.line_ref, "unknown or unsellable trip plan")
    parts = await conn.fetch(PLAN_COMPONENTS_SQL, ident)
    if not parts:
        raise _no_price(ask.line_ref, "the trip plan has no bookable components")
    out = LineCost(kind=KIND_PLAN)
    for part in parts:
        on = ask.service_date + timedelta(days=int(part["day_no"]) - 1)
        if part["room_type_id"]:
            await _room(conn, ask, part["room_type_id"], out, nights=1, start=on)
        else:
            await _service(conn, ask, part["service_id"], out, days=1, on=on)
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
