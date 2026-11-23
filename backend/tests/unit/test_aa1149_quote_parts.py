"""AA-1149 (T15): the sold snapshot of a tour line (``components.PartCost`` and :mod:`app.b2b.quote_parts`) and the
component bookings made from it (:mod:`app.ops.component_bookings`, pure parts)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest

from app.b2b import components, quote_parts
from app.b2b.components import LineAsk, LineCost, RoomAsk
from app.errors import Conflict, Invalid
from app.ops import component_bookings as cb
from app.ops import supplier_bookings as sb
from tests.unit.test_aa682_supplier_bookings_flows import AUDIT, SALES, SB_ID, SET_STATUS, booking_row, write_conn

DAY = date(2027, 3, 1)
ROOM, SERVICE, SUP = "rt-1", "sv-1", "sup-1"


def ask(adult: int = 3, rooms: tuple[RoomAsk, ...] = ()) -> LineAsk:
    return LineAsk("T", "tp:plan", DAY, None, adult, 0, 0, rooms)


def unit(part: int, day: int, *, room: bool, pax: int = 2, rooms: int = 1) -> components._Unit:
    if room:
        return components._Unit(0, ROOM, None, DAY, pax, rooms, 1, False, part, day)
    return components._Unit(0, None, SERVICE, DAY, pax, 0, 1, False, part, day)


def rate(rid: str, unit_kind: str = "room_night") -> dict[str, Any]:
    return {"id": rid, "unit": unit_kind}


ITEMS = {ROOM: {"supplier_id": SUP}, SERVICE: {"supplier_id": SUP}}


def test_a_room_part_sums_its_mix_and_keeps_the_occupancy_names() -> None:
    out = [LineCost(kind="trip_plan")]
    units = [unit(1, 1, room=True, pax=2, rooms=1), unit(1, 1, room=True, pax=1, rooms=1), unit(2, 1, room=False)]
    rates = [rate("r-d"), rate("r-s"), rate("r-t", "group")]
    costs = [(D("100"), "USD"), (D("70"), "USD"), (D("30"), "USD")]
    components._attach_parts(out, [ask()], units, rates, costs, ITEMS)
    room, service = out[0].parts
    assert (room.part_no, room.quantity, room.net, room.currency, room.rate_ids) == (
        1,
        2,
        D("170"),
        "USD",
        ["r-d", "r-s"],
    )
    assert room.rooms == [{"occupancy": "double", "count": 1}, {"occupancy": "single", "count": 1}]
    assert (service.quantity, service.net, service.rooms, service.supplier_id) == (1, D("30"), None, SUP)
    assert sum(p.net for p in out[0].parts if p.net is not None) == sum(c for c, _ in costs)


def test_a_part_whose_rates_differ_in_currency_keeps_no_net() -> None:
    out = [LineCost(kind="trip_plan")]
    units = [unit(1, 1, room=True, pax=2), unit(1, 1, room=True, pax=1)]
    components._attach_parts(out, [ask()], units, [rate("a"), rate("b")], [(D("1"), "USD"), (D("2"), "VND")], ITEMS)
    [part] = out[0].parts
    assert (part.net, part.currency) == (None, None)


def test_a_per_person_service_counts_the_party() -> None:
    out = [LineCost(kind="trip_plan")]
    components._attach_parts(
        out, [ask(adult=4)], [unit(1, 2, room=False)], [rate("p", "person")], [(D("40"), "USD")], ITEMS
    )
    assert (out[0].parts[0].quantity, out[0].parts[0].pax, out[0].parts[0].day_no) == (4, 4, 2)


def test_room_names_follow_the_ask_or_the_default_mix() -> None:
    assert components.room_names(ask(adult=5)) == [
        {"occupancy": "double", "count": 2},
        {"occupancy": "single", "count": 1},
    ]
    twins = ask(rooms=(RoomAsk("twin", 1), RoomAsk("single", 1), RoomAsk("twin", 1)))
    assert components.room_names(twins) == [{"occupancy": "twin", "count": 2}, {"occupancy": "single", "count": 1}]


def test_store_rows_carry_every_part_with_its_line_number() -> None:
    cost = LineCost(kind="trip_plan")
    cost.parts.append(
        components.PartCost(
            1, 1, DAY, ROOM, None, SUP, 2, 3, [{"occupancy": "double", "count": 2}], D("10.005"), "USD", ["r"]
        )
    )
    rows = quote_parts.rows("q-1", [4, 5], [LineCost(kind="hotel_room"), cost])
    assert len(rows) == 1
    assert rows[0][:3] == ("q-1", 5, 1) and rows[0][11] == D("10.01")
    assert json.loads(rows[0][10]) == [{"occupancy": "double", "count": 2}]


# ── Component bookings (pure parts) ─────────────────────────────────────────


def part(**kw: Any) -> dict[str, Any]:
    base = {
        "id": "qp-1",
        "line_no": 1,
        "part_no": 1,
        "day_no": 2,
        "service_date": DAY,
        "hotel_room_type_id": ROOM,
        "service_id": None,
        "supplier_id": SUP,
        "supplier_code": "SUP-000001",
        "supplier_name": "Red River",
        "supplier_status": "active",
        "quantity": 2,
        "nights": 1,
        "pax": 3,
        "rooms": json.dumps([{"occupancy": "double", "count": 1}, {"occupancy": "single", "count": 1}]),
        "net_amount": D("170.00"),
        "net_currency": "USD",
        "line_ref": "T",
        "line_status": "confirmed",
        "supply_label": "Hanoi Emerald · Deluxe",
        "service_type": None,
        "item_active": True,
        "line_pax": 3,
        "live_booking": None,
        "waive_reason": None,
    }
    return base | kw


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({}, None),
        ({"live_booking": "SB-000001"}, "booked"),
        ({"waive_reason": "No hotel needed at all here."}, "waived"),
        ({"line_status": "declined"}, "b2b_line_closed"),
        ({"line_status": "on_request"}, "line_not_confirmed"),
        ({"supplier_status": "pending"}, "supplier_not_active"),
        ({"item_active": False}, "item_not_active"),
        ({"item_active": None}, "item_not_active"),
    ],
)
def test_skip_reasons(changes: dict[str, Any], reason: str | None) -> None:
    assert cb.skip_reason(part(**changes)) == reason


def test_descriptions_name_the_room_mix_or_the_day() -> None:
    assert cb.description(part()) == "Hanoi Emerald · Deluxe · Double ×1 + Single ×1, 1 night"
    service = part(hotel_room_type_id=None, service_id=SERVICE, rooms=None, supply_label="Airport pick-up")
    assert cb.description(service) == "Airport pick-up · day 2 · 3 pax"
    assert cb._row(service)["nights"] is None and cb._row(part())["nights"] == 1


def test_a_line_reduced_after_the_sale_books_the_current_pax_and_says_so() -> None:
    """An agency removed 2 of 3 travellers before Generate: the booking takes 1 pax and a note; the quantity and the
    net stay as sold for staff to check (amendments CURRENT_PAX_SQL rule)."""
    assert cb._row(part())["notes"] is None and cb._row(part())["pax"] == 3
    reduced = part(line_pax=1)
    row = cb._row(reduced)
    assert (row["pax"], row["quantity"], row["net_amount"]) == (1, 2, D("170.00"))
    assert row["notes"] == (
        "The agency removed 2 traveller(s) from line T after the sale: check the quantity and the net"
        " with the supplier."
    )
    service = part(hotel_room_type_id=None, service_id=SERVICE, rooms=None, supply_label="Guide", line_pax=2)
    assert cb.description(service) == "Guide · day 2 · 2 pax"
    assert cb.booked_pax(part(line_pax=0)) == (1, 3)  # never below 1 (a fully cancelled line is closed)
    assert cb.booked_pax(part(line_pax=5)) == (3, 0)  # never more than the part as sold


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.inserted: list[tuple[Any, ...]] = []
        self.executed: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.rows if "b2b_quote_line_parts qp ON qp.quote_id" in sql else []

    async def fetchval(self, sql: str, *args: Any) -> str:
        self.inserted.append(args)
        return f"sb-{len(self.inserted)}"

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, *args))
        return "OK"


class _Staff:
    cis_user_id = "it-ops"


@pytest.mark.asyncio
async def test_generate_books_every_bookable_part_once() -> None:
    conn = _Conn(
        [
            part(),
            part(id="qp-2", live_booking="SB-1"),
            part(id="qp-3", supplier_status="inactive"),
            part(id="qp-4", item_active=False),
        ]
    )
    created = await cb.generate(conn, _Staff(), "case-1")  # type: ignore[arg-type]
    assert created == ["sb-1"]
    assert conn.inserted[0][:3] == ("case-1", "qp-1", SUP) and conn.inserted[0][9] == 2


@pytest.mark.asyncio
async def test_create_for_part_and_waive_refuse_what_is_closed() -> None:
    case = {"id": "case-1", "code": "TC-000001", "channel": "b2b"}
    checked: list[Any] = []

    async def check(_conn: Any, *args: Any) -> None:
        checked.append(args)

    conn = _Conn([part(supplier_status="inactive"), part(id="qp-2", live_booking="SB-9")])
    new_id = await cb.create_for_part(
        conn, _Staff(), case, {"quote_part_id": "qp-1", "supplier_id": "sup-2"}, check_supply=check
    )  # type: ignore[arg-type]
    # Another supplier: the part's room type (the old supplier's) is dropped; the booking keeps the description.
    assert new_id == "sb-1" and checked == [("sup-2", None, None)]
    assert conn.inserted[0][2:6] == ("sup-2", None, None, cb.description(part()))
    with pytest.raises(Conflict):
        await cb.create_for_part(conn, _Staff(), case, {"quote_part_id": "qp-2"}, check_supply=check)  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        await cb.create_for_part(conn, _Staff(), case, {"quote_part_id": "qp-x"}, check_supply=check)  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        await cb.waive(conn, _Staff(), case, "qp-1", reason="short")  # type: ignore[arg-type]
    with pytest.raises(Conflict):
        await cb.waive(conn, _Staff(), case, "qp-2", reason="The agency dropped this transfer.")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_another_supplier_names_its_own_item_or_a_description() -> None:
    """The part's own supplier keeps the part's room type; another one books its own service (checked against it) or
    only what staff write; a blank description falls back to the part's."""
    case = {"id": "case-1", "code": "TC-000001", "channel": "b2b"}
    checked: list[Any] = []

    async def check(_conn: Any, *args: Any) -> None:
        checked.append(args)

    conn = _Conn([part(supplier_status="inactive")])
    body = {"quote_part_id": "qp-1", "supplier_id": "sup-2", "service_id": "sv-9", "item_description": "  Car  "}
    await cb.create_for_part(conn, _Staff(), case, body, check_supply=check)  # type: ignore[arg-type]
    blank = {"quote_part_id": "qp-1", "supplier_id": "sup-2", "item_description": "   "}
    await cb.create_for_part(conn, _Staff(), case, blank, check_supply=check)  # type: ignore[arg-type]
    conn.rows = [part()]
    await cb.create_for_part(conn, _Staff(), case, {"quote_part_id": "qp-1"}, check_supply=check)  # type: ignore[arg-type]
    # The part's room type was deactivated since the sale: the same supplier books without it (a description only).
    conn.rows = [part(item_active=False)]
    await cb.create_for_part(conn, _Staff(), case, {"quote_part_id": "qp-1"}, check_supply=check)  # type: ignore[arg-type]
    assert checked == [("sup-2", None, "sv-9"), ("sup-2", None, None), (SUP, ROOM, None), (SUP, None, None)]
    assert [row[2:6] for row in conn.inserted] == [
        ("sup-2", None, "sv-9", "Car"),
        ("sup-2", None, None, cb.description(part())),
        (SUP, ROOM, None, cb.description(part())),
        (SUP, None, None, cb.description(part())),
    ]


@pytest.mark.asyncio
async def test_a_supplier_no_longer_active_is_not_requested() -> None:
    """AA-1149 review: a supplier deactivated after Generate (or while it ran) keeps its pending bookings; request()
    refuses them (409 supplier_inactive) until staff move the booking to another supplier or cancel it."""
    for status in ("inactive", "pending"):
        conn = write_conn(booking_row(supplier_status=status))
        with pytest.raises(Conflict) as exc:
            await sb.request(conn, SALES, SB_ID)
        assert exc.value.code == "supplier_inactive" and status in exc.value.message
        assert conn.sql(SET_STATUS) == [] and conn.sql(AUDIT) == []
