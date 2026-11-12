"""AA-1091 price setup workbook without a database: the parser (columns by name, cell problems per sheet, row and
column, the bounds), the checks that need no database, the price rows written, the merge with the computed rows,
the template round trip, the matching and the dry run with a scripted connection. Invented tours and codes only."""

from __future__ import annotations

import io
from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from openpyxl import Workbook

from app.auth.staff import Staff
from app.b2b import departures, plan_components
from app.catalog import cost_sheet, price_readiness, price_workbench, trip_plan_prices
from app.catalog import price_import as pi
from app.errors import Invalid
from tests.unit.fakes import ScriptedConn

TODAY = date(2027, 3, 1)
KEY = "aa109100-0000-4000-8000-000000000001"
OTHER = "aa109100-0000-4000-8000-000000000002"
STAFF = Staff("it-1091", None, "it-1091", "contracting", True, "reviewer")


def _book(sheets: dict[str, list[list[Any]]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _codes(sheets: pi.Sheets) -> set[tuple[str, int | None, str | None, str]]:
    return {(p.sheet, p.row, p.column, p.code) for p in sheets.problems}


def test_parse_reads_every_sheet_by_header_name() -> None:
    data = _book(
        {
            "Tours": [["Sellable", "Tour key"], ["yes", KEY], ["", OTHER]],
            "components": [["tour key", "day", "service code"], [KEY, 2, "svc-000101"]],
            "Prices": [
                ["Tour key", "Valid from", "Valid to", "Price per person", "Peak", "Currency"],
                [KEY, "2027-04-01", "01/12/2027", "1,200.50", "no", "usd"],
            ],
            "Departures": [["Tour key", "Start date", "Capacity", "Auto confirm"], [KEY, date(2027, 5, 1), 12, "n"]],
            "Notes": [["anything"]],
            "Read me": [["text"]],
        }
    )
    sheets = pi.parse(data)
    assert sheets.problems == [] and sheets.ignored_sheets == ["Notes"]
    assert [t["sellable"] for t in sheets.tours] == [True, None]
    assert sheets.components[0] | {} == {
        "_row": 2,
        "tour_key": KEY,
        "day_no": 2,
        "hotel_code": None,
        "room_type": None,
        "service_code": "SVC-000101",
    }
    price = sheets.prices[0]
    assert (price["valid_from"], price["valid_to"], price["amount"], price["currency"], price["peak"]) == (
        date(2027, 4, 1),
        date(2027, 12, 1),
        D("1200.50"),
        "USD",
        False,
    )
    assert sheets.departures[0]["auto_confirm"] is False
    assert sheets.keys() == [KEY, OTHER]


def test_parse_collects_cell_and_shape_problems() -> None:
    data = _book(
        {
            "Tours": [["Title"], ["no key column"]],
            "Components": [["Tour key", "Day"], [KEY, 400], [None, 1]],
            "Prices": [
                ["Tour key", "Valid from", "Valid to", "Price per person", "Peak"],
                [KEY, "soon", "2027-05-01", "12.345", "maybe"],
                [KEY, "2027-04-01", "2027-05-01", 99999999999, None],
            ],
            "Departures": [["Tour key", "Start date", "Capacity", "Min to operate"], [KEY, "2027-05-01", "ten", -1]],
        }
    )
    codes = _codes(pi.parse(data))
    assert ("Tours", 1, "Tour key", "missing_column") in codes
    assert ("Components", 2, "Day", "invalid_value") in codes
    assert ("Components", 3, "Tour key", "required") in codes
    assert ("Prices", 2, "Valid from", "invalid_value") in codes
    assert ("Prices", 2, "Price per person", "invalid_value") in codes  # 3 decimals
    assert ("Prices", 2, "Peak", "invalid_value") in codes
    assert ("Prices", 3, "Price per person", "invalid_value") in codes  # too high
    assert ("Departures", 2, "Capacity", "invalid_value") in codes
    assert ("Departures", 2, "Min to operate", "invalid_value") in codes
    assert not any(c[3] == "required" and c[2] == "Capacity" for c in codes)  # a bad cell is not also "required"


def test_parse_bounds_and_empty() -> None:
    assert _codes(pi.parse(pi.template())) == {("Tours", None, None, "no_rows")}
    with pytest.raises(Invalid) as exc:
        pi.parse(b"not a workbook")
    assert exc.value.code == "rate_sheet_not_xlsx"
    many = _book({"Tours": [["Tour key"]] + [[f"key-{i}"] for i in range(pi.MAX_TOURS + 1)]})
    with pytest.raises(Invalid) as exc:
        pi.parse(many)
    assert exc.value.code == "price_import_too_many_tours"


def test_parse_row_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi, "MAX_ROWS", 2)
    with pytest.raises(Invalid) as exc:
        pi.parse(_book({"Tours": [["Tour key"], ["a"], ["b"], ["c"]]}))
    assert exc.value.code == "price_import_too_many_rows"


def _price(**over: Any) -> dict[str, Any]:
    row = {
        "_row": 2,
        "tour_key": KEY,
        "channel": None,
        "season": None,
        "peak": None,
        "valid_from": TODAY,
        "valid_to": TODAY + timedelta(days=100),
        "pax_min": None,
        "pax_max": None,
        "currency": None,
        "amount": D("400"),
    }
    return {**row, **over}


def test_check_shapes() -> None:
    sheets = pi.Sheets(
        tours=[{"_row": 2, "tour_key": KEY}, {"_row": 3, "tour_key": KEY}],
        components=[
            {"_row": 2, "tour_key": KEY, "day_no": 1, "hotel_code": "HTL-1", "room_type": None, "service_code": None},
            {"_row": 3, "tour_key": KEY, "day_no": 1, "hotel_code": None, "room_type": None, "service_code": None},
            {"_row": 4, "tour_key": KEY, "day_no": 1, "hotel_code": None, "room_type": None, "service_code": "SVC-1"},
            {"_row": 5, "tour_key": KEY, "day_no": 1, "hotel_code": None, "room_type": None, "service_code": "SVC-1"},
        ],
        prices=[
            _price(),
            _price(_row=3, pax_min=2),  # overlaps row 2
            _price(_row=4, channel="b2c_tailor_made", currency="THB", valid_to=TODAY - timedelta(days=1)),
            _price(
                _row=5,
                pax_min=5,
                pax_max=3,
                amount=D("0"),
                valid_from=TODAY + timedelta(days=200),
                valid_to=TODAY + timedelta(days=300),
            ),
        ],
        departures=[
            {"_row": 2, "tour_key": KEY, "start_date": TODAY, "capacity": 4, "min_to_operate": 6, "status": "open"},
        ],
    )
    pi.check_shapes(sheets, TODAY)
    codes = {(p.sheet, p.row, p.code) for p in sheets.problems}
    assert codes == {
        ("Tours", 3, "duplicate_tour"),
        ("Components", 2, "required"),
        ("Components", 3, "invalid_component"),
        ("Components", 5, "duplicate_component"),
        ("Prices", 3, "overlapping_prices"),
        ("Prices", 4, "invalid_value"),
        ("Prices", 4, "invalid_currency"),
        ("Prices", 4, "invalid_validity"),
        ("Prices", 5, "invalid_pax_band"),
        ("Prices", 5, "invalid_value"),
        ("Departures", 2, "invalid_start_date"),
        ("Departures", 2, "invalid_status"),
        ("Departures", 2, "invalid_min_to_operate"),
    }
    assert sheets.prices[0]["channel"] == "b2b" and sheets.prices[0]["currency"] == "USD"


def test_hand_row_and_merge() -> None:
    hand = pi.hand_row(
        _price(channel="b2b", currency="USD", pax_min=1, season="x" * 99, child_amount=D("1")), D("0.25")
    )
    assert hand["rrp_amount"] == D("500") and hand["computed"] is False and hand["net_amount"] is None
    assert len(hand["season"]) == cost_sheet.SEASON_MAX and hand["child_amount"] == D("1")
    assert pi.hand_row(_price(channel="fixed_departure", currency="USD", pax_min=1), D("0.25"))["rrp_amount"] is None
    computed = {
        "position": 1,
        "channel": "b2b",
        "season": None,
        "peak": False,
        "valid_from": TODAY,
        "valid_to": TODAY + timedelta(days=30),
        "pax_min": 1,
        "pax_max": 1,
        "currency": "USD ",
        "amount": "300.00",
        "net_amount": "200.00",
        "net_currency": "USD",
        "effective_margin": "0.3",
        "priced_on": TODAY,
        "overridden": False,
        "computed": True,
        "rrp_amount": "375.00",
    }
    later = {**computed, "valid_from": TODAY + timedelta(days=400), "valid_to": TODAY + timedelta(days=500)}
    staff_row = {**computed, "computed": False, "net_amount": None, "net_currency": None, "effective_margin": None}
    pricing = cost_sheet.Pricing(TODAY, {}, {})
    final, dropped = pi.merge_prices([computed, later, staff_row], [hand], pricing)
    assert dropped == 1  # the computed row under the hand row; the old staff row is replaced by the file
    assert [r["position"] for r in final] == [1, 2] and final[0]["computed"] is False and final[1]["computed"] is True


def test_template_round_trip() -> None:
    data = pi.build_workbook(
        [{"tour_key": KEY, "title": "Invented Ridge Trek", "country": "Vietnam", "sellable": False}],
        [{"source_id": KEY, "day_no": 1, "hotel_code": "HTL-000101", "room_type": "Garden Twin", "service_code": None}],
        [
            {
                "source_id": KEY,
                "channel": "b2b",
                "season": "Low",
                "peak": False,
                "valid_from": TODAY,
                "valid_to": TODAY + timedelta(days=9),
                "pax_min": 1,
                "pax_max": 2,
                "currency": "USD",
                "amount": D("410.50"),
                "child_amount": None,
                "infant_amount": None,
                "single_supplement_amount": D("90"),
            }
        ],
        [
            {
                "source_id": KEY,
                "start_date": TODAY + timedelta(days=5),
                "capacity": 10,
                "min_to_operate": 2,
                "sales_cutoff_days": 3,
                "decision_days_before": 21,
                "auto_confirm": True,
                "status": "scheduled",
            }
        ],
        note="a note",
    )
    sheets = pi.parse(data)
    assert sheets.problems == []
    assert sheets.tours[0]["sellable"] is False
    assert sheets.prices[0]["amount"] == D("410.50") and sheets.prices[0]["single_supplement_amount"] == D("90")
    assert sheets.departures[0]["sales_cutoff_days"] == 3 and sheets.departures[0]["auto_confirm"] is True
    assert pi.filename(TODAY) == "aa-price-setup-2027-03-01.xlsx"


def _match_conn() -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(
        "SELECT p.id::text AS id, p.source_id, p.sellable FROM booking.trip_plans p",
        [{"id": "p-1", "source_id": KEY, "sellable": False}, {"id": "p-2", "source_id": OTHER, "sellable": False}],
    )
    conn.on(
        "SELECT DISTINCT ON (source_id) source_id, title, country, duration_days",
        [{"source_id": KEY, "title": "Invented Ridge Trek", "country": "Vietnam", "duration_days": 3}],
    )
    conn.on(
        "FROM booking.hotel_room_types t JOIN booking.hotels h",
        [
            {
                "hotel_code": "HTL-1",
                "room_type": "garden twin",
                "id": "rt-1",
                "status": "active",
                "hotel_status": "active",
            },
            {
                "hotel_code": "HTL-1",
                "room_type": "old room",
                "id": "rt-2",
                "status": "inactive",
                "hotel_status": "active",
            },
        ],
    )
    conn.on(
        "FROM booking.services WHERE upper(code)",
        [{"code": "SVC-1", "id": "sv-1", "status": "active"}, {"code": "SVC-2", "id": "sv-2", "status": "inactive"}],
    )
    conn.on(
        "SELECT trip_plan_id::text AS plan_id, start_date FROM booking.departures",
        [{"plan_id": "p-1", "start_date": TODAY + timedelta(days=10)}],
    )
    conn.on(
        "FROM booking.trip_plan_components WHERE trip_plan_id = ANY",
        [{"plan_id": "p-2", "day_no": 1, "hotel_room_type_id": None, "service_id": "sv-1"}],
    )
    return conn


def _comp(row: int, key: str, day: int, **item: Any) -> dict[str, Any]:
    return {
        "_row": row,
        "tour_key": key,
        "day_no": day,
        "hotel_code": None,
        "room_type": None,
        "service_code": None,
        **item,
    }


@pytest.mark.asyncio
async def test_match() -> None:
    sheets = pi.Sheets(
        tours=[
            {"_row": 2, "tour_key": KEY, "sellable": True},
            {"_row": 3, "tour_key": OTHER, "sellable": True},
            {"_row": 4, "tour_key": "gone", "sellable": None},
        ],
        components=[
            _comp(2, KEY, 1, hotel_code="HTL-1", room_type="Garden  Twin"),
            _comp(3, KEY, 2, service_code="SVC-1"),
            _comp(4, KEY, 9, service_code="SVC-1"),
            _comp(5, KEY, 2, hotel_code="HTL-1", room_type="Old room"),
            _comp(6, KEY, 2, hotel_code="HTL-1", room_type="Missing"),
            _comp(7, KEY, 3, service_code="SVC-2"),
            _comp(8, KEY, 3, service_code="SVC-9"),
        ],
        prices=[_price(tour_key=OTHER, channel="b2b", currency="USD", pax_min=1)],
        departures=[
            {"_row": 2, "tour_key": KEY, "start_date": TODAY + timedelta(days=10)},
            {"_row": 3, "tour_key": KEY, "start_date": TODAY + timedelta(days=11)},
            {"_row": 4, "tour_key": KEY, "start_date": TODAY + timedelta(days=11)},
            {"_row": 5, "tour_key": "gone", "start_date": TODAY + timedelta(days=11)},
        ],
    )
    plans = await pi.match(_match_conn(), sheets)  # type: ignore[arg-type]
    assert {(p.sheet, p.row, p.code) for p in sheets.problems} == {
        ("Tours", 4, "unknown_tour"),
        ("Components", 4, "day_out_of_tour"),
        ("Components", 5, "inactive_item"),
        ("Components", 6, "unknown_item"),
        ("Components", 7, "inactive_item"),
        ("Components", 8, "unknown_item"),
        ("Departures", 5, "unknown_tour"),
        ("Tours", 3, "not_in_catalog"),  # OTHER has a component but left the catalog
    }
    one = plans[KEY]
    assert one.components == [
        {"day_no": 1, "hotel_room_type_id": "rt-1", "service_id": None},
        {"day_no": 2, "hotel_room_type_id": None, "service_id": "sv-1"},
    ]
    assert one.existing_departures == [TODAY + timedelta(days=10), TODAY + timedelta(days=11)]
    assert [d["_row"] for d in one.new_departures] == [3]
    assert (one.title, one.in_catalog, one.sellable_after) == ("Invented Ridge Trek", True, True)
    assert plans[OTHER].hand and plans[OTHER].components_now[0]["service_id"] == "sv-1"


@pytest.mark.asyncio
async def test_match_without_components_or_catalog() -> None:
    sheets = pi.Sheets(tours=[{"_row": 2, "tour_key": KEY, "sellable": True}])
    conn = _match_conn().on("FROM booking.trip_plan_components WHERE trip_plan_id = ANY", [])
    await pi.match(conn, sheets)  # type: ignore[arg-type]
    assert [p.code for p in sheets.problems] == ["no_components"]
    import asyncpg

    sheets = pi.Sheets(tours=[{"_row": 2, "tour_key": OTHER, "sellable": True}])
    broken = _match_conn().on(
        "SELECT DISTINCT ON (source_id) source_id, title, country, duration_days", asyncpg.UndefinedTableError("x")
    )
    plans = await pi.match(broken, sheets)  # type: ignore[arg-type]
    assert [p.code for p in sheets.problems] == ["catalog_unreadable"] and plans[OTHER].in_catalog is None


@pytest.mark.asyncio
async def test_status_after() -> None:
    facts = price_readiness.Facts(
        TODAY,
        cost_sheet.Pricing(TODAY, {"b2b": object()}, {}),  # type: ignore[dict-item]
        [
            price_readiness.Plan("p-1", KEY, False),
            price_readiness.Plan("p-2", OTHER, False, hand_rows=1),
        ],
        {"rt-1": {"USD"}},
        [],
    )
    plans = {
        KEY: pi.Plan("p-1", KEY, False, components=[{"day_no": 1, "hotel_room_type_id": "rt-1", "service_id": None}]),
        OTHER: pi.Plan("p-2", OTHER, False),
        "new": pi.Plan("p-3", "new", False),
    }
    conn = ScriptedConn().on("FROM booking.supplier_rates x JOIN booking.supplier_contracts k", [])
    out = await pi.status_after(conn, plans, facts, TODAY)  # type: ignore[arg-type]
    assert out == {KEY: "priced", OTHER: "hand_priced", "new": None}
    plans[KEY].components = [{"day_no": 1, "hotel_room_type_id": None, "service_id": "sv-x"}]
    plans[OTHER].hand = [{"valid_to": TODAY - timedelta(days=1)}]
    out = await pi.status_after(conn, plans, facts, TODAY)  # type: ignore[arg-type]
    assert out[KEY] == "missing_rates" and out[OTHER] == "no_components"


@pytest.mark.asyncio
async def test_import_refuses_with_every_problem_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_sheet, "today", lambda: TODAY)
    data = _book({"Tours": [["Tour key", "Sellable"], ["gone", "yes"]]})
    conn = _match_conn()
    with pytest.raises(Invalid) as exc:
        await pi.import_workbook(conn, STAFF, data, dry_run=True)  # type: ignore[arg-type]
    assert exc.value.code == "price_import_invalid"
    assert exc.value.extra["errors"] == [
        {
            "sheet": "Tours",
            "row": 2,
            "column": "Tour key",
            "code": "unknown_tour",
            "message": "no trip plan with the key gone",
        }
    ]
    assert not conn.sql("INSERT INTO")


@pytest.mark.asyncio
async def test_import_dry_run_and_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_sheet, "today", lambda: TODAY)
    pricing = cost_sheet.Pricing(TODAY, {}, {})

    async def load_pricing(conn: Any, on: date) -> Any:
        return pricing

    facts = price_readiness.Facts(TODAY, pricing, [price_readiness.Plan("p-1", KEY, False)], {}, [])

    async def load(conn: Any, on: date, ids: Any = None) -> Any:
        return facts

    async def resolve(conn: Any, f: Any, rated: Any) -> list[dict[str, Any]]:
        return [{"tour_key": KEY, "status": "no_components"}]

    calls: list[str] = []

    async def replace_components(conn: Any, staff: Any, key: str, items: Any) -> None:
        calls.append(f"components {key} {len(items)}")

    async def store(conn: Any, plan_id: str, key: str, final: Any, **kw: Any) -> bool:
        calls.append(f"prices {key} {len(final)} {kw['action']}")
        return True

    async def create_departure(conn: Any, staff: Any, key: str, data: dict[str, Any]) -> None:
        calls.append(f"departure {key} {data['start_date']} {data['auto_confirm']} {data['decision_days_before']}")

    async def set_sellable(conn: Any, staff: Any, plan: Any, value: bool) -> None:
        calls.append(f"sellable {plan['source_id']} {value}")

    async def recompute_plan(conn: Any, plan_id: str, p: Any) -> dict[str, Any]:
        calls.append(f"recompute {plan_id}")
        return {"outcome": "changed"}

    monkeypatch.setattr(cost_sheet, "load_pricing", load_pricing)
    monkeypatch.setattr(cost_sheet, "recompute_plan", recompute_plan)
    monkeypatch.setattr(price_readiness, "load", load)
    monkeypatch.setattr(price_readiness, "resolve", resolve)
    monkeypatch.setattr(plan_components, "replace_components", replace_components)
    monkeypatch.setattr(trip_plan_prices, "store", store)
    monkeypatch.setattr(departures, "create_departure", create_departure)
    monkeypatch.setattr(price_workbench, "set_sellable", set_sellable)
    start = TODAY + timedelta(days=20)
    data = pi.build_workbook(
        [{"tour_key": KEY, "sellable": True}],
        [{"source_id": KEY, "day_no": 1, "hotel_code": "HTL-1", "room_type": "Garden Twin"}],
        [
            {
                "source_id": KEY,
                "channel": "b2b",
                "valid_from": TODAY,
                "valid_to": start,
                "pax_min": 1,
                "currency": "USD",
                "amount": D("400"),
            }
        ],
        [{"source_id": KEY, "start_date": start, "capacity": 8}],
    )
    conn = (
        _match_conn()
        .on("FROM booking.trip_plan_prices pr", [])
        .on(
            "SELECT p.id::text AS id, p.source_id, p.sellable FROM booking.trip_plans p",
            [{"id": "p-1", "source_id": KEY, "sellable": False}],
        )
    )
    preview = await pi.import_workbook(conn, STAFF, data, dry_run=True)  # type: ignore[arg-type]
    assert calls == [] and preview["dry_run"] is True
    (tour,) = preview["tours"]
    assert (tour["components_after"], tour["components_changed"], tour["hand_rows_after"]) == (1, True, 1)
    assert tour["departures_new"] == [start] and tour["status_before"] == "no_components"
    assert preview["totals"]["sellable_changes"] == 1

    done = await pi.import_workbook(conn, STAFF, data, dry_run=False)  # type: ignore[arg-type]
    assert calls == [
        f"components {KEY} 1",
        f"prices {KEY} 1 trip_plan.prices_imported",
        f"departure {KEY} {start} True None",
        f"sellable {KEY} True",
        "recompute p-1",
    ]
    assert done["recomputed"] == 1
    assert any("trip_plan.bulk_imported" in str(a) for a in conn.sql("INSERT INTO booking.audit_logs"))


@pytest.mark.asyncio
async def test_prefilled_notes_the_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pi, "MAX_TOURS", 1)
    conn = ScriptedConn()
    items = [{"tour_key": KEY, "title": "A", "country": "Vietnam", "sellable": True}, {"tour_key": OTHER}]
    data = await pi.prefilled(conn, items, today=TODAY)  # type: ignore[arg-type]
    assert conn.sql("FROM booking.trip_plan_components c")[0] == ([KEY],)
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True)
    lines = [r[0] for r in wb["Read me"].iter_rows(values_only=True)]
    assert lines[-1].startswith("Prefilled with the first 1 of 2 tours")
