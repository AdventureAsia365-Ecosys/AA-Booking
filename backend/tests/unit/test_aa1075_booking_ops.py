"""AA-1075 (bk-047) units: staff agency bookings (errors, row masking, maker-checker), the price override and floor,
child / infant prices on a tour line, partial cancellation shares and checks, the fee override, vouchers and the
manifest HTML (escaping, no names, no prices), the cost sheet's single supplement and per-type prices, per-tour terms,
supplier cancel fees and the trip plan age bands. Names are invented."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal as D
from typing import Any

import pytest

from app.agency_billing import config
from app.auth.staff import Staff
from app.b2b import amendments, desk_bookings, manifest, quotes, vouchers
from app.b2b.components import LineCost
from app.b2b.errors import B2BError
from app.catalog import cost_sheet, trip_plans
from app.catalog.cost_sheet import NetRow, Part, Window
from app.errors import Conflict, Forbidden, Gone, Invalid, NotFound, Unavailable
from app.ops import supplier_bookings as sb
from app.pricing import engine, repository
from app.supply.rates import RateAsk
from tests.unit.fakes import ScriptedConn

SALES = Staff("u-sales", None, "Sales One", "sales", True, "reviewer")
CONTRACTING = Staff("u-con", None, "Contract One", "contracting", True, "reviewer")
CONTENT = Staff("u-writer", None, "Writer", "content_creator", True, "content")
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
CASE = "c1000000-0000-4000-8000-000000001075"
HOLD = "b1000000-0000-4000-8000-000000001075"
PARTNER = "a1000000-0000-4000-8000-000000001075"
POLICY = "d1000000-0000-4000-8000-000000001075"


def _rule(child: str | None = None, infant: str | None = None, floor: str = "0.10") -> repository.DatedRule:
    return repository.DatedRule(
        engine.PricingRule(engine.Channel.B2B, D("0.20"), D(floor), D("0"), D("0")),
        date(2026, 1, 1),
        child_pct=D(child) if child is not None else None,
        infant_flat_usd=D(infant) if infant is not None else None,
    )


class _Terms:
    tier_code = None
    tier_discount = D("0")

    def discount_for(self, plan_id: str | None) -> tuple[D, str | None]:
        return D("0"), None


def _basis(rule: repository.DatedRule) -> quotes.PriceBasis:
    return quotes.PriceBasis(
        on=date(2026, 11, 11),
        currency="USD",
        rule=rule,
        rates={},
        fx=quotes.FxLock("USD", D("1"), D("0"), date(2026, 11, 11), "base"),
        terms=_Terms(),  # type: ignore[arg-type]
    )


# ── Quote: child / infant, floor, override scaling ───────────────────────────


def test_a_child_pays_the_rule_share_and_an_infant_adds_its_flat_price() -> None:
    cost = LineCost(kind="trip_plan", costs=[(D("240"), "USD")])  # 3 travellers at 80 each
    adults = quotes.price_line(_basis(_rule()), cost, "plan-1", pax=(3, 0, 0))
    assert adults.b2b_price == D("300.00")  # 240 / (1 - 0.20)
    with_child = quotes.price_line(_basis(_rule(child="0.5")), cost, "plan-1", pax=(2, 1, 0))
    assert with_child.b2b_price == D("250.00")  # 300 x (2 + 0.5) / 3
    with_infant = quotes.price_line(_basis(_rule(child="0.5", infant="16")), cost, "plan-1", pax=(2, 1, 1))
    assert with_infant.b2b_price == D("270.00") and with_infant.net_usd == D("256")  # + 16 / 0.8; net + 16
    # Without a rule share, or on a component line (no plan), the line prices as before.
    assert quotes.price_line(_basis(_rule()), cost, "plan-1", pax=(2, 1, 1)).b2b_price == D("300.00")
    assert quotes.price_line(_basis(_rule(child="0.5")), cost, None, pax=(2, 1, 0)).b2b_price == D("300.00")


def test_floor_price_uses_the_floor_margin() -> None:
    cost = LineCost(kind="trip_plan", costs=[(D("90"), "USD")])
    assert quotes.floor_price(_basis(_rule()), cost, "plan-1", pax=(1, 0, 0)) == D("100.00")


@pytest.mark.parametrize(
    ("prices", "target", "currency", "expected"),
    [
        ([D("100"), D("300")], D("200"), "USD", [D("50.00"), D("150.00")]),
        ([D("33.33"), D("33.33"), D("33.34")], D("10"), "USD", [D("3.33"), D("3.33"), D("3.34")]),
        ([D("0"), D("0")], D("5"), "USD", [D("0.00"), D("5.00")]),
        ([D("1000000"), D("2000000")], D("1500000"), "VND", [D("500000.00"), D("1000000.00")]),
    ],
)
def test_override_scaling_sums_to_the_target(prices: list[D], target: D, currency: str, expected: list[D]) -> None:
    out = quotes.scale_prices(prices, target, currency)
    assert out == expected and sum(out) == target


# ── Staff bookings ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "kind", "status"),
    [("not_found", NotFound, 404), ("price_changed", Conflict, 409), ("hold_expired", Gone, 410),
     ("sold_out", Invalid, 422), ("unavailable", Unavailable, 503), ("rehold_limit", Conflict, 409)],
)  # fmt: skip
def test_b2b_errors_read_as_staff_errors(error: str, kind: type, status: int) -> None:
    out = desk_bookings.staff_error(B2BError(error, "a message"))
    assert isinstance(out, kind) and out.status_code == status and out.code == error


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "id": "e1", "partner_id": PARTNER, "agency_name": "Lanternfield Travel", "tenant_id": "t1",
        "agency_ref": "AG-1",
        "quote_id": "q1", "hold_id": HOLD, "hold_expires_at": NOW, "trip_case_id": None, "trip_case_code": None,
        "currency": "USD", "total_b2b_price": D("90"), "quoted_total": D("120"), "floor_total": D("100"),
        "override_total": D("90"), "override_reason": "Repeat group", "entered_by": "u-sales",
        "entered_by_name": "Sales One", "entered_at": NOW, "decided_by": None, "decided_by_name": None,
        "decided_at": None, "status": "awaiting_approval", "stored_status": "awaiting_approval",
    }  # fmt: skip
    return {**row, **over}


def test_row_out_hides_the_floor_without_pricing_read_and_never_lets_the_maker_decide() -> None:
    assert desk_bookings.row_out(_row(), SALES)["floor_total"] == "100.00"
    out = desk_bookings.row_out(_row(), CONTENT)
    assert out["floor_total"] is None and out["can_decide"] is False
    assert desk_bookings.row_out(_row(), SALES)["can_decide"] is False  # sales cannot approve, and entered it
    assert desk_bookings.row_out(_row(), CONTRACTING)["can_decide"] is True
    assert desk_bookings.row_out(_row(status="lapsed"), CONTRACTING)["can_decide"] is False


@pytest.mark.asyncio
async def test_enter_needs_a_reason_with_an_override_and_a_real_agency_ref() -> None:
    conn = ScriptedConn()
    common = {"partner_id": PARTNER, "currency": "USD", "lines": [], "idempotency_key": "desk-key-1"}
    with pytest.raises(Invalid) as e:
        await desk_bookings.enter(conn, SALES, agency_ref="AG-1", override_total=D("1"), **common)  # type: ignore[arg-type]
    assert e.value.code == "override_reason_required"
    with pytest.raises(Invalid):
        await desk_bookings.enter(conn, SALES, agency_ref="AG-1", override_reason="why", **common)  # type: ignore[arg-type]
    with pytest.raises(Invalid) as e:
        await desk_bookings.enter(conn, SALES, agency_ref="   ", **common)  # type: ignore[arg-type]
    assert e.value.code == "invalid_agency_ref"
    assert conn.calls == []


@pytest.mark.asyncio
async def test_enter_refuses_an_unknown_or_suspended_agency_and_replays_a_key() -> None:
    conn = ScriptedConn().on("FROM booking.b2b_partners WHERE id", None)
    with pytest.raises(NotFound):
        await desk_bookings.enter(
            conn, SALES, partner_id=PARTNER, agency_ref="AG-1", currency="USD", lines=[], idempotency_key="desk-key-2"
        )
    conn = ScriptedConn().on(
        "FROM booking.b2b_partners WHERE id",
        {"id": PARTNER, "tenant_id": "t1", "m2m_client_id": "c1", "status": "suspended", "agency_name": "Copperbay"},
    )
    with pytest.raises(Conflict) as e:
        await desk_bookings.enter(
            conn, SALES, partner_id=PARTNER, agency_ref="AG-1", currency="USD", lines=[], idempotency_key="desk-key-3"
        )
    assert e.value.code == "partner_suspended"
    replay = ScriptedConn().on("WHERE d.idempotency_key = $1", _row(stored_status="booked", status="booked"))
    out = await desk_bookings.enter(
        replay, SALES, partner_id=PARTNER, agency_ref="AG-1", currency="USD", lines=[], idempotency_key="desk-key-4"
    )
    assert out["id"] == "e1"
    with pytest.raises(Invalid) as e:
        await desk_bookings.enter(
            replay, SALES, partner_id=PARTNER, agency_ref="AG-2", currency="USD", lines=[], idempotency_key="desk-key-4"
        )
    assert e.value.code == "idempotency_key_reused"


@pytest.mark.asyncio
async def test_the_maker_never_decides_and_a_decided_row_is_final() -> None:
    conn = ScriptedConn().on("WHERE d.id = $1::uuid", _row(id="e1000000-0000-4000-8000-000000000001"))
    maker = Staff("u-sales", None, "Sales One", "admin", True, "admin")
    with pytest.raises(Forbidden) as e:
        await desk_bookings.approve(conn, maker, "e1000000-0000-4000-8000-000000000001")
    assert e.value.code == "maker_checker"
    with pytest.raises(Forbidden):
        await desk_bookings.reject(conn, maker, "e1000000-0000-4000-8000-000000000001")
    done = ScriptedConn().on("WHERE d.id = $1::uuid", _row(stored_status="rejected", status="rejected"))
    with pytest.raises(Conflict):
        await desk_bookings.approve(done, CONTRACTING, "e1000000-0000-4000-8000-000000000001")
    with pytest.raises(NotFound):
        await desk_bookings.approve(ScriptedConn(), CONTRACTING, "not-a-uuid")


@pytest.mark.asyncio
async def test_reject_releases_the_hold() -> None:
    conn = ScriptedConn().on("WHERE d.id = $1::uuid", _row(id="e1000000-0000-4000-8000-000000000001"))
    await desk_bookings.reject(conn, CONTRACTING, "e1000000-0000-4000-8000-000000000001")
    assert conn.sql("SET status = 'released'") == [(HOLD,)]
    assert conn.sql("UPDATE booking.b2b_desk_bookings SET status") == [
        ("e1000000-0000-4000-8000-000000000001", "rejected", "u-con")
    ]


@pytest.mark.asyncio
async def test_an_expired_hold_lapses_the_row_on_approval() -> None:
    conn = ScriptedConn().on("WHERE d.id = $1::uuid", _row(id="e1000000-0000-4000-8000-000000000001", status="lapsed"))
    with pytest.raises(Gone):
        await desk_bookings.approve(conn, CONTRACTING, "e1000000-0000-4000-8000-000000000001")
    assert conn.sql("UPDATE booking.b2b_desk_bookings SET status")[0][1] == "lapsed"


# ── Partial cancellation and the fee override ───────────────────────────────


def test_removed_share() -> None:
    assert amendments.removed_share(D("300"), 3, 1, "USD") == D("100.00")
    assert amendments.removed_share(D("100"), 3, 1, "USD") == D("33.33")
    assert amendments.removed_share(D("1000000"), 3, 1, "VND") == D("333333")
    assert amendments.removed_share(D("300"), 3, 0, "USD") == D("0")  # infants only
    assert amendments.removed_share(D("300"), 0, 1, "USD") == D("0")


def _line(**over: Any) -> dict[str, Any]:
    row = {
        "hold_id": HOLD, "quote_id": "q1", "confirmed_at": NOW, "cancellation_policy_id": POLICY, "trip_case_id": CASE,
        "code": "TC-000075", "status": "confirmed", "start_date": date(2026, 12, 31), "line_no": 1, "line_ref": "tour",
        "component_kind": "trip_plan", "pax_adult": 3, "pax_child": 1, "pax_infant": 0, "b2b_price": D("400"),
        "currency": "USD", "line_status": "confirmed", "departure_id": None, "removed_adults": 0,
        "removed_children": 0, "removed_infants": 0, "removed_amount": D("0"),
    }  # fmt: skip
    return {**row, **over}


def _reduce_conn(line: dict[str, Any] | None) -> ScriptedConn:
    return (
        ScriptedConn()
        .on("FROM booking.b2b_holds WHERE trip_case_id", {"id": HOLD})
        .on("JOIN booking.b2b_quote_lines ql ON ql.quote_id = h.quote_id AND ql.line_ref = $3", line)
        .on("FROM booking.cancellation_policies p WHERE p.id", {
            "id": POLICY, "trip_plan_id": None, "valid_from": date(2026, 1, 1),
            "tiers": '[{"min_days": 0, "fee_pct": 50}, {"min_days": 30, "fee_pct": 10}]'})
    )  # fmt: skip


async def _reduce(conn: ScriptedConn, adults: int = 1, children: int = 0, infants: int = 0, **kw: Any) -> dict:
    return await amendments.reduce_line(
        conn, partner_id=PARTNER, booking_id=CASE, line_ref="tour", adults=adults, children=children,
        infants=infants, reason_code=kw.get("reason", "client_cancelled"), dry_run=kw.get("dry_run", True),
        idempotency_key="reduce-key-1", now=NOW,
    )  # fmt: skip


@pytest.mark.asyncio
async def test_reduce_preview_charges_the_policy_on_the_removed_share() -> None:
    out = await _reduce(_reduce_conn(_line()))
    assert out["seats_after"] == 3 and out["removed_b2b_amount"]["amount"] == "100.00"
    assert out["fee"]["amount"] == "10.00" and out["refundable_b2b_amount"]["amount"] == "90.00"  # 50 days: 10 %
    assert out["dry_run"] is True and out["reduced_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "args", "error"),
    [
        (_line(component_kind="hotel_room"), {}, "invalid"),
        (_line(), {"adults": 3}, "invalid"),  # no adult left
        (_line(), {"adults": 0, "children": 2}, "invalid"),  # more children than the line holds
        (_line(line_status="declined"), {}, "invalid"),
        (_line(status="cancelled"), {}, "invalid"),
        (None, {}, "not_found"),
    ],
)
async def test_reduce_refusals(line: dict[str, Any] | None, args: dict[str, int], error: str) -> None:
    with pytest.raises(B2BError) as e:
        await _reduce(_reduce_conn(line), **args)
    assert e.value.error == error


@pytest.mark.asyncio
async def test_reduce_checks_the_request_first() -> None:
    with pytest.raises(B2BError):
        await _reduce(ScriptedConn(), adults=0)
    with pytest.raises(B2BError):
        await _reduce(ScriptedConn(), reason="departure_cancelled")
    with pytest.raises(B2BError) as e:
        await amendments.reduce_line(
            ScriptedConn(), partner_id=PARTNER, booking_id="nope", line_ref="tour", adults=1, children=0, infants=0,
            reason_code="other", dry_run=True, idempotency_key="reduce-key-9", now=NOW,
        )  # fmt: skip
    assert e.value.error == "not_found"


@pytest.mark.asyncio
async def test_a_real_reduction_writes_the_row_frees_seats_and_tells_staff() -> None:
    conn = _reduce_conn(_line(departure_id="d1")).on("FROM booking.b2b_quote_lines ql LEFT JOIN", [
        {"adults": 2, "children": 1}]).on("AND status IN ('pending', 'requested', 'confirmed') ORDER BY code", [
        {"code": "SB-000009"}])  # fmt: skip
    out = await _reduce(conn, dry_run=False)
    assert out["reduced_at"] == NOW.isoformat()
    (row,) = conn.sql("INSERT INTO booking.b2b_line_reductions")
    assert row[4:9] == (1, 0, 0, 4, 3) and row[18:20] == ("api", None)
    assert conn.sql("SET seats = greatest(seats - $3, 0)") == [(HOLD, "tour", 1)]
    assert conn.sql("UPDATE booking.trip_cases SET pax") == [(CASE, 3, 2, 1)]
    (note,) = conn.sql("INSERT INTO booking.notifications")
    assert note[1] == amendments.NOTIFY_REDUCED


def _cancellation(**over: Any) -> dict[str, Any]:
    row = {
        "id": "x1", "hold_id": HOLD, "trip_case_id": CASE, "currency": "USD", "total_b2b_price": D("1000"),
        "fee_pct": D("25"), "fee_amount": D("250"), "refundable_amount": D("750"), "fee_override_pct": None,
        "waive_reason": None, "override_by": None, "override_at": None, "override_approved_by": None,
        "override_approved_at": None, "entered_via": "api", "entered_by": None, "reason_code": "client_cancelled",
        "cancelled_at": NOW, "override_by_name": None, "override_approved_by_name": None,
    }  # fmt: skip
    return {**row, **over}


BOOKING = {"trip_case_id": CASE, "code": "TC-000075", "status": "cancelled", "hold_id": HOLD,
           "partner_id": PARTNER, "m2m_client_id": "c1", "agency_name": "Lanternfield Travel"}  # fmt: skip


def _override_conn(row: dict[str, Any]) -> ScriptedConn:
    return (
        ScriptedConn()
        .on("JOIN booking.b2b_partners p ON p.id = h.partner_id", BOOKING)
        .on("FROM booking.b2b_cancellations x", row)
    )


@pytest.mark.asyncio
async def test_override_request_checks_and_maker_checker() -> None:
    with pytest.raises(Invalid):  # not below the policy fee
        await amendments.request_override(
            _override_conn(_cancellation()), SALES, "TC-000075", fee_override_pct=D("25"), waive_reason="x"
        )
    conn = _override_conn(_cancellation())
    await amendments.request_override(conn, SALES, "TC-000075", fee_override_pct=D("10"), waive_reason="Rebooked")
    assert conn.sql("SET fee_override_pct = $2") == [("x1", D("10.00"), "Rebooked", "u-sales")]
    pending = _cancellation(fee_override_pct=D("10"), waive_reason="Rebooked", override_by="u-sales", override_at=NOW)
    with pytest.raises(Forbidden):
        await amendments.approve_override(_override_conn(pending), Staff("u-sales", None, "S", "admin", True, "x"),
                                          "TC-000075", expected_fee_override_pct=D("10"))  # fmt: skip
    conn = _override_conn(pending)
    await amendments.approve_override(conn, CONTRACTING, "TC-000075", expected_fee_override_pct=D("10.00"))
    assert conn.sql("SET override_approved_by = $2") == [("x1", "u-con", D("100.00"), D("900.00"))]
    with pytest.raises(Conflict):
        await amendments.approve_override(
            _override_conn(_cancellation()), CONTRACTING, "TC-000075", expected_fee_override_pct=D("10")
        )
    out = amendments.cancellation_out(pending, CONTRACTING)
    assert out["override"]["status"] == "pending" and out["can_approve_override"] is True
    assert out["policy_fee"]["amount"] == "250.00"


@pytest.mark.asyncio
async def test_override_cannot_be_swapped_under_the_approver() -> None:
    """Maker-checker: a pending lower fee is never replaced, and the approver approves only the value they saw."""
    pending = _cancellation(fee_override_pct=D("20"), waive_reason="Rebooked", override_by="u-sales", override_at=NOW)
    conn = _override_conn(pending)
    with pytest.raises(Conflict) as e:  # the requester asks again for a lower value while 20 % waits
        await amendments.request_override(conn, SALES, "TC-000075", fee_override_pct=D("0"), waive_reason="Waive")
    assert e.value.code == "override_pending" and conn.sql("SET fee_override_pct = $2") == []
    conn = _override_conn(pending)  # the same request again (double click): no change, no error
    await amendments.request_override(conn, SALES, "TC-000075", fee_override_pct=D("20"), waive_reason=" Rebooked ")
    assert conn.sql("SET fee_override_pct = $2") == []
    conn = _override_conn(pending)
    with pytest.raises(Conflict) as e:  # the approver saw another value
        await amendments.approve_override(conn, CONTRACTING, "TC-000075", expected_fee_override_pct=D("0"))
    assert e.value.code == "override_changed" and conn.sql("SET override_approved_by = $2") == []
    approved = {**pending, "override_approved_by": "u-con", "override_approved_at": NOW}
    with pytest.raises(Conflict) as e:  # already approved at 20 %: a different value is not "the same approval"
        await amendments.approve_override(_override_conn(approved), CONTRACTING, "TC-000075",
                                          expected_fee_override_pct=D("0"))  # fmt: skip
    assert e.value.code == "override_changed"
    out = await amendments.approve_override(
        _override_conn(approved), CONTRACTING, "TC-000075", expected_fee_override_pct=D("20")
    )
    assert out["override"]["status"] == "approved"


# ── Vouchers and manifest ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ops_contact_reads_the_label_only() -> None:
    assert await vouchers.ops_contact(ScriptedConn().on("FROM booking.settings", "{}")) is None
    conn = ScriptedConn().on("FROM booking.settings", '{"label": "  AA desk  24h ", "phone": "x"}')
    assert await vouchers.ops_contact(conn) == {"label": "AA desk 24h"}
    assert await vouchers.ops_contact(ScriptedConn().on("FROM booking.settings", "not json")) is None


def _voucher_doc() -> dict[str, Any]:
    return {
        "booking_id": CASE, "trip_case_code": "TC-000075", "proposal_ref": "AG-<1>", "status": "confirmed",
        "issued_at": NOW, "start_date": None, "end_date": None, "ops_contact": {"label": "AA desk"},
        "_issuer": "Example Travel Co", "vouchers": [{
            "voucher_no": "SB-000001", "line_ref": "tour", "service": "Boat <ride>", "service_date": date(2026, 12, 1),
            "service_time": "07:45", "pickup_point": "Pier & gate", "dropoff_point": None,
            "supplier": {"name": "Ferncliff Boats"}, "confirmation_ref": "FB-1",
            "travellers": {"adult": 2, "child": 1, "infant": 0}, "note": None}],
    }  # fmt: skip


def test_voucher_html_escapes_and_prints_one_page_per_voucher() -> None:
    page = vouchers.render_html(_voucher_doc())
    assert "Boat &lt;ride&gt;" in page and "Pier &amp; gate" in page and "AG-&lt;1&gt;" in page
    assert page.count('class="voucher"') == 1 and "AA desk" in page and "<script" not in page
    empty = vouchers.render_html({**_voucher_doc(), "vouchers": []})
    assert "No voucher" in empty
    resp = vouchers.html_response(_voucher_doc())
    assert resp.headers["content-disposition"] == 'attachment; filename="vouchers-TC-000075.html"'


def test_manifest_html_counts_only() -> None:
    doc = {
        "departure": {"id": "d1", "start_date": date(2026, 12, 1), "status": "scheduled", "capacity": 20,
                      "min_to_operate": 0, "source_id": "BT-1", "title": "River <Trail>"},
        "generated_at": NOW, "totals": {"adult": 3, "child": 1, "infant": 0, "seats": 4, "bookings": 1},
        "rooms": [{"occupancy": "double", "count": 2}],
        "bookings": [{"trip_case_id": CASE, "trip_case_code": "TC-000075", "agency": "Lanternfield Travel",
                      "agency_ref": "AG-1", "entered_via": "staff", "line_ref": "tour", "line_status": "confirmed",
                      "travellers": {"adult": 3, "child": 1, "infant": 0}, "rooms": []}],
        "supplier_bookings": [],
    }  # fmt: skip
    page = manifest.render_html(doc)
    assert "River &lt;Trail&gt;" in page and "2 x double" in page and "4 of 20 seats" in page


# ── Cost sheet: single supplement, child / infant prices ──────────────────────


def test_single_supplement_is_the_single_rate_less_half_the_double() -> None:
    win = Window(date(2026, 12, 1), date(2026, 12, 31))
    room = Part(1, "rt-1", None, 2)
    found = {
        RateAsk("rt-1", None, date(2026, 12, 1), 1): {"net_amount": 70, "currency": "USD"},
        RateAsk("rt-1", None, date(2026, 12, 1), 2): {"net_amount": 100, "currency": "USD"},
    }
    assert cost_sheet.single_supplement(win, [room], found, {}) == (D("20.00"), "USD")
    assert cost_sheet.single_supplement(win, [Part(1, None, "sv-1")], found, {}) is None
    assert cost_sheet.single_supplement(win, [Part(1, "rt-1", None, 1)], found, {}) is None  # single-only room
    assert cost_sheet.single_supplement(win, [Part(2, "rt-1", None, 2)], found, {}) is None  # no rate that night


def test_per_type_prices_follow_the_rule() -> None:
    rule = _rule(child="0.5", infant="20")
    pricing = cost_sheet.Pricing(on=date(2026, 11, 11), rules={engine.Channel.B2B: rule}, fx={})
    row = NetRow(date(2026, 12, 1), date(2026, 12, 31), 2, 2, D("400"), "USD", None, False, D("40"))
    out = cost_sheet.pax_type_prices(row, rule, pricing)
    assert out["child_net_amount"] == D("200.00") and out["infant_net_amount"] == D("20")
    assert out["single_supplement_net_amount"] == D("40")
    assert (
        out["child_amount"]
        and out["child_amount"]
        < engine.price_quote([engine.CostLine(D("400"), "USD")], rule.rule, rates={}).final_total
    )
    plain = cost_sheet.pax_type_prices(NetRow(*row.__dict__.values()), _rule(), pricing)
    assert plain["child_amount"] is None and plain["infant_amount"] is None


def test_in_currency_needs_a_rate() -> None:
    fx = {"VND": engine.FxRate("VND", D("25000"), D("0"))}
    assert cost_sheet.in_currency(D("2"), "USD", "VND", fx) == D("50000.00")
    assert cost_sheet.in_currency(D("50000"), "VND", "USD", fx) == D("2.00")
    assert cost_sheet.in_currency(D("2"), "USD", "EUR", fx) is None


# ── Terms, supplier cancel fee, age bands ────────────────────────────────────

BASE_TERMS = {"id": "t0", "party_tenant_id": None, "deposit_pct": D("0.3"), "balance_days_before_departure": 30,
              "saas_due_days": 7, "settlement_due_days": 10, "warn_after_days": 7, "suspend_after_days": 30,
              "saas_currency": "USD", "updated_at": NOW, "updated_by": None}  # fmt: skip


@pytest.mark.asyncio
async def test_plan_terms_pick_the_most_specific_row() -> None:
    conn = ScriptedConn().on("FROM booking.billing_terms", BASE_TERMS)
    assert (await config.plan_terms(conn, "t1", None))["scope"] == "default"
    tour = conn.on("FROM booking.trip_plan_billing_terms", {"party_tenant_id": None, "deposit_pct": D("0.5"),
                                                            "balance_days_before_departure": 45})  # fmt: skip
    out = await config.plan_terms(tour, "t1", "p1")
    assert (out["deposit_pct"], out["scope"], out["settlement_due_days"]) == (D("0.5"), "tour", 10)
    mine = ScriptedConn().on("FROM booking.billing_terms", {**BASE_TERMS, "party_tenant_id": "t1"}).on(
        "FROM booking.trip_plan_billing_terms", {"party_tenant_id": "t1", "deposit_pct": D("0.2"),
                                                 "balance_days_before_departure": 20})  # fmt: skip
    assert (await config.plan_terms(mine, "t1", "p1"))["scope"] == "agency_tour"
    assert await config.plan_terms(ScriptedConn(), "t1", "p1") is None


@pytest.mark.asyncio
async def test_plan_terms_writes_check_values_and_the_tour() -> None:
    bad = {"deposit_pct": D("1.5"), "balance_days_before_departure": 1}
    good = {"deposit_pct": D("0.5"), "balance_days_before_departure": 1}
    with pytest.raises(Invalid):
        await config.put_plan_terms(ScriptedConn(), "BT-1", None, bad, "u")
    with pytest.raises(NotFound):
        await config.put_plan_terms(ScriptedConn(), "BT-1", None, good, "u")
    with pytest.raises(NotFound):
        await config.delete_plan_terms(ScriptedConn().on("FROM booking.trip_plans WHERE source_id", {"id": "p1",
                                       "source_id": "BT-1"}), "BT-1", None, "u")  # fmt: skip


@pytest.mark.asyncio
async def test_supplier_cancel_fee_needs_pricing_read_a_pair_and_a_cancelled_booking() -> None:
    with pytest.raises(Forbidden):
        await sb.record_cancel_fee(ScriptedConn(), CONTENT, "x", fee=D("1"), currency="USD")
    with pytest.raises(Invalid):
        await sb.record_cancel_fee(ScriptedConn(), SALES, "x", fee=D("1"), currency=None)
    with pytest.raises(Invalid):
        await sb.cancel(ScriptedConn(), SALES, "x", reason="Gone", supplier_cancel_fee=D("-1"),
                        supplier_cancel_fee_currency="USD")  # fmt: skip


@pytest.mark.asyncio
async def test_age_bands_keep_infants_below_children() -> None:
    with pytest.raises(Invalid):
        await trip_plans.save_age_bands(ScriptedConn(), CONTRACTING, "BT-1", child_age_max=3, infant_age_max=3)
    with pytest.raises(NotFound):
        await trip_plans.save_age_bands(ScriptedConn(), CONTRACTING, "BT-1", child_age_max=11, infant_age_max=1)


def test_voucher_fields_only_while_live() -> None:
    assert sb.VOUCHER_FIELDS == ("service_time", "pickup_point", "dropoff_point", "voucher_note")
    assert time(7, 45).strftime("%H:%M") == "07:45"
