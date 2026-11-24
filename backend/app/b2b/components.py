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

**Board basis and several DMCs** (AA-1153, bk-069, :mod:`app.supply.rates`): a room night asks the line's board
(``LineAsk.meal_plan``), else the tour night's (``trip_plan_components.meal_plan``), else the pricing policy's default
board (``hotel_default_board``, seeded BB; the quote passes the value in force on its day); when several DMCs sell the
hotel the rate choice takes the asked board, the preferred DMC, then the lowest net in USD. **One DMC per room**: every
night and occupancy of one room type on one line (an ``rt:`` line, or the nights of one room type of a ``tp:`` line)
is priced by the DMC of its first night's first room; a unit another DMC was cheaper for is asked again pinned to that
DMC, and a unit that DMC has no rate for refuses the line (never a mix of two DMCs' nets on one booking). A tour part
names the supplier of the rate that priced it (the booking goes to that DMC). A night on HB / FB / AI whose day also
holds a ``meal`` service (a tour component of that day, or another ``sv:`` meal line of the quote on one of the line's
nights) is flagged ``LineCost.board`` ``double_dinner`` (a warning; the price is the rates'). **Children beyond the
room mix** (bk-069 ``hotel_child_policies``): when the rooms asked hold fewer travellers than the party, each extra
child (the oldest first, by ``LineAsk.child_ages``; no age = the band reaching the oldest age) pays the DMC's hotel
child price per night for its age band, board (exact board first, then any board) and **basis**: ``sharing`` while
the rooms' free places (``max_occupancy`` less the room's occupancy, over the mix; all of them when the room type
has no maximum) hold the child, ``extra_bed`` for the children beyond them (the oldest first); the other basis only
when the DMC gives no price on that one. None found = no extra, said in ``LineCost.child_room``.

**Children on a service line** (AA-1145, bk-060, B4, :mod:`app.supply.child_price`): on an ``sv:`` line whose rate is
per person, adults pay the adult rate and each child the supplier's child rate, else the contract's child %, else the
policy's child % of the service type (``child_pct_by_type``), else the adult price with an age note
(``LineCost.child``); an infant pays the supplier's infant rate when there is one, else nothing. A ``tp:`` line keeps
the pricing rule's child % on the sell price (``app.b2b.quotes.pax_sell_usd``), so its components price the party
alike as before. **Supply asks** (``LineCost.supply``): every night and component of the line, for the availability
engine (:mod:`app.supply.restrictions`, a stop-sale puts the line on request in the quote).

Bounded work (AA-972 review): items, plans and their components are read with one query each for the whole quote,
every rate with one batched query (``rates_in_force_many``), and a quote needing more than :data:`MAX_RATE_LOOKUPS`
rate lookups is refused before any is run (422 ``invalid``).

A line with no rate in force (or an inactive item) is refused with 422 ``invalid`` naming the line; nothing is ever
priced at a guessed or zero net. Only ids, statuses, units and net amounts are read here: no content (O7).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from app.b2b.errors import B2BError
from app.b2b.hotel_children import child_bases, extra_children, room_child_costs
from app.pricing import policy_values
from app.supply import addon_rules, child_price
from app.supply import rates as supply_rates
from app.supply.rates import RateAsk, rates_in_force_many
from app.supply.restrictions import SupplyAsk

PREFIX_ROOM = "rt"
PREFIX_SERVICE = "sv"
PREFIX_PLAN = "tp"
KIND_ROOM = "hotel_room"
KIND_PLAN = "trip_plan"
SERVICE_KINDS = frozenset({"transfer", "guide", "activity", "entrance", "meal"})
OCCUPANCY_PAX = {"single": 1, "double": 2, "twin": 2, "triple": 3}
FAMILY = "family"
MAX_RATE_LOOKUPS = 1000  # one quote's budget: 200 lines x 5 nights, or a 5-line quote of 200-component plans


def ask_from_request(line: Any) -> LineAsk:
    """The pricing ask of one ``QuoteLineRequest`` (the agency API and the staff desk build it alike; AA-1153: the
    board and the children's ages)."""
    return LineAsk(
        line_ref=line.line_ref,
        component_id=line.component_id,
        service_date=line.service_date,
        nights=line.nights,
        adult=line.pax.adult,
        child=line.pax.child,
        infant=line.pax.infant,
        rooms=tuple(RoomAsk(r.occupancy, r.count, r.room_type_id) for r in line.rooms),
        meal_plan=line.meal_plan,
        child_ages=tuple(line.child_ages or ()),
    )


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
    meal_plan: str | None = None  # AA-1153 (T9): the board asked for every room night of the line; None = the default
    child_ages: tuple[int, ...] = ()  # AA-1153: the children's ages on the service date (optional, one per child)

    @property
    def party(self) -> int:
        return self.adult + self.child


@dataclass(frozen=True)
class PartCost:
    """One component of a ``tp:`` line as priced (AA-1149, bk-065 ``b2b_quote_line_parts``): the sold snapshot."""

    part_no: int
    day_no: int
    service_date: date
    room_type_id: str | None
    service_id: str | None
    supplier_id: str
    quantity: int  # rooms of the room night, or the quantity of the service's rate unit
    pax: int
    rooms: list[dict[str, Any]] | None  # the room mix [{"occupancy", "count"}] of a room part
    net: Decimal | None  # None (with currency) when the part's rates are in more than one currency
    currency: str | None
    rate_ids: list[str]


@dataclass
class LineCost:
    kind: str
    costs: list[tuple[Decimal, str]] = field(default_factory=list)  # (net amount, currency)
    rate_ids: list[str] = field(default_factory=list)
    child: dict[str, Any] | None = None  # B4: how a child of an sv: line was priced (the quote's price note)
    board: dict[str, Any] | None = None  # AA-1153 (T9): the board asked and the double-dinner warnings
    child_room: dict[str, Any] | None = None  # AA-1153: children beyond the room mix and how they were priced
    supply: list[SupplyAsk] = field(default_factory=list)  # the availability engine's asks (bk-060)
    parts: list[PartCost] = field(default_factory=list)  # AA-1149: the components of a tp: line (bk-065)

    def notes(self, supply_note: Any) -> dict[str, Any]:
        """The price-note parts this cost brings (stop-sale dates, B4 child basis, AA-1153 board and hotel children);
        only the ones it has."""
        parts = {"supply": supply_note, "child": self.child, "board": self.board, "child_room": self.child_room}
        return {k: v for k, v in parts.items() if v}


def board_hash(line: dict[str, Any]) -> dict[str, Any]:
    """AA-1153: the board and the children's ages a quote line asked, for its price hash; only when sent (any other
    line hashes as before)."""
    out: dict[str, Any] = {}
    if line.get("meal_plan"):
        out["meal_plan"] = line["meal_plan"]
    if line.get("child_ages"):
        out["child_ages"] = list(line["child_ages"])
    return out


PLANS_SQL = "SELECT id::text AS id, sellable FROM booking.trip_plans WHERE id = ANY($1::uuid[])"
PLAN_COMPONENTS_SQL = """
SELECT trip_plan_id::text AS trip_plan_id, day_no, hotel_room_type_id::text AS room_type_id,
       service_id::text AS service_id, meal_plan
FROM booking.trip_plan_components WHERE trip_plan_id = ANY($1::uuid[])
ORDER BY trip_plan_id, day_no, coalesce(hotel_room_type_id, service_id)
"""
ROOMS_SQL = """
SELECT rt.id::text AS id, rt.status, rt.max_occupancy, h.status AS hotel_status, h.supplier_id::text AS supplier_id,
       h.id::text AS hotel_id
FROM booking.hotel_room_types rt JOIN booking.hotels h ON h.id = rt.hotel_id
WHERE rt.id = ANY($1::uuid[])
"""
SERVICES_SQL = (
    "SELECT id::text AS id, status, service_type, supplier_id::text AS supplier_id FROM booking.services"
    " WHERE id = ANY($1::uuid[])"
)


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
    split: bool = False  # an sv: line with children or infants: their rates are looked up too (B4)
    part: int = 0  # AA-1149: the part number of a tp: line's component (1-based; 0 = not a tp: component)
    day_no: int = 0  # the component's day (tp: only)
    meal_plan: str | None = None  # AA-1153: the board asked (rooms only)
    children: tuple[int | None, ...] = ()  # AA-1153: ages of the children beyond the room mix (first unit of a night)
    bases: tuple[str, ...] = ()  # AA-1153: the basis of each of those children (sharing / extra_bed)


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


CONTRACT_CHILD_SQL = """
SELECT supplier_contract_id::text AS contract_id, child_pct FROM booking.supplier_contract_terms
WHERE supplier_contract_id = ANY($1::uuid[]) AND child_pct IS NOT NULL
"""


async def price_lines(
    conn: asyncpg.Connection,
    asks: list[LineAsk],
    *,
    child_pct_by_type: Mapping[str, Decimal] | None = None,
    child_age_max: int | None = None,
    default_board: str | None = None,
) -> list[LineCost]:
    """The kind and net cost lines of every quote line (same order), or B2BError 422.

    A fixed number of queries whatever the request: plans, plan components, room types, services, then one batched
    rate query (plus one pinned rate query when a room line's units were cheapest at different DMCs, and, only when an
    sv: line has children whose contract may set a child %, one terms query). The lookup budget is checked while the
    lookups are planned, before the rate query runs. ``child_pct_by_type`` (the policy's child % per service type) and
    ``child_age_max`` feed the B4 rule of an sv: line with children. ``default_board`` (bk-069 T9): the policy's
    default board in force on the quote day; read from the policy here when None and a room night needs it (503
    ``unavailable`` without one).
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

    def room_units(
        no: int,
        ask: LineAsk,
        room_type_id: str,
        *,
        start: date,
        nights: int,
        part: tuple[int, ...] = (),
        meal_plan: str | None = None,
    ) -> None:
        row = rooms.get(room_type_id)
        if row is None or row["status"] != "active" or row["hotel_status"] != "active":
            raise _no_price(ask.line_ref, "unknown or inactive room type")
        mix = room_mix(ask, row["max_occupancy"])
        extra = extra_children(ask, mix)
        bases = child_bases(len(extra), mix, row["max_occupancy"])
        budget.take(nights * len(mix))
        for i in range(nights):
            for n, (pax, count) in enumerate(mix):
                units.append(
                    _Unit(
                        no,
                        room_type_id,
                        None,
                        start + timedelta(days=i),
                        pax,
                        count,
                        1,
                        False,
                        *part,
                        meal_plan=meal_plan,
                        children=extra if n == 0 else (),
                        bases=bases if n == 0 else (),
                    )
                )

    def service_unit(
        no: int, ask: LineAsk, service_id: str, *, on: date, days: int, split: bool = False, part: tuple[int, ...] = ()
    ) -> str:
        row = services.get(service_id)
        if row is None or row["status"] != "active" or row["service_type"] not in SERVICE_KINDS:
            raise _no_price(ask.line_ref, "unknown or inactive service")
        budget.take(1 + 2 * int(split))  # B4: a split unit also looks up the child and infant rates
        units.append(_Unit(no, None, service_id, on, ask.party, 0, days, split, *part))
        return str(row["service_type"])

    for no, (ask, (prefix, ident)) in enumerate(zip(asks, parsed, strict=True)):
        nights = ask.nights or 1
        if prefix == PREFIX_ROOM:
            check_room_types(ask, {ident})
            out.append(LineCost(kind=KIND_ROOM))
            room_units(no, ask, ident, start=ask.service_date, nights=nights, meal_plan=ask.meal_plan)
        elif prefix == PREFIX_SERVICE:
            check_room_types(ask, set())
            split = ask.child + ask.infant > 0
            out.append(LineCost(kind=service_unit(no, ask, ident, on=ask.service_date, days=nights, split=split)))
        else:
            plan = plans.get(ident)
            if plan is None or not plan["sellable"]:
                raise _no_price(ask.line_ref, "unknown or unsellable trip plan")
            if not parts[ident]:
                raise _no_price(ask.line_ref, "the trip plan has no bookable components")
            check_room_types(ask, {r["room_type_id"] for r in parts[ident] if r["room_type_id"]})
            out.append(LineCost(kind=KIND_PLAN))
            for part_no, part in enumerate(parts[ident], start=1):
                on = ask.service_date + timedelta(days=int(part["day_no"]) - 1)
                tag = (part_no, int(part["day_no"]))
                if part["room_type_id"]:
                    board = ask.meal_plan or part.get("meal_plan")
                    room_units(no, ask, part["room_type_id"], start=on, nights=1, part=tag, meal_plan=board)
                else:
                    service_unit(no, ask, part["service_id"], on=on, days=1, part=tag)

    rates, by_split, contract_pct, offer_pct, pinned = await _lookup(conn, asks, units, default_board)
    unit_costs: list[tuple[Decimal, str]] = []
    for i, (unit, rate) in enumerate(zip(units, rates, strict=True)):
        ask = asks[unit.line]
        if rate is None:
            who = f" for {unit.pax} per room" if unit.room_type_id else ""
            dmc = " from the DMC that prices the first night of this room" if i in pinned else ""
            raise _no_price(ask.line_ref, f"no rate in force on {unit.on.isoformat()}{who}{dmc}")
        line = out[unit.line]
        line.rate_ids.append(rate["id"])
        line.supply.append(_supply_ask(unit, ask, rate))
        policy_pct = (child_pct_by_type or {}).get(str(services.get(unit.service_id or "", {}).get("service_type")))
        cost = _unit_cost(
            unit,
            ask,
            line,
            rate,
            by_split.get(id(unit)),
            offer_pct=offer_pct.get(unit.service_id or ""),
            contract_pct=contract_pct.get(rate["contract_id"]),
            policy_pct=policy_pct,
            child_age_max=child_age_max,
        )
        line.costs.append(cost)
        unit_costs.append(cost)
    await room_child_costs(conn, asks, out, units, rates, unit_costs, rooms)
    _attach_parts(out, asks, units, rates, unit_costs, {**rooms, **services})
    _board_notes(asks, parsed, out, units, rates, parts, services)
    return out


async def default_board_b2b(conn: asyncpg.Connection, on: date | None = None) -> str:
    """bk-069 (T9): the policy's default board in force on ``on`` (today); missing = AA-side data missing (503
    ``unavailable``)."""
    try:
        return await supply_rates.default_board(conn, on=on)
    except policy_values.PolicyUnavailable:
        raise B2BError("unavailable", "pricing policy is not available") from None


DINNER_BOARDS = frozenset({"HB", "FB", "AI"})


def _board_notes(
    asks: list[LineAsk],
    parsed: list[tuple[str, str]],
    out: list[LineCost],
    units: list[_Unit],
    rates: list[dict[str, Any] | None],
    parts: Mapping[str, list[Any]],
    services: Mapping[str, Any],
) -> None:
    """AA-1153 (T9): the board of each line with room nights and a ``double_dinner`` warning per night on HB / FB /
    AI whose day also holds a meal service (a tour component of that day, or another sv: meal line on that date)."""
    meal_dates = {
        asks[n].service_date
        for n, (prefix, ident) in enumerate(parsed)
        if prefix == PREFIX_SERVICE and services.get(ident, {}).get("service_type") == "meal"
    }
    for no, (prefix, ident) in enumerate(parsed):
        boards = {}
        for unit, rate in zip(units, rates, strict=True):
            if unit.line == no and unit.room_type_id and rate is not None:
                boards[(unit.on, unit.day_no)] = rate.get("meal_plan") or unit.meal_plan
        if not boards:
            continue
        meal_days = (
            {
                int(c["day_no"])
                for c in parts.get(ident, [])
                if c["service_id"] and services.get(c["service_id"], {}).get("service_type") == "meal"
            }
            if prefix == PREFIX_PLAN
            else set()
        )
        warnings = [
            {"code": "double_dinner", "date": on.isoformat(), "meal_plan": board}
            for (on, day_no), board in sorted(boards.items())
            if board in DINNER_BOARDS and (day_no in meal_days or on in meal_dates)
        ]
        asked = asks[no].meal_plan
        if asked or warnings:
            out[no].board = {"meal_plan": asked, **({"warnings": warnings} if warnings else {})}


def room_names(ask: LineAsk) -> list[dict[str, Any]]:
    """The room mix of a line as the agency asked it (occupancy names kept: twin is not double), or the default mix
    ``room_mix`` prices (``party // 2`` doubles plus one single when the party is odd)."""
    if not ask.rooms:
        default = [("double", ask.party // 2), ("single", ask.party % 2)]
        return [{"occupancy": name, "count": n} for name, n in default if n]
    counts: dict[str, int] = {}
    for room in ask.rooms:
        counts[room.occupancy] = counts.get(room.occupancy, 0) + room.count
    return [{"occupancy": name, "count": n} for name, n in counts.items()]


def _attach_parts(
    out: list[LineCost],
    asks: list[LineAsk],
    units: list[_Unit],
    rates: list[dict[str, Any] | None],
    costs: list[tuple[Decimal, str]],
    items: Mapping[str, Any],
) -> None:
    """AA-1149 (bk-065): one :class:`PartCost` per component of each tp: line, from the units it was priced on (a
    room night = one unit per occupancy of the mix, a service = one unit). A part whose units' rates are in more than
    one currency keeps no net (staff enter it on the booking); never a guessed net."""
    grouped: dict[tuple[int, int], list[int]] = {}
    for i, unit in enumerate(units):
        if unit.part:
            grouped.setdefault((unit.line, unit.part), []).append(i)
    for (line_no, part_no), idx in grouped.items():
        first, ask = units[idx[0]], asks[line_no]
        currencies = {costs[i][1] for i in idx}
        one = len(currencies) == 1
        found = [rate for i in idx if (rate := rates[i]) is not None]  # every unit has a rate here (else 422 above)
        if first.room_type_id:
            quantity, mix = sum(units[i].rooms for i in idx), room_names(ask)
        else:
            quantity, mix = _service_quantity(str(found[0]["unit"]), ask, days=1), None
        out[line_no].parts.append(
            PartCost(
                part_no=part_no,
                day_no=first.day_no,
                service_date=first.on,
                room_type_id=first.room_type_id,
                service_id=first.service_id,
                # AA-1153: the DMC of the rate that priced the part (a hotel may be sold by several).
                supplier_id=str(
                    found[0].get("supplier_id") or items[first.room_type_id or first.service_id or ""]["supplier_id"]
                ),
                quantity=max(1, min(quantity, 999)),
                pax=max(1, min(ask.party, 120)),
                rooms=mix,
                net=sum((costs[i][0] for i in idx), Decimal("0")) if one else None,
                currency=next(iter(currencies)) if one else None,
                rate_ids=list(dict.fromkeys(str(r["id"]) for r in found)),
            )
        )


def _unit_cost(
    unit: _Unit,
    ask: LineAsk,
    line: LineCost,
    rate: dict[str, Any],
    split: tuple[dict[str, Any] | None, dict[str, Any] | None] | None,
    *,
    offer_pct: Decimal | None = None,
    contract_pct: Decimal | None,
    policy_pct: Decimal | None,
    child_age_max: int | None,
) -> tuple[Decimal, str]:
    """A room night x its rooms, a service x its unit's quantity (the whole party alike), or, for a per-person service
    of an sv: line with children or infants, the B4 split (:func:`_split_cost`)."""
    if split is not None and rate["unit"] == "person":
        child_rate, infant_rate = split
        return _split_cost(
            ask,
            line,
            rate,
            child_rate,
            infant_rate,
            offer_pct=offer_pct,
            contract_pct=contract_pct,
            policy_pct=policy_pct,
            child_age_max=child_age_max,
        )
    qty = unit.rooms if unit.room_type_id else _service_quantity(rate["unit"], ask, days=unit.days)
    return Decimal(str(rate["net_amount"])) * qty, rate["currency"]


def _supply_ask(unit: _Unit, ask: LineAsk, rate: dict[str, Any]) -> SupplyAsk:
    """The availability engine's ask of one rate lookup: a room night (its room count) or a service day (the party),
    under the supplier of the rate that priced it (AA-1153 part 2: a DMC's own allotment and stop-sales)."""
    dmc = str(rate["supplier_id"]) if rate.get("supplier_id") else None
    if unit.room_type_id:
        return SupplyAsk(unit.room_type_id, None, unit.on, ask.party, units=unit.rooms, supplier_id=dmc)
    return SupplyAsk(None, unit.service_id, unit.on, ask.party, units=max(ask.party, 1), supplier_id=dmc)


Split = dict[int, tuple[dict[str, Any] | None, dict[str, Any] | None]]


async def _lookup(
    conn: asyncpg.Connection, asks: list[LineAsk], units: list[_Unit], board: str | None
) -> tuple[list[dict[str, Any] | None], Split, dict[str, Decimal], dict[str, Decimal], set[int]]:
    """One batched rate query for every unit (plus, B4, the child and infant rates of a split unit's service and day,
    same party band), the room units pinned to their room's DMC (:func:`_one_dmc_per_room`), and the child % of the
    contracts and of the published add-on offers (AA-1151, bk-068) a child still needs (only when one does). The last
    item: the indexes of the units asked again pinned."""
    if board is None and any(u.room_type_id and u.meal_plan is None for u in units):
        board = await default_board_b2b(conn)
    lookups = [RateAsk(u.room_type_id, u.service_id, u.on, u.pax, "adult", u.meal_plan) for u in units]
    split_units = [u for u in units if u.split]
    for u in split_units:
        lookups += [
            RateAsk(None, u.service_id, u.on, u.pax, "child"),
            RateAsk(None, u.service_id, u.on, u.pax, "infant"),
        ]
    found = await rates_in_force_many(conn, lookups, board=board)
    rates, extra = found[: len(units)], iter(found[len(units) :])
    pinned = await _one_dmc_per_room(conn, units, rates, board)
    by_split: Split = {id(u): (next(extra), next(extra)) for u in split_units}
    need_terms = sorted(
        {
            r["contract_id"]
            for u, r in zip(units, rates, strict=True)
            if u.split and r is not None and r["unit"] == "person" and asks[u.line].child and by_split[id(u)][0] is None
        }
    )
    if not need_terms:
        return rates, by_split, {}, {}, pinned
    rows = await conn.fetch(CONTRACT_CHILD_SQL, need_terms)
    services = {
        u.service_id
        for u, r in zip(units, rates, strict=True)
        if u.split and u.service_id and r is not None and asks[u.line].child and by_split[id(u)][0] is None
    }
    offers = await addon_rules.child_pcts(conn, services)
    return rates, by_split, {r["contract_id"]: Decimal(str(r["child_pct"])) for r in rows}, offers, pinned


async def _one_dmc_per_room(
    conn: asyncpg.Connection, units: list[_Unit], rates: list[dict[str, Any] | None], board: str | None
) -> set[int]:
    """AA-1153 review: one DMC per room of a line. The units of one room type on one line take the DMC of the first
    of them (the first night's first room); a unit whose best rate came from another DMC is asked again pinned to it
    (one more batched query, only when needed) and keeps no rate when that DMC has none (the line is then refused).
    ``rates`` is updated in place; returns the indexes of the units asked again."""
    dmc: dict[tuple[int, str], str] = {}
    redo: list[int] = []
    for i, (unit, rate) in enumerate(zip(units, rates, strict=True)):
        if not unit.room_type_id or rate is None:
            continue
        chosen = dmc.setdefault((unit.line, unit.room_type_id), str(rate["supplier_id"]))
        if str(rate["supplier_id"]) != chosen:
            redo.append(i)
    if not redo:
        return set()
    again = [
        RateAsk(u.room_type_id, None, u.on, u.pax, "adult", u.meal_plan, dmc[(u.line, u.room_type_id or "")])
        for u in (units[i] for i in redo)
    ]
    for i, rate in zip(redo, await rates_in_force_many(conn, again, board=board), strict=True):
        rates[i] = rate
    return set(redo)


def _split_cost(
    ask: LineAsk,
    line: LineCost,
    adult: dict[str, Any],
    child_rate: dict[str, Any] | None,
    infant_rate: dict[str, Any] | None,
    *,
    offer_pct: Decimal | None = None,
    contract_pct: Decimal | None,
    policy_pct: Decimal | None,
    child_age_max: int | None,
) -> tuple[Decimal, str]:
    """B4: one per-person service of an sv: line priced by age band (the child / infant rates must be in the adult
    rate's currency to be used; a contract's rates always are)."""
    currency = adult["currency"]
    adult_net = Decimal(str(adult["net_amount"]))
    total = adult_net * ask.adult
    if ask.child:
        rate = child_rate if child_rate is not None and child_rate["currency"] == currency else None
        priced = child_price.child_net(
            adult_net,
            child_rate_net=Decimal(str(rate["net_amount"])) if rate else None,
            child_rate_age_max=rate.get("age_max") if rate else None,
            offer_pct=offer_pct,
            contract_pct=contract_pct,
            policy_pct=policy_pct,
            child_age_max=child_age_max,
        )
        total += priced.net * ask.child
        line.child = priced.note()
        if rate:
            line.rate_ids.append(rate["id"])
    if ask.infant and infant_rate is not None and infant_rate["currency"] == currency:
        total += Decimal(str(infant_rate["net_amount"])) * ask.infant
        line.rate_ids.append(infant_rate["id"])
    return total, currency


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
