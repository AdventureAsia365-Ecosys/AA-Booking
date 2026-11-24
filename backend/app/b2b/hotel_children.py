"""Hotel child prices of a B2B room line (AA-1153, bk-069 ``booking.hotel_child_policies``; split out of
:mod:`app.b2b.components`, file size limit).

A room line charges the DMC's hotel child price per night for each child its room mix does not hold
(:func:`extra_children`, the oldest first), by the child's age band, board (exact first, then any board) and basis
(:func:`child_bases`: ``sharing`` while the rooms' free places hold the child, ``extra_bed`` beyond them; the other
basis only when the DMC gives none on the chosen one). None found = no extra, said in ``LineCost.child_room``."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:  # the types only: components imports this module
    from app.b2b.components import LineAsk, LineCost, _Unit


def child_bases(extra: int, mix: list[tuple[int, int]], max_occupancy: int | None) -> tuple[str, ...]:
    """AA-1153: the basis of each child beyond the room mix (same order as :func:`extra_children`, the oldest first):
    ``sharing`` for as many as the rooms' free places hold (``max_occupancy`` less each room's occupancy; every child
    when the room type has no maximum), ``extra_bed`` for the oldest beyond them."""
    if not extra:
        return ()
    free = extra if max_occupancy is None else sum(max(max_occupancy - pax, 0) * n for pax, n in mix)
    beds = max(extra - free, 0)
    return (*(EXTRA_BED,) * beds, *(SHARING,) * (extra - beds))


def extra_children(ask: LineAsk, mix: list[tuple[int, int]]) -> tuple[int | None, ...]:
    """AA-1153: the ages of the children the room mix does not hold (the oldest first; None = age unknown). The
    rooms always hold the adults, so the travellers left over are children."""
    extra = max(ask.party - sum(pax * n for pax, n in mix), 0)
    if not extra:
        return ()
    unknown: list[int | None] = [None] * max(ask.child - len(ask.child_ages), 0)
    return tuple([*unknown, *sorted(ask.child_ages, reverse=True)][:extra])


CHILD_POLICIES_SQL = """
SELECT supplier_contract_id::text AS contract_id, hotel_id::text AS hotel_id, age_min, age_max, meal_plan, basis,
       net_amount::text AS net_amount
FROM booking.hotel_child_policies
WHERE (supplier_contract_id, hotel_id) IN (SELECT * FROM unnest($1::uuid[], $2::uuid[]))
"""


SHARING = "sharing"
EXTRA_BED = "extra_bed"


def child_policy(
    policies: list[dict[str, Any]], age: int | None, meal_plan: str | None, basis: str = SHARING
) -> dict[str, Any] | None:
    """The hotel child price of one child: its basis first (``sharing`` / ``extra_bed``, the other one only when the
    DMC gives none on it), within it its board exactly, else a row for any board; by its age band, or (age unknown)
    the band reaching the oldest age. Deterministic: ties go to the band starting oldest."""
    boards = (meal_plan, None) if meal_plan is not None else (None,)
    for wanted in (basis, EXTRA_BED if basis == SHARING else SHARING):
        for board in boards:
            rows = [p for p in policies if p["meal_plan"] == board and p.get("basis", SHARING) == wanted]
            if age is not None:
                rows = [p for p in rows if int(p["age_min"]) <= age <= int(p["age_max"])]
            rows.sort(key=lambda p: (-int(p["age_max"]), -int(p["age_min"])))
            if rows:
                return rows[0]
    return None


async def room_child_costs(
    conn: asyncpg.Connection,
    asks: list[LineAsk],
    out: list[LineCost],
    units: list[_Unit],
    rates: list[dict[str, Any] | None],
    costs: list[tuple[Decimal, str]],
    rooms: Mapping[str, Any],
) -> None:
    """AA-1153: add the hotel child price of every child beyond the room mix to the first unit of each night (one
    query for every contract and hotel involved, only when a line has such a child)."""
    wanted = [i for i, u in enumerate(units) if u.children and rates[i] is not None]
    if not wanted:
        return
    pairs = sorted({(rates[i]["contract_id"], rooms[units[i].room_type_id]["hotel_id"]) for i in wanted})  # type: ignore[index]
    rows = await conn.fetch(CHILD_POLICIES_SQL, [c for c, _ in pairs], [h for _, h in pairs])
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_pair.setdefault((row["contract_id"], row["hotel_id"]), []).append(dict(row))
    slot: dict[int, int] = {}  # the index of each unit's cost in its line's costs (units append in order)
    seen: dict[int, int] = {}
    for i, u in enumerate(units):
        slot[i] = seen.get(u.line, 0)
        seen[u.line] = slot[i] + 1
    for i in wanted:
        unit, rate = units[i], rates[i] or {}
        policies = by_pair.get((rate["contract_id"], rooms[unit.room_type_id or ""]["hotel_id"]), [])
        bases = unit.bases or (SHARING,) * len(unit.children)
        found = [
            child_policy(policies, age, rate.get("meal_plan"), basis)
            for age, basis in zip(unit.children, bases, strict=True)
        ]
        extra = sum((Decimal(str(f["net_amount"])) for f in found if f is not None), start=Decimal("0"))
        if out[unit.line].child_room is None:  # the first night says how the children were priced
            priced = sum(1 for f in found if f is not None)
            out[unit.line].child_room = {
                "children": len(found),
                "priced": priced,
                "unpriced": len(found) - priced,
                "extra_bed": sum(1 for b in bases if b == EXTRA_BED),
            }
        if extra:
            amount, currency = costs[i]
            costs[i] = (amount + extra, currency)
            out[unit.line].costs[slot[i]] = costs[i]
