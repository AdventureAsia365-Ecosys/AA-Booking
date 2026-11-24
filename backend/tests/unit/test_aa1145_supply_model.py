"""AA-1145 (bk-060): the supply model without a database.

The availability engine's precedence (stop_sale > allotment / free_sale > on_request), the child price rule B4, the
contract terms (tiers, payment, offers override), the B7 net of a commissionable price, the rate-sheet columns, the
quote's stop-sale step, the B2B line pricing of children, and the stop-sale permission. The HTTP flow, the DB guards
and the issue's done-when run on PostgreSQL in ``tests/integration/test_aa1145_supply_model.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest

from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS
from app.b2b import components, departures
from app.errors import Invalid
from app.pricing import policy_params, policy_values
from app.supply import availability, child_price, contract_terms, rate_sheet, rates, restrictions
from app.supply.restrictions import Restriction, evaluate
from tests.unit.fakes import ScriptedConn, policy_rows

DAY = date(2027, 12, 24)
TP = "tp:00000000-0000-4000-8000-0000000011a6"
TODAY = date(2027, 11, 1)


def _r(kind: str, rid: str, start: date = date(2027, 12, 1), end: date = date(2027, 12, 31), **kw: Any) -> Restriction:
    status = kw.pop("status", "pending" if kind == "stop_sale" else "contract")
    return Restriction(id=rid, kind=kind, date_from=start, date_to=end, status=status, **kw)


# ── Engine ───────────────────────────────────────────────────────────────────


def test_no_row_is_on_request() -> None:
    assert evaluate([], DAY, pax=2, today=TODAY) == restrictions.Verdict("on_request", "no_terms")


def test_stop_sale_wins_over_free_sale_and_allotment() -> None:
    rows = [_r("free_sale", "f1"), _r("allotment", "a1", units=5, release_days=7), _r("stop_sale", "s1", DAY, DAY)]
    assert evaluate(rows, DAY, pax=2, today=TODAY) == restrictions.Verdict("on_request", "stop_sale", "s1")
    assert evaluate(rows, date(2027, 12, 23), pax=2, today=TODAY).status == "available"


def test_a_stop_sale_counts_pending_or_approved_never_closed() -> None:
    free = _r("free_sale", "f1")
    for status, ended, effect in (
        ("pending", False, True),
        ("approved", False, True),
        ("approved", True, False),
        ("rejected", False, False),
        ("withdrawn", False, False),
    ):
        stop = _r("stop_sale", "s1", DAY, DAY, status=status, ended=ended)
        verdict = evaluate([free, stop], DAY, pax=2, today=TODAY)
        assert (verdict.reason == "stop_sale") is effect, status


def test_allotment_units_release_and_free_sale_limit() -> None:
    allot = _r("allotment", "a1", units=2, release_days=7)
    assert evaluate([allot], DAY, pax=2, units=2, today=TODAY).reason == "allotment"
    assert evaluate([allot], DAY, pax=2, units=1, used=1, today=TODAY).reason == "allotment"
    assert evaluate([allot], DAY, pax=2, units=1, used=2, today=TODAY).reason == "allotment_full"
    # Released: today is past date - release days (17/12 for 24/12).
    assert evaluate([allot], DAY, pax=2, today=date(2027, 12, 17)).reason == "allotment"
    assert evaluate([allot], DAY, pax=2, today=date(2027, 12, 18)).reason == "released"
    # Free sale after an exhausted allotment; over its limit = on request.
    free = _r("free_sale", "f1", max_pax=8)
    assert evaluate([allot, free], DAY, pax=8, units=3, today=TODAY).reason == "free_sale"
    assert evaluate([free], DAY, pax=9, today=TODAY) == restrictions.Verdict("on_request", "over_max_pax")
    assert evaluate([_r("on_request", "o1")], DAY, pax=2, today=TODAY).reason == "on_request"
    # Outside the row's dates nothing applies.
    assert evaluate([free], date(2028, 1, 1), pax=2, today=TODAY).reason == "no_terms"


def test_quote_step_puts_an_available_line_on_request_on_a_stop_sale() -> None:
    stop = restrictions.Verdict("on_request", "stop_sale", "s1")
    free = restrictions.Verdict("available", "free_sale", "f1")
    days = [date(2027, 12, 23), DAY]
    line = availability.line_availability
    assert line(departures.AVAILABLE, TP, [free, free], days) == (departures.AVAILABLE, None)
    assert line(departures.AVAILABLE, TP, [free, stop], days) == (departures.ON_REQUEST, {"stop_sale": ["2027-12-24"]})
    assert line(departures.SOLD_OUT, TP, [stop, stop], days)[0] == departures.SOLD_OUT


def test_component_ids() -> None:
    rid = "00000000-0000-4000-8000-000000001145"
    assert restrictions.parse_component(f"rt:{rid}") == (rid, None)
    assert restrictions.parse_component(f"sv:{rid}") == (None, rid)
    for bad in ("tp:" + rid, "sv:nope", ""):
        with pytest.raises(Invalid):
            restrictions.parse_component(bad)


# ── Child price B4 ───────────────────────────────────────────────────────────


def test_child_price_order() -> None:
    adult = D("40.00")
    rate = child_price.child_net(adult, child_rate_net=D("15"), child_rate_age_max=11, contract_pct=D("50"))
    assert (rate.net, rate.basis, rate.note()) == (D("15"), "supplier_rate", {"basis": "supplier_rate", "age_max": 11})
    contract = child_price.child_net(adult, contract_pct=D("50"), policy_pct=D("70"))
    assert (contract.net, contract.note()) == (D("20.00"), {"basis": "contract_pct", "pct": "50"})
    policy = child_price.child_net(adult, policy_pct=D("75"))
    assert (policy.net, policy.basis) == (D("30.00"), "policy_pct")
    # 100 % (the seed) = the adult price with an age note.
    for pct in (D("100"), None):
        same = child_price.child_net(adult, policy_pct=pct, child_age_max=11)
        assert (same.net, same.note()) == (adult, {"basis": "adult_price", "age_max": 11})
    assert child_price.child_net(adult, contract_pct=D("0")).net == D("0.00")


def _service_conn(*, child_rate: dict[str, Any] | None, contract_pct: str | None) -> ScriptedConn:
    adult = {"id": "r-adult", "unit": "person", "net_amount": "40.00", "currency": "USD", "season": None,
             "contract_id": "k-1", "age_max": None}  # fmt: skip

    def rates_answer(*args: Any) -> list[dict[str, Any]]:
        out = []
        for n, pax_type in enumerate(args[4], start=1):
            found = {"adult": adult, "child": child_rate, "infant": None}[pax_type]
            out.append({"n": n, **(found or {"id": None})})
        return out

    conn = ScriptedConn()
    conn.on("FROM booking.services WHERE id = ANY", [{"id": SERVICE, "status": "active", "service_type": "activity"}])
    conn.on("AS a(room_type_id, service_id, on_day, pax, pax_type, meal_plan, supplier_id, n)", rates_answer)
    conn.on(
        "FROM booking.supplier_contract_terms",
        [{"contract_id": "k-1", "child_pct": D(contract_pct)}] if contract_pct else [],
    )
    return conn


SERVICE = "00000000-0000-4000-8000-0000000011a5"
ASK = components.LineAsk("l1", f"sv:{SERVICE}", DAY, None, adult=2, child=1, infant=1)


@pytest.mark.asyncio
async def test_sv_line_children_follow_b4() -> None:
    # Contract 50 %: 2 x 40 + 1 x 20 (the infant has no rate: free).
    [cost] = await components.price_lines(_service_conn(child_rate=None, contract_pct="50"), [ASK])
    assert cost.costs == [(D("100.00"), "USD")] and cost.child == {"basis": "contract_pct", "pct": "50"}
    assert cost.supply == [restrictions.SupplyAsk(None, SERVICE, DAY, 3, units=3)]
    # A supplier child rate wins over the contract %.
    child = {"id": "r-child", "unit": "person", "net_amount": "12.00", "currency": "USD", "season": None,
             "contract_id": "k-1", "age_max": 11}  # fmt: skip
    [cost] = await components.price_lines(_service_conn(child_rate=child, contract_pct="50"), [ASK])
    assert cost.costs == [(D("92.00"), "USD")] and cost.rate_ids == ["r-adult", "r-child"]
    # Neither: the policy % of the service type, else the adult price with an age note.
    conn = _service_conn(child_rate=None, contract_pct=None)
    [cost] = await components.price_lines(conn, [ASK], child_pct_by_type={"activity": D("60")}, child_age_max=11)
    assert cost.costs == [(D("104.00"), "USD")] and cost.child == {"basis": "policy_pct", "pct": "60"}
    [cost] = await components.price_lines(conn, [ASK], child_pct_by_type={"activity": D("100")}, child_age_max=11)
    assert cost.costs == [(D("120.00"), "USD")] and cost.child == {"basis": "adult_price", "age_max": 11}


@pytest.mark.asyncio
async def test_adults_only_sv_line_prices_as_before() -> None:
    conn = _service_conn(child_rate=None, contract_pct="50")
    [cost] = await components.price_lines(conn, [components.LineAsk("l1", f"sv:{SERVICE}", DAY, None, 2, 0, 0)])
    assert cost.costs == [(D("80.00"), "USD")] and cost.child is None
    (rate_args,) = conn.sql("AS a(room_type_id, service_id, on_day, pax, pax_type, meal_plan, supplier_id, n)")
    assert rate_args[4] == ["adult"]  # only the adult rate is looked up
    assert conn.sql("FROM booking.supplier_contract_terms") == []


def test_policy_parameter_and_accessor() -> None:
    param = policy_params.PARAMS["child_price_pct_by_service_type"]
    assert param.choices == policy_params.SERVICE_TYPES and param.group == policy_params.PRICING
    full = dict.fromkeys(policy_params.SERVICE_TYPES, "50")
    assert policy_params.normalise(param.key, full)["activity"] == "50"
    for bad in ({"activity": "50"}, {**full, "guide": "101"}):
        with pytest.raises(policy_params.PolicyValueError):
            policy_params.normalise(param.key, bad)
    policy = policy_values.from_rows(DAY, policy_rows())
    assert policy.child_price_pct_by_service_type["activity"] == D("100")


# ── Contract terms ───────────────────────────────────────────────────────────


TIERS = [{"min_days": 0, "fee_pct": "100"}, {"min_days": 30, "fee_pct": 0}, {"min_days": 15, "fee_pct": "50"}]


def test_terms_parse_tiers_and_payment() -> None:
    terms = contract_terms.parse(
        {
            "cancellation_tiers": TIERS,
            "deposit_pct": "30",
            "deposit_due_days": 7,
            "balance_due_days_before": 21,
            "child_pct": "50",
        }  # fmt: skip
    )
    assert [t.min_days for t in terms.cancellation_tiers or ()] == [30, 15, 0]
    assert (terms.fee_pct(40), terms.fee_pct(20), terms.fee_pct(3)) == (D("0.00"), D("50.00"), D("100.00"))
    assert terms.out()["child_pct"] == "50.00" and terms.out()["cancellation_tiers"][0] == {
        "min_days": 30,
        "fee_pct": "0.00",
    }
    assert contract_terms.EMPTY.fee_pct(3) is None
    for body, code in (
        ({"deposit_pct": "30"}, "deposit_pair"),
        ({"balance_due_days_before": 10, "credit_days_after": 30}, "balance_or_credit"),
        ({"deposit_pct": "0", "deposit_due_days": 1}, "invalid_deposit_pct"),
        ({"child_pct": "100.001"}, "invalid_child_pct"),
        ({"credit_days_after": 400}, "invalid_credit_days_after"),
        ({"cancellation_tiers": [{"min_days": 5, "fee_pct": 10}]}, "invalid_cancellation_tiers"),
        ({"surprise": 1}, "not_editable"),
    ):
        with pytest.raises(Invalid) as err:
            contract_terms.parse(body)
        assert err.value.code == code, body


def test_offer_overrides_only_what_it_sets() -> None:
    contract = contract_terms.parse({"cancellation_tiers": TIERS, "child_pct": "50", "credit_days_after": 30})
    offer = contract_terms.Terms(child_pct=D("0"))
    merged = contract_terms.effective(contract, offer)
    assert merged.child_pct == D("0") and merged.cancellation_tiers == contract.cancellation_tiers
    assert merged.credit_days_after == 30 and contract_terms.effective(contract) is contract


# ── Rates: B7 net and pax types ──────────────────────────────────────────────


def test_net_from_published() -> None:
    assert rates.net_from_published(D("70"), D("20")) == D("56.00")
    assert rates.net_from_published(D("33.33"), D("12.5")) == D("29.16")  # 29.163... half-up
    assert rates.commissionable({"net_amount": D("10")}) == {"net_amount": D("10")}
    out = rates.commissionable({"published_amount": "70", "commission_pct": "20"})
    assert out["net_amount"] == D("56.00") and out["published_amount"] == D("70")
    for body, code in (
        ({"published_amount": "70"}, "commission_pair"),
        ({"published_amount": "70", "commission_pct": "100"}, "invalid_commission_pct"),
        ({"published_amount": "70", "commission_pct": "20", "net_amount": "50"}, "net_mismatch"),
    ):
        with pytest.raises(Invalid) as err:
            rates.commissionable(body)
        assert err.value.code == code


def test_pax_type_rules() -> None:
    ok = {"unit": "person", "pax_type": "child", "age_min": 4, "age_max": 11}
    rates._check_pax_type(ok)
    for merged, code in (
        ({**ok, "unit": "room_night"}, "pax_type_per_person"),
        ({**ok, "pax_type": "senior"}, "unknown_pax_type"),
        ({"unit": "person", "pax_type": "adult", "age_max": 11}, "age_band_adult"),
        ({**ok, "age_max": 18}, "invalid_age_band"),
        ({**ok, "age_min": 12}, "invalid_age_band"),
    ):
        with pytest.raises(Invalid) as err:
            rates._check_pax_type(merged)
        assert err.value.code == code


# ── Rate sheet: Published + Commission % and pax type ────────────────────────

CONTRACT = {"Title": "Invented Bay 2027", "Valid from": "2027-01-01", "Valid to": "2027-12-31", "Currency": "USD"}


def test_rate_sheet_commission_and_pax_columns() -> None:
    data = rate_sheet.build_workbook(
        CONTRACT,
        hotel_rows=[("HTL-900001", "Garden", None, None, None, "BB", None, None, None, None, 70, 20)],
        service_rows={
            "Activity": [
                ("SVC-900002", None, None, None, None, None, 40, None, None, None, None),
                ("SVC-900002", None, None, None, None, None, 15, None, "Child", 4, 11),
            ]
        },
    )
    sheet = rate_sheet.parse(data)
    assert sheet.problems == []
    hotel, adult, child = sheet.rows
    assert (hotel.values["net_amount"], hotel.values["published_amount"], hotel.values["commission_pct"]) == (
        D("56.00"),
        D("70"),
        D("20"),
    )
    assert "pax_type" not in adult.values
    assert (child.values["pax_type"], child.values["age_min"], child.values["age_max"]) == ("child", 4, 11)


def test_rate_sheet_commission_problems() -> None:
    data = rate_sheet.build_workbook(
        CONTRACT,
        hotel_rows=[
            ("HTL-900001", "Garden", None, None, None, None, None, None, 50, None, 70, 20),
            ("HTL-900001", "Garden", None, None, None, None, None, None, None, None, 70, None),
            ("HTL-900001", "Garden", None, None, None, None, None, None, None, None, 70, 100),
        ],
        service_rows={"Activity": [("SVC-900002", None, None, None, None, None, 40, None, "teen", 20, None)]},
    )
    found = {(p.row, p.column, p.code) for p in rate_sheet.parse(data).problems if p.sheet != "Contract"}
    assert {(2, "Net", "net_mismatch"), (3, "Commission %", "commission_pair"),
            (4, "Commission %", "invalid_value")} <= found  # fmt: skip
    assert {(2, "Pax type", "invalid_value"), (2, "Age min", "invalid_value")} <= found


# ── Permission ───────────────────────────────────────────────────────────────


def test_stop_sale_permission_is_contracting_and_admin() -> None:
    assert PERMISSIONS["stop_sale.write"] == frozenset({"admin", "contracting"})
    assert "sales" not in PERMISSIONS["stop_sale.write"]
    assert "stop_sale.write" not in PREVIEW_PERMISSIONS


def test_season_stop_sale_counts_while_pending() -> None:
    """T13 review: a pending stop-sale range of a season calendar already takes effect; a pending season does not."""
    from app.pricing import seasons

    sql = " ".join(seasons.RANGES_SQL.split())
    assert "r.ended_at IS NULL" in sql
    assert "(r.status = 'approved' OR (r.kind = 'stop_sale' AND r.status = 'pending'))" in sql
