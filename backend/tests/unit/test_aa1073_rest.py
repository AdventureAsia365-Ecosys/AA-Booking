"""AA-1073 rest (bk-048) units: the departure board helpers (month window, early booking, row shape), the ops sheet
checks, departure fees (checks, maker-checker, suggestions, the settlement line), price adjustments (checks,
maker-checker, notes), the quote engine's early-booking discount and departure adjustment (order, conversion, note and
hash), the 1.8 event fee and the agency invoice role, and the early-booking checks of a pricing rule. Names are
invented."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from app.agency_billing import drafts
from app.auth.staff import Staff
from app.b2b import departure_board, departure_fees, departure_ops, events, invoices, price_adjustments, quotes
from app.b2b.components import LineCost
from app.errors import Conflict, Forbidden, Invalid
from app.pricing import engine, policy, repository
from tests.unit.fakes import ScriptedConn

SALES = Staff("u-sales", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-con", None, "Contract One", "contracting", True, "reviewer")
ADMIN = Staff("u-admin", None, "Admin One", "admin", True, "admin")
DEP = "d1000000-0000-4000-8000-000000001073"
FEE = "f1000000-0000-4000-8000-000000001073"
ADJ = "a2000000-0000-4000-8000-000000001073"
PARTNER = "a1000000-0000-4000-8000-000000001073"
TODAY = date(2026, 11, 11)


def _rule(*, early_days: int | None = None, pts: str | None = None, floor: str = "0.10") -> repository.DatedRule:
    return repository.DatedRule(
        engine.PricingRule(engine.Channel.B2B, D("0.20"), D(floor), D("0"), D("0")),
        date(2026, 1, 1),
        early_days=early_days,
        early_discount_pts=D(pts) if pts is not None else None,
    )


class _Terms:
    tier_code = None
    tier_discount = D("0")

    def __init__(self, discount: str = "0") -> None:
        self.discount = D(discount)

    def discount_for(self, plan_id: str | None) -> tuple[D, str | None]:
        return self.discount, None


def _basis(rule: repository.DatedRule, *, currency: str = "USD", discount: str = "0", rates: dict | None = None):
    return quotes.PriceBasis(
        on=TODAY,
        currency=currency,
        rule=rule,
        rates=rates or {},
        fx=quotes.FxLock(currency, D("1"), D("0"), TODAY, "base"),
        terms=_Terms(discount),  # type: ignore[arg-type]
    )


# ── Quote engine: early booking + departure adjustment (bk-17) ───────────────


COST = LineCost(kind="trip_plan", costs=[(D("240"), "USD")])  # 300.00 at a 20 % margin


def test_line_without_rule_or_adjustment_prices_as_before() -> None:
    plain = quotes.price_line(_basis(_rule()), COST, "plan-1", pax=(2, 0, 0), start=TODAY + timedelta(days=90))
    assert (plain.b2b_price, plain.note) == (D("300.00"), None)


def test_early_booking_takes_margin_points_until_start_minus_days() -> None:
    rule = _rule(early_days=60, pts="0.04")
    early = quotes.price_line(_basis(rule), COST, "plan-1", pax=(2, 0, 0), start=TODAY + timedelta(days=60))
    assert early.b2b_price == D("285.72")  # 240 / (1 - 0.16), rounded up
    assert early.note == {"early_booking": {"days": 60, "pts": "0.04"}}
    late = quotes.price_line(_basis(rule), COST, "plan-1", pax=(2, 0, 0), start=TODAY + timedelta(days=59))
    assert (late.b2b_price, late.note) == (D("300.00"), None)
    # Never on a component line or a line without a departure.
    assert quotes.price_line(_basis(rule), COST, None, pax=(2, 0, 0), start=TODAY + timedelta(days=90)).note is None
    assert quotes.price_line(_basis(rule), COST, "plan-1", pax=(2, 0, 0)).note is None


def test_adjustment_applies_after_the_agency_discount() -> None:
    adj = price_adjustments.Adjustment(ADJ, D("-0.1000"), None, None)
    line = quotes.price_line(_basis(_rule(), discount="0.10"), COST, "plan-1", pax=(2, 0, 0), adjustment=adj)
    assert line.b2b_price == D("243.00")  # 300 less 10 % = 270, then less 10 %
    assert line.note == {"adjustment": {"id": ADJ, "pct": "-0.1"}}
    amount = price_adjustments.Adjustment(ADJ, None, D("-15.00"), "USD")
    per_head = quotes.price_line(_basis(_rule()), COST, "plan-1", pax=(2, 1, 1), adjustment=amount)
    assert per_head.b2b_price == D("255.00")  # 300 - 15 x 3 travellers (the infant takes no seat)
    assert per_head.note == {"adjustment": {"id": ADJ, "amount": "-15", "currency": "USD"}}
    huge = price_adjustments.Adjustment(ADJ, None, D("-1000"), "USD")
    assert quotes.price_line(_basis(_rule()), COST, "plan-1", pax=(1, 0, 0), adjustment=huge).b2b_price == D("0.00")


def test_adjustment_amount_in_another_currency_converts_at_the_plain_rate() -> None:
    rates = {"EUR": engine.FxRate("EUR", D("0.5"), D("0.10"))}
    adj = price_adjustments.Adjustment(ADJ, None, D("10"), "EUR")
    line = quotes.price_line(_basis(_rule(), rates=rates), COST, "plan-1", pax=(1, 0, 0), adjustment=adj)
    assert line.b2b_price == D("320.00")  # 10 EUR = 20 USD, no buffer


def test_note_hash_only_when_an_adjustment_or_early_discount_applied() -> None:
    assert quotes._note_hash({"price_note": None}) == {}
    assert quotes._note_hash({"price_note": {"below_floor": True}}) == {}
    note = {"adjustment": {"id": ADJ, "pct": "-0.1"}, "below_floor": True}
    assert quotes._note_hash({"price_note": note}) == {"price_note": {"adjustment": {"id": ADJ, "pct": "-0.1"}}}


# ── Departure board ──────────────────────────────────────────────────────────


def test_month_window_and_early_booking_window() -> None:
    assert departure_board.month_window(date(2026, 2, 14)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert departure_board.month_window(date(2026, 12, 31)) == (date(2026, 12, 1), date(2026, 12, 31))
    rule = _rule(early_days=30, pts="0.03")
    start = TODAY + timedelta(days=30)
    assert departure_board.early_booking(rule, start, TODAY) == {"days": 30, "pts": D("0.03"), "until": TODAY}
    assert departure_board.early_booking(rule, start, TODAY + timedelta(days=1)) is None
    assert departure_board.early_booking(_rule(), start, TODAY) is None
    assert departure_board.early_booking(None, start, TODAY) is None


def _board_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": DEP, "trip_plan_id": "p-1", "source_id": "tour-1", "start_date": TODAY, "status": "scheduled",
        "capacity": 10, "held": 2, "sold": 3, "allotted": 4, "allotment_unused": 4, "free": 1,
        "occupancy_pct": D("50.0"), "min_to_operate": 5, "at_risk": True, "open_incident_id": None,
        "open_incident_code": None, "agencies_count": 2, "auto_confirm": True, "sales_cutoff_days": 0,
        "decision_days_before": 21, "meeting_point": None, "meeting_time": None, "price_hint_amount": None,
        "price_hint_currency": None, "price_hint_basis": None,
    }  # fmt: skip
    return row | over


def test_board_row_shape() -> None:
    adj = price_adjustments.Adjustment(ADJ, D("-0.05"), None, None)
    out = departure_board.row_out(
        _board_row(
            open_incident_id="i-1",
            open_incident_code="road",
            price_hint_amount=D("990"),
            price_hint_currency="USD",
            price_hint_basis="per_person",
        ),  # fmt: skip
        {"tour-1": {"title": "Invented Lantern Valley Trek", "country": "Vietnam"}},
        adj,
        None,
    )
    assert (out["tour_title"], out["country"], out["free"], out["at_risk"]) == (
        "Invented Lantern Valley Trek",
        "Vietnam",
        1,
        True,
    )
    assert out["open_incident"] == {"id": "i-1", "incident_code": "road"}
    assert out["price"] == {
        "hint": {"amount": D("990"), "currency": "USD", "basis": "per_person"},
        "adjustment": {"id": ADJ, "adjust_pct": D("-0.05"), "amount": None, "currency": None},
        "early_booking": None,
    }
    bare = departure_board.row_out(_board_row(), {}, None, None)
    assert bare["tour_title"] is None and bare["price"]["hint"] is None and bare["open_incident"] is None


@pytest.mark.asyncio
async def test_board_refuses_a_bad_window_and_status() -> None:
    conn = ScriptedConn()
    with pytest.raises(Invalid):
        await departure_board.board(conn, start=date(2027, 1, 1), end=date(2026, 1, 1))  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        await departure_board.board(conn, start=TODAY, end=TODAY + timedelta(days=401))  # type: ignore[arg-type]
    with pytest.raises(Invalid):
        await departure_board.board(conn, status="paused", today=TODAY)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_board_reads_the_view_with_the_filters() -> None:
    conn = ScriptedConn()
    conn.on("FROM booking.v_departure_occupancy o", [_board_row()])
    conn.on("count(*)::int AS departures", {"departures": 1, "capacity": 10, "held": 2, "sold": 3, "free": 1,
                                             "at_risk": 1, "incidents": 0})  # fmt: skip
    conn.on("lower(country) = lower($1)", [{"source_id": "tour-1"}])
    conn.on("SELECT DISTINCT p.source_id", [{"source_id": "tour-1"}, {"source_id": "tour-2"}])
    conn.on("SELECT source_id, title, country FROM catalog.v_tour", [
        {"source_id": "tour-1", "title": "Invented Lantern Valley Trek", "country": "Vietnam"}])  # fmt: skip
    out = await departure_board.board(conn, country="vietnam", at_risk=True, today=TODAY)  # type: ignore[arg-type]
    assert out["total"] == 1 and out["countries"] == ["Vietnam"]
    assert out["plans"] == [
        {"source_id": "tour-1", "title": "Invented Lantern Valley Trek"},
        {"source_id": "tour-2", "title": None},
    ]
    assert (out["from"], out["to"]) == (date(2026, 11, 1), date(2026, 11, 30))
    args = conn.sql("ORDER BY o.start_date")[0]
    assert args[:6] == (date(2026, 11, 1), date(2026, 11, 30), None, None, True, ["tour-1"])


# ── Ops sheet ────────────────────────────────────────────────────────────────


def test_clean_ops_sets_only_what_is_sent() -> None:
    assert departure_ops.clean_ops({}) == {}
    assert departure_ops.clean_ops({"meeting_point": "  Lantern Square fountain ", "meeting_time": time(7, 30)}) == {
        "meeting_point": "Lantern Square fountain",
        "meeting_time": time(7, 30),
    }
    assert departure_ops.clean_ops({"meeting_point": "", "ops_note": "  "}) == {"meeting_point": None, "ops_note": None}
    with pytest.raises(Invalid):
        departure_ops.clean_ops({"meeting_point": "x" * 301})
    with pytest.raises(Invalid):
        departure_ops.clean_ops({"meeting_time": "07:30"})


@pytest.mark.asyncio
async def test_update_ops_writes_only_changes_and_refuses_a_cancelled_meeting() -> None:
    row = {"id": DEP, "start_date": TODAY, "status": "scheduled", "meeting_point": "Old gate", "meeting_time": None,
           "ops_note": None}  # fmt: skip
    conn = ScriptedConn().on("FROM booking.departures WHERE id = $1::uuid", row)
    await departure_ops.update_ops(conn, SALES, DEP, {"meeting_point": "Old gate"})  # type: ignore[arg-type]
    assert not conn.sql("UPDATE booking.departures")
    await departure_ops.update_ops(conn, SALES, DEP, {"ops_note": "Coach two"})  # type: ignore[arg-type]
    assert conn.sql("UPDATE booking.departures")[0][1:4] == ("Old gate", None, "Coach two")
    audit = conn.sql("INSERT INTO booking.audit_logs")[-1]
    assert "Coach two" not in str(audit)  # the staff note is never copied into the audit row
    cancelled = ScriptedConn().on("FROM booking.departures WHERE id = $1::uuid", row | {"status": "cancelled"})
    with pytest.raises(Conflict):
        await departure_ops.update_ops(cancelled, SALES, DEP, {"meeting_time": time(8, 0)})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_assign_checks_one_kind_of_person() -> None:
    conn = ScriptedConn().on(
        "FROM booking.departures WHERE id = $1::uuid",
        {
            "id": DEP,
            "start_date": TODAY,
            "status": "scheduled",
            "meeting_point": None,
            "meeting_time": None,
            "ops_note": None,
        },  # fmt: skip
    )
    for bad in ({"role": "cook", "staff_user_id": "u-1"}, {"role": "guide"},
                {"role": "guide", "staff_user_id": "u-1", "supplier_id": PARTNER}):  # fmt: skip
        with pytest.raises(Invalid):
            await departure_ops.assign(conn, SALES, DEP, bad)  # type: ignore[arg-type]
    with pytest.raises(Invalid):  # not an active staff member
        await departure_ops.assign(conn, SALES, DEP, {"role": "guide", "staff_user_id": "u-x"})  # type: ignore[arg-type]


# ── Departure fees ───────────────────────────────────────────────────────────


def test_check_fee() -> None:
    reason, seats, price, currency, amount, note = departure_fees.check_fee(
        {"reason": "min_shortfall", "seats": 3, "price_per_seat": "40", "currency": "usd"}
    )
    assert (reason, seats, price, currency, amount, note) == ("min_shortfall", 3, D("40.00"), "USD", D("120.00"), None)
    for bad in (
        {"reason": "late", "seats": 1, "price_per_seat": "1", "currency": "USD"},
        {"reason": "other", "seats": 1, "price_per_seat": "1", "currency": "USD"},  # needs a note
        {"reason": "min_shortfall", "seats": 0, "price_per_seat": "1", "currency": "USD"},
        {"reason": "min_shortfall", "seats": 1, "price_per_seat": "0", "currency": "USD"},
        {"reason": "min_shortfall", "seats": 1, "price_per_seat": "1", "currency": "US"},
    ):
        with pytest.raises(Invalid):
            departure_fees.check_fee(bad)


def test_fee_suggestions() -> None:
    allotments = [
        {"partner_id": "p-1", "state": "released", "unused": 2},
        {"partner_id": "p-2", "state": "in_force", "unused": 0},
    ]
    assert departure_fees.suggestions(min_to_operate=8, sold=5, status="scheduled", allotments=allotments) == [
        {"reason": "min_shortfall", "partner_id": None, "seats": 3},
        {"reason": "allotment_unreleased", "partner_id": "p-1", "seats": 2},
    ]
    assert departure_fees.suggestions(min_to_operate=0, sold=0, status="scheduled", allotments=[]) == []
    assert departure_fees.suggestions(min_to_operate=8, sold=1, status="cancelled", allotments=[]) == []


def _fee_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": FEE, "departure_id": DEP, "start_date": TODAY, "partner_id": PARTNER,
        "agency_name": "Lanternfield Travel",
        "reason": "min_shortfall", "seats": 2, "price_per_seat": D("40.00"), "currency": "USD", "amount": D("80.00"),
        "note": None, "status": "pending", "created_by": SALES.cis_user_id, "created_by_name": "Sales One",
        "created_at": datetime(2026, 11, 11, tzinfo=UTC), "decided_by": None, "decided_by_name": None,
        "decided_at": None, "invoice_number": None, "billed": False,
    }  # fmt: skip
    return row | over


@pytest.mark.asyncio
async def test_fee_decision_is_maker_checker_and_final() -> None:
    conn = ScriptedConn().on("FROM booking.departure_fees f", _fee_row())
    with pytest.raises(Forbidden):
        await departure_fees.decide(conn, SALES, FEE, approve=True)  # type: ignore[arg-type]
    assert departure_fees.fee_out(_fee_row(), ADMIN)["can_decide"] is True
    assert departure_fees.fee_out(_fee_row(), SALES)["can_decide"] is False  # no b2b_desk.approve
    done = ScriptedConn().on("FROM booking.departure_fees f", _fee_row(status="approved", decided_by="u-admin"))
    assert (await departure_fees.decide(done, ADMIN, FEE, approve=True))["status"] == "approved"  # type: ignore[arg-type]
    with pytest.raises(Conflict):
        await departure_fees.decide(done, ADMIN, FEE, approve=False)  # type: ignore[arg-type]
    ok = ScriptedConn().on("FROM booking.departure_fees f", _fee_row())
    await departure_fees.decide(ok, ADMIN, FEE, approve=True)  # type: ignore[arg-type]
    assert ok.sql("UPDATE booking.departure_fees SET status")[0][1:3] == ("approved", "u-admin")


def test_settlement_fee_line() -> None:
    line = drafts.fee_line(
        {
            "fee_id": FEE,
            "seats": 2,
            "price_per_seat": D("40.00"),
            "reason": "allotment_unreleased",
            "start_date": date(2026, 12, 5),
        }  # fmt: skip
    )
    assert line == {
        "line_role": "departure_fee",
        "source_fee_id": FEE,
        "quantity": D(2),
        "unit_price": D("40.00"),
        "description": "Departure fee (allotted seats not released): departure 2026-12-05, 2 seats",
    }


# ── Price adjustments ────────────────────────────────────────────────────────


def test_check_adjustment_value() -> None:
    assert price_adjustments.check_value({"adjust_pct": D("-0.1"), "reason": " Low season "}) == (
        D("-0.1"),
        None,
        None,
        "Low season",
    )
    assert price_adjustments.check_value({"amount": D("25"), "currency": "eur", "reason": "Festival week"}) == (
        None,
        D("25"),
        "EUR",
        "Festival week",
    )
    for bad in (
        {"reason": "x"},
        {"adjust_pct": D("0.1"), "amount": D("5"), "currency": "USD", "reason": "x"},
        {"adjust_pct": D("0"), "reason": "x"},
        {"adjust_pct": D("-0.6"), "reason": "x"},
        {"amount": D("5"), "reason": "x"},
        {"adjust_pct": D("0.1"), "reason": " "},
    ):
        with pytest.raises(Invalid):
            price_adjustments.check_value(bad)
    assert price_adjustments.describe(D("-0.1"), None, None) == "-10 %"
    assert price_adjustments.describe(None, D("25"), "EUR") == "+25 EUR per traveller"


def _adj_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": ADJ, "departure_id": DEP, "adjust_pct": D("-0.1"), "amount": None, "currency": None,
        "reason": "Low season", "replaces_id": None, "created_by": CONTRACTING.cis_user_id,
        "created_by_name": "Contract One", "created_at": datetime(2026, 11, 11, tzinfo=UTC), "approved_by": None,
        "approved_by_name": None, "approved_at": None, "ended_at": None, "ended_by": None, "ended_by_name": None,
        "end_reason": None,
    }  # fmt: skip
    return row | over


@pytest.mark.asyncio
async def test_adjustment_maker_checker_and_end() -> None:
    conn = ScriptedConn().on("FROM booking.departure_price_adjustments a", _adj_row())
    with pytest.raises(Forbidden):
        await price_adjustments.approve(conn, CONTRACTING, ADJ)  # type: ignore[arg-type]
    assert price_adjustments.adjustment_out(_adj_row(), ADMIN)["can_approve"] is True
    assert price_adjustments.adjustment_out(_adj_row(), SALES)["can_approve"] is False  # no pricing.write
    ok = ScriptedConn().on("FROM booking.departure_price_adjustments a", _adj_row())
    ok.on("SELECT status FROM booking.departures", "scheduled")
    await price_adjustments.approve(ok, ADMIN, ADJ)  # type: ignore[arg-type]
    assert ok.sql("end_reason = 'replaced'") and ok.sql("SET approved_by = $2")
    cancelled = ScriptedConn().on("FROM booking.departure_price_adjustments a", _adj_row())
    cancelled.on("SELECT status FROM booking.departures", "cancelled")
    with pytest.raises(Conflict):
        await price_adjustments.approve(cancelled, ADMIN, ADJ)  # type: ignore[arg-type]
    ended = ScriptedConn().on("FROM booking.departure_price_adjustments a", _adj_row())
    await price_adjustments.end(ended, ADMIN, ADJ)  # type: ignore[arg-type]
    assert ended.sql("SET ended_at = now()")[0][2] == "rejected"


@pytest.mark.asyncio
async def test_create_refuses_a_second_pending_adjustment() -> None:
    conn = ScriptedConn().on(
        "FROM booking.departures WHERE id = $1::uuid FOR UPDATE",
        {"id": DEP, "start_date": TODAY, "status": "scheduled"},
    )
    conn.on("WHERE departure_id = $1::uuid AND ended_at IS NULL", [{"id": ADJ, "approved_at": None}])
    with pytest.raises(Conflict) as err:
        await price_adjustments.create(conn, CONTRACTING, DEP, {"adjust_pct": D("0.05"), "reason": "Peak"})  # type: ignore[arg-type]
    assert err.value.code == "price_adjustment_pending"


# ── Contract 1.8 and the agency invoice ──────────────────────────────────────


def test_event_fee_and_agency_line_role() -> None:
    assert events.fee_out({"fee_id": None}) is None
    assert events.fee_out({"fee_id": FEE, "amount": D("80"), "currency": "USD"}) == {
        "fee_id": FEE,
        "amount": "80.00",
        "currency": "USD",
    }
    row = {"line_no": 1, "line_role": "departure_fee", "description": "Departure fee", "quantity": D(2),
           "unit_price": D("40"), "amount": D("80"), "voided": False}  # fmt: skip
    assert invoices.line(row)["line_role"] == "adjustment"
    assert invoices.line(row | {"line_role": "deposit"})["line_role"] == "deposit"


# ── Pricing rule: early booking ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_early_booking_checks() -> None:
    base = {"target_margin": D("0.20"), "floor_margin": D("0.10"), "peak_uplift_pts": D("0"), "card_fee_pct": D("0"),
            "valid_from": date(2027, 1, 1)}  # fmt: skip
    conn = ScriptedConn()
    for data, code in (
        ({"channel": "b2b", "early_days": 30}, "invalid_early_booking"),
        ({"channel": "b2c_tailor_made", "early_days": 30, "early_discount_pts": D("0.02")}, "early_booking_channel"),
        ({"channel": "b2b", "early_days": 30, "early_discount_pts": D("0.25")}, "invalid_early_booking"),
    ):
        with pytest.raises(Invalid) as err:
            await policy.create_rule(conn, CONTRACTING, base | data, today=TODAY)  # type: ignore[arg-type]
        assert err.value.code == code
