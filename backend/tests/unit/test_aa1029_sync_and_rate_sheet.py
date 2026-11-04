"""AA-1029 R4 without a database: the reference price parser of the catalog bridge, its schedule key, the rate-sheet
workbook parser (shape problems per sheet, row and column; the template round-trips), the rule markup guard, and the
rate-sheet routes' size and type checks. Invented supplier and item codes only."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal as D
from typing import Any

import pytest
from openpyxl import Workbook

from app.catalog import trip_plan_sync as sync
from app.errors import Invalid
from app.pricing import engine, policy
from app.supply import rate_sheet as rs


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("$1,722/person", (D("1722.00"), "USD")),
        ("from USD 495", (D("495.00"), "USD")),
        ("1.200 EUR pp", (D("1200.00"), "EUR")),
        ("EUR 1.234,50 per person", (D("1234.50"), "EUR")),
        ("£899 twin share", (D("899.00"), "GBP")),
        ("US$ 2,350.75", (D("2350.75"), "USD")),
        ("12,500,000 VND", (D("12500000.00"), "VND")),
        ("1,722", None),  # no currency
        ("on request", None),
        ("", None),
        (None, None),
        ("USD 0", None),
    ],
)
def test_parse_reference(raw: str | None, parsed: tuple[D, str] | None) -> None:
    assert sync.parse_reference(raw) == parsed


def test_clean_raw_and_schedule_key() -> None:
    assert sync.clean_raw("  from \n USD  495 ") == "from USD 495"
    assert sync.clean_raw("x" * 900) == "x" * sync.RAW_MAX
    assert sync.clean_raw("   ") is None
    at = datetime(2027, 1, 1, 0, 7, tzinfo=UTC)
    assert sync.schedule_key(at) == sync.schedule_key(datetime(2027, 1, 1, 0, 14, tzinfo=UTC))
    assert sync.schedule_key(at) != sync.schedule_key(datetime(2027, 1, 1, 0, 15, tzinfo=UTC))


CONTRACT = {"Title": "Lumen Ridge 2027", "Valid from": "2027-01-01", "Valid to": "2027-12-31", "Currency": "USD"}


def test_template_round_trips_with_no_rates() -> None:
    sheet = rs.parse(rs.template())
    codes = {p.code for p in sheet.problems}
    assert codes == {"required", "no_rates"}  # empty contract fields, no rate row


def test_parse_reads_hotel_and_service_rows() -> None:
    data = rs.build_workbook(
        CONTRACT,
        hotel_rows=[("htl-900001", "Garden  Twin", "Low", date(2027, 1, 1), "2027-06-30", "bb", 2, 2, 85, "invented")],
        service_rows={"Guide": [("SVC-900002", None, None, None, None, None, "60.5", None)]},
    )
    sheet = rs.parse(data)
    assert sheet.problems == []
    assert sheet.contract["title"] == "Lumen Ridge 2027" and sheet.contract["valid_from"] == date(2027, 1, 1)
    hotel, guide = sheet.rows
    assert (hotel.hotel_code, hotel.room_type) == ("HTL-900001", "Garden Twin")
    assert hotel.values == {"season": "Low", "valid_from": date(2027, 1, 1), "valid_to": date(2027, 6, 30),
                            "pax_min": 2, "pax_max": 2, "net_amount": D("85"), "notes": "invented",
                            "meal_plan": "BB"}  # fmt: skip
    assert (guide.sheet, guide.service_code, guide.values) == ("Guide", "SVC-900002", {"net_amount": D("60.5")})


def test_parse_reports_every_problem_by_sheet_row_and_column() -> None:
    data = rs.build_workbook(
        {**CONTRACT, "Currency": "dollars", "Valid from": "first of May"},
        hotel_rows=[("HTL-900001", None, None, None, None, None, 0, 1000, "-5", None)],
        service_rows={"Meal": [(None, None, "2027-13-01", None, None, None, None, None)]},
    )
    problems = {(p.sheet, p.row, p.column, p.code) for p in rs.parse(data).problems}
    assert ("Contract", 3, "Valid from", "invalid_value") in problems
    assert ("Contract", 5, "Currency", "invalid_value") in problems
    assert ("Hotel", 2, "Room type", "required") in problems
    assert ("Hotel", 2, "Pax min", "invalid_value") in problems
    assert ("Hotel", 2, "Pax max", "invalid_value") in problems
    assert ("Hotel", 2, "Net", "invalid_value") in problems
    assert ("Hotel", 2, "Net", "required") not in problems  # reported once
    assert ("Meal", 2, "Service code", "required") in problems
    assert ("Meal", 2, "Valid from", "invalid_value") in problems
    assert ("Meal", 2, "Net", "required") in problems


def _workbook(sheets: dict[str, list[list[Any]]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_headers_any_order_ignored_sheets_and_missing_columns() -> None:
    data = _workbook({
        "Read me": [["How to fill this sheet"]],
        "contract": [["title", "X"], ["valid from", "2027-01-01"], ["currency", "thb"]],
        "Activity": [["Net", "service code"], [12, "svc-900003"]],
        "Transport": [["Season"], ["Low"]],
    })  # fmt: skip
    sheet = rs.parse(data)
    assert sheet.ignored_sheets == ["Read me"]
    assert sheet.contract["currency"] == "THB"
    assert [(r.sheet, r.service_code, r.values["net_amount"]) for r in sheet.rows] == [("Activity", "SVC-900003", 12)]
    assert {(p.sheet, p.code) for p in sheet.problems} == {("Transport", "missing_column")}


def test_parse_refuses_what_is_not_a_workbook_and_bounds_size_and_rows() -> None:
    with pytest.raises(Invalid) as exc:
        rs.parse(b"not a zip")
    assert exc.value.code == "rate_sheet_not_xlsx"
    with pytest.raises(Invalid) as exc:
        rs.parse(b"")
    assert exc.value.code == "rate_sheet_empty"
    with pytest.raises(Invalid) as exc:
        rs.parse(b"x" * (rs.MAX_BYTES + 1))
    assert exc.value.code == "rate_sheet_too_large"
    many = rs.build_workbook(CONTRACT, service_rows={"Guide": [("SVC-1", None, None, None, None, None, 1, None)]
                                                     * (rs.MAX_ROWS + 1)})  # fmt: skip
    with pytest.raises(Invalid) as exc:
        rs.parse(many)
    assert exc.value.code == "rate_sheet_too_many_rows"


class _Conn:
    async def fetchval(self, *args: Any) -> Any:  # pragma: no cover — never reached
        raise AssertionError("no write expected")


@pytest.mark.asyncio
async def test_rrp_markup_is_b2b_only() -> None:
    staff: Any = type("S", (), {"cis_user_id": "u-1"})()
    data = {"channel": "fixed_departure", "target_margin": D("0.25"), "floor_margin": D("0.18"),
            "peak_uplift_pts": D("0"), "card_fee_pct": D("0"), "valid_from": date(2027, 1, 1), "note": None,
            "rrp_markup_pct": D("0.25")}  # fmt: skip
    with pytest.raises(Invalid) as exc:
        await policy.create_rule(_Conn(), staff, data, today=date(2027, 1, 1))  # type: ignore[arg-type]
    assert exc.value.code == "rrp_markup_b2b_only"
    assert engine.Channel.B2B.value == "b2b"
