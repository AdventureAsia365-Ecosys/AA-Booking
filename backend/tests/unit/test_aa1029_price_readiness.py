"""AA-1029 price readiness, the pure part: the status of a plan (every reason in the cost sheet's order, hand rows win,
an expired hand row counts for nothing, a non-USD rate without an FX rate), the horizon of a rate, the summary, the
filters, the staff CSV and a rate sheet's draft spans; the cost sheet probe with a stub connection."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any

import pytest

from app.catalog import cost_sheet, price_readiness
from app.catalog.price_readiness import Component, Plan

ON = date(2027, 3, 1)
ROOM, GUIDE, BOAT = "room-1", "svc-guide", "svc-boat"


def _room(active: bool = True) -> Component:
    return Component(1, ROOM, None, active, "HTL-000001", "Garden Twin", None, "SUP-000001")


def _service(item: str = GUIDE, code: str = "SVC-000001") -> Component:
    return Component(2, None, item, True, None, None, code, "SUP-000002")


def _plan(*components: Component, computed: int = 0, hand: int = 0, expired: int = 0) -> Plan:
    return Plan(
        plan_id="p-1",
        source_id="aa000000-0000-4000-8000-000000000001",
        sellable=True,
        title="Invented Ridge Trek",
        country="Vietnam",
        duration_days=3,
        in_catalog=True,
        components=list(components),
        computed_rows=computed,
        hand_rows=hand,
        hand_rows_expired=expired,
    )


USD_RATES = {ROOM: {"USD"}, GUIDE: {"USD"}}


def _status(plan: Plan, rated: dict[str, set[str]] = USD_RATES, *, rule: bool = True, fx: tuple[str, ...] = ()) -> Any:
    return price_readiness.classify(plan, rated, has_rule=rule, fx=fx)


def test_every_reason_in_the_cost_sheet_order() -> None:
    assert _status(_plan()).status == "no_components"
    assert _status(_plan(_room(active=False), _service())).status == "inactive_component"
    assert _status(_plan(_room(), _service()), rule=False).status == "no_rule"
    # an inactive component is reported before a missing rule, a missing rule before missing rates
    assert _status(_plan(_room(active=False)), {}, rule=False).status == "inactive_component"
    assert _status(_plan(_room()), {}, rule=False).status == "no_rule"
    missing = _status(_plan(_room(), _service(BOAT, "SVC-000009")))
    assert (missing.status, missing.reason) == ("missing_rates", "missing_rates")
    assert [c.out() for c in missing.unrated] == [
        {"day_no": 2, "kind": "service", "code": "SVC-000009", "room_type": None, "supplier_code": "SUP-000002"}
    ]
    assert missing.missing_fx == ()


def test_the_reason_words_are_the_cost_sheet_ones() -> None:
    assert set(price_readiness.STATUSES) >= {
        cost_sheet.NO_COMPONENTS,
        cost_sheet.INACTIVE_COMPONENT,
        cost_sheet.NO_RULE,
    }
    assert cost_sheet.blocking_reason(0, True, True) == "no_components"
    assert cost_sheet.blocking_reason(2, False, True) == "inactive_component"
    assert cost_sheet.blocking_reason(2, True, False) == "no_rule"
    assert cost_sheet.blocking_reason(2, True, True) is None
    assert cost_sheet.component_active({"status": "active", "hotel_status": "inactive"}) is False


def test_a_non_usd_rate_without_an_fx_rate_is_missing_fx() -> None:
    rated = {ROOM: {"VND"}, GUIDE: {"USD"}}
    verdict = _status(_plan(_room(), _service()), rated)
    assert (verdict.status, verdict.missing_fx, verdict.unrated) == ("missing_fx", ("VND",), ())
    ready = _status(_plan(_room(), _service()), rated, fx=("VND",))
    assert (ready.status, ready.reason) == (None, None)  # nothing stops it: the cost sheet decides
    # a component with rates in two currencies is ready when one of them prices
    assert _status(_plan(_room()), {ROOM: {"VND", "USD"}}).status is None
    assert _status(_plan(_room()), {ROOM: {"VND", "THB"}}).missing_fx == ("THB", "VND")
    # missing rates are reported before missing FX
    assert _status(_plan(_room(), _service(BOAT)), {ROOM: {"VND"}}).status == "missing_rates"


def test_computed_rows_are_priced_and_hand_rows_win() -> None:
    priced = _status(_plan(_room(), computed=12, hand=1))
    assert (priced.status, priced.reason) == ("priced", None)
    hand = _status(_plan(hand=2))
    assert (hand.status, hand.reason) == ("hand_priced", "no_components")
    hand_rated = _status(_plan(_room(), _service(BOAT), hand=1))
    assert (hand_rated.status, hand_rated.reason) == ("hand_priced", "missing_rates")
    # a priced plan whose rule ended still shows the reason until the next recompute removes its rows
    assert _status(_plan(_room(), computed=3), rule=False).reason == "no_rule"


def test_an_expired_hand_row_counts_for_nothing() -> None:
    assert _status(_plan(hand=0, expired=3)).status == "no_components"
    assert _status(_plan(_room(), hand=0, expired=1), {}).status == "missing_rates"


def _span(item: str, start: date, stop: date, currency: str = "USD") -> dict[str, Any]:
    room = item if item.startswith("room") else None
    return {
        "room_type_id": room,
        "service_id": None if room else item,
        "start": start,
        "stop": stop,
        "currency": currency,
    }


def test_a_rate_counts_when_it_covers_a_day_from_today_to_the_horizon() -> None:
    end = price_readiness.horizon(ON)
    assert end == ON + timedelta(days=365)
    spans = [
        _span(ROOM, ON - timedelta(days=30), ON - timedelta(days=1)),  # over
        _span(GUIDE, end + timedelta(days=1), date(2029, 1, 1)),  # starts after the horizon
        _span(BOAT, end, cost_sheet.FOREVER, "VND "),  # starts on the last day: counts, currency stripped
        _span("room-2", ON - timedelta(days=5), ON),  # ends today: counts
    ]
    assert price_readiness.rated_items(spans, ON) == {BOAT: {"VND"}, "room-2": {"USD"}}


def _row(status: str, country: str | None, title: str = "Invented Trek", key: str = "k-1") -> dict[str, Any]:
    return {"status": status, "country": country, "title": title, "tour_key": key}


def test_the_summary_counts_per_status_and_country() -> None:
    rows = [_row("priced", "Vietnam"), _row("no_components", "Vietnam"), _row("no_components", None)]
    out = price_readiness.summary(rows)
    assert out["total"] == 3
    assert out["by_status"] == {
        "priced": 1,
        "hand_priced": 0,
        "missing_rates": 0,
        "missing_fx": 0,
        "no_rule": 0,
        "no_components": 2,
        "inactive_component": 0,
    }
    assert [(c["country"], c["total"], c["no_components"]) for c in out["by_country"]] == [
        ("(no country)", 1, 1),
        ("Vietnam", 2, 1),
    ]


def test_the_filters() -> None:
    row = _row("priced", "Vietnam", "Invented Ridge Trek", "aa10-key")
    match = price_readiness.matches
    assert match(row, status=None, country=None, q=None)
    assert match(row, status="priced", country=" vietnam ", q="RIDGE")
    assert match(row, status=None, country=None, q="aa10")
    assert not match(row, status="no_rule", country=None, q=None)
    assert not match(row, status=None, country="Laos", q=None)
    assert not match(row, status=None, country=None, q="lake")
    assert match(_row("priced", None), status=None, country="(no country)", q=None)


def _item(**over: Any) -> dict[str, Any]:
    base = {
        "tour_key": "aa000000-0000-4000-8000-000000000001",
        "title": "=HYPERLINK(1)",
        "country": "Vietnam",
        "duration_days": 3,
        "sellable": True,
        "status": "missing_rates",
        "reason": "missing_rates",
        "components": 2,
        "components_without_rate": [
            {"day_no": 1, "kind": "hotel", "code": "HTL-000001", "room_type": "Garden Twin", "supplier_code": "SUP-1"},
            {"day_no": 2, "kind": "service", "code": "SVC-000002", "room_type": None, "supplier_code": "SUP-2"},
        ],
        "missing_fx": ["THB", "VND"],
        "computed_rows": 0,
        "hand_rows": 0,
        "from_price": None,
        "note": None,
    }
    return base | over


def test_the_csv_has_a_fixed_header_and_neutralises_formulas() -> None:
    text = price_readiness.to_csv(
        {"items": [_item(), _item(title="Invented Lake", from_price={"amount": "235.00", "currency": "USD"})]}
    )
    lines = text.split("\r\n")
    assert lines[0] == ",".join(price_readiness.CSV_COLUMNS)
    assert price_readiness.CSV_COLUMNS == (
        "tour_key",
        "title",
        "country",
        "duration_days",
        "sellable",
        "status",
        "reason",
        "components",
        "components_without_rate",
        "missing_fx",
        "computed_rows",
        "hand_rows",
        "from_price",
        "from_currency",
        "note",
    )
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["title"] == "'=HYPERLINK(1)"
    assert rows[0]["components_without_rate"] == "day 1 HTL-000001 Garden Twin (SUP-1); day 2 SVC-000002 (SUP-2)"
    assert (rows[0]["missing_fx"], rows[0]["sellable"], rows[0]["from_price"]) == ("THB VND", "yes", "")
    assert (rows[1]["from_price"], rows[1]["from_currency"]) == ("235.00", "USD")
    assert not any("net" in c or "margin" in c for c in price_readiness.CSV_COLUMNS)


def test_a_draft_rate_takes_the_contract_dates() -> None:
    contract = {"currency": "THB", "valid_from": ON, "valid_to": ON + timedelta(days=100)}
    rates = [
        {"hotel_room_type_id": ROOM},  # the contract's dates
        {"service_id": GUIDE, "valid_from": ON + timedelta(days=400)},  # after the horizon
        {"service_id": BOAT, "valid_to": ON - timedelta(days=1)},  # over before today
    ]
    assert price_readiness.draft_rated(contract, rates, ON) == {ROOM: {"THB"}}
    assert price_readiness.draft_rated({**contract, "valid_from": None}, rates, ON) == {}
    open_ended = {"currency": "USD", "valid_from": ON - timedelta(days=10), "valid_to": None}
    assert price_readiness.draft_rated(open_ended, [{"service_id": GUIDE}], ON) == {GUIDE: {"USD"}}


class _Conn:
    """No database: the probe calls ``cost_sheet.compute`` (patched)."""


@pytest.mark.asyncio
async def test_a_plan_only_the_cost_sheet_can_decide_is_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"p-rows": ([{"channel": "b2b"}], {"reason": None}), "p-none": ([], {"reason": "no_rates"})}
    asked: list[str] = []

    async def compute(conn: Any, plan_id: str, pricing: Any) -> tuple[list[Any], dict[str, Any]]:
        asked.append(plan_id)
        return answers.get(plan_id, ([], {"reason": "no_rates"}))

    monkeypatch.setattr(cost_sheet, "compute", compute)
    monkeypatch.setattr(price_readiness, "MAX_PROBES", 2)
    plans = [
        Plan("p-rows", "k-1", True, components=[_room()]),
        Plan("p-none", "k-2", True, components=[_room()]),
        Plan("p-late", "k-3", True, components=[_room()]),
        Plan("p-bare", "k-4", True),
    ]
    pricing = cost_sheet.Pricing(on=ON, rules={cost_sheet.engine.Channel.B2B: object()}, fx={})  # type: ignore[dict-item]
    facts = price_readiness.Facts(ON, pricing, plans, {ROOM: {"USD"}}, [])
    out = {r["tour_key"]: r for r in await price_readiness.resolve(_Conn(), facts, facts.rated)}  # type: ignore[arg-type]
    assert asked == ["p-rows", "p-none"]  # at most MAX_PROBES, never a plan with a reason
    assert (out["k-1"]["status"], out["k-1"]["pending_recompute"], out["k-1"]["note"]) == (
        "priced",
        True,
        price_readiness.NOTE_PENDING,
    )
    assert (out["k-2"]["status"], out["k-2"]["note"]) == ("missing_rates", price_readiness.NOTE_NO_COMMON_WINDOW)
    assert (out["k-3"]["status"], out["k-3"]["note"]) == ("missing_rates", price_readiness.NOTE_NOT_CHECKED)
    assert out["k-4"]["status"] == "no_components" and out["k-4"]["pending_recompute"] is False
