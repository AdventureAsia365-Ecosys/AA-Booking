"""AA-1151 (bk-068): add-on offers without a database: the rule parsing, the T7 sale rules at quote time, the B4
offer child %, the activity catalogue workbook parsing, the contract expiry schedule and the role matrix.

Names and places are invented."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import pytest

from app.auth import custom_roles
from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS
from app.errors import Invalid
from app.jobs import kinds
from app.pricing import policy_params
from app.supply import activity_import, addon_rules, addons, child_price, contract_expiry

RULE = addon_rules.SaleRule(
    offer_id="o-1",
    min_pax=2,
    cutoff_hours=24,
    operating_weekdays=(1, 3, 5),
    start_times=(time(14, 0), time(8, 30)),
)
MONDAY = date(2031, 3, 3)  # a Monday


# ── Rules ────────────────────────────────────────────────────────────────────


def test_weekdays_parse_names_numbers_and_daily() -> None:
    assert addons.parse_weekdays("Mon, wed;FRI") == [1, 3, 5]
    assert addons.parse_weekdays("7,1,1") == [1, 7]
    assert addons.parse_weekdays("daily") == [1, 2, 3, 4, 5, 6, 7]
    assert addons.parse_weekdays([6, 7]) == [6, 7]
    for bad in ([], [0], [8], "Funday", [True]):
        with pytest.raises(Invalid):
            addons.parse_weekdays(bad)


def test_start_times_parse_sorted_and_unique() -> None:
    assert addons.parse_start_times("13:30, 08:00 08:00") == [time(8, 0), time(13, 30)]
    assert addons.parse_start_times(None) == []
    assert addons.parse_start_times([time(9, 15, 30)]) == [time(9, 15)]
    for bad in ("25:00", "8:00", ["noon"]):
        with pytest.raises(Invalid):
            addons.parse_start_times(bad)


def test_normalize_rules_checks_each_field() -> None:
    out = addons.normalize_rules(
        {
            "min_pax": 2,
            "cutoff_hours": 0,
            "child_pct": "50",
            "cancellation_tiers": [{"min_days": 2, "fee_pct": "0"}, {"min_days": 0, "fee_pct": "100"}],
            "featured": True,
        }
    )
    assert out["child_pct"] == Decimal("50.00") and out["cutoff_hours"] == 0 and out["featured"] is True
    assert '"min_days": 0' in out["cancellation_tiers"]
    for bad in (
        {"min_pax": 0},
        {"cutoff_hours": 721},
        {"child_pct": "100.5"},
        {"cancellation_tiers": [{"min_days": 3, "fee_pct": "10"}]},
        {"featured": "yes"},
        {"net_amount": 10},
    ):
        with pytest.raises(Invalid):
            addons.normalize_rules(bad)
    assert addons.normalize_rules({"child_pct": None}) == {"child_pct": None}


def test_places_keep_one_role_per_place() -> None:
    places = addons.normalize_places(
        [{"destination_id": "p-1"}, {"destination_id": "p-2", "role": "pickup"}, {"destination_id": "p-1"}]
    )
    assert places == [{"destination_id": "p-1", "role": "operates"}, {"destination_id": "p-2", "role": "pickup"}]
    with pytest.raises(Invalid):
        addons.normalize_places([{"destination_id": "p-1", "role": "visits"}])
    with pytest.raises(Invalid):
        addons.normalize_places([{"role": "operates"}])


def test_missing_lists_what_stops_a_sale() -> None:
    ready = {"place_count": 1, "has_price": True, "service_status": "active", "supplier_status": "active"}
    assert addons.missing(ready) == []
    assert addons.missing({**ready, "content_published": None}) == []  # CIS read-back off: not counted
    assert addons.missing({**ready, "content_published": False}) == ["no_content"]
    assert addons.missing({**ready, "place_count": 0, "has_price": False}) == ["no_place", "no_price"]


# ── T7 at quote time ─────────────────────────────────────────────────────────


def test_sale_closes_cutoff_hours_before_the_first_departure() -> None:
    assert RULE.closes_at(MONDAY) == datetime(2031, 3, 2, 8, 30)
    flexible = addon_rules.SaleRule("o-2", 1, 12, (1,), ())
    assert flexible.closes_at(MONDAY) == datetime(2031, 3, 2, 12, 0)


def test_line_problem_minimum_weekday_and_cutoff() -> None:
    early = datetime(2031, 3, 1, 9, 0, tzinfo=UTC)
    assert addon_rules.line_problem(RULE, MONDAY, 2, early) is None
    assert addon_rules.line_problem(RULE, MONDAY, 1, early)[0] == "below_min_pax"
    assert addon_rules.line_problem(RULE, date(2031, 3, 4), 2, early)[0] == "not_operating"  # a Tuesday
    late = datetime(2031, 3, 2, 8, 31, tzinfo=UTC)
    assert addon_rules.line_problem(RULE, MONDAY, 2, late)[0] == "past_cutoff"
    assert addon_rules.line_problem(RULE, MONDAY, 2, datetime(2031, 3, 2, 8, 30)) is None  # naive = UTC, at the edge


def test_offer_child_pct_wins_over_the_contract_but_not_a_child_rate() -> None:
    adult = Decimal("40.00")
    by_offer = child_price.child_net(adult, offer_pct=Decimal("25"), contract_pct=Decimal("50"))
    assert (by_offer.net, by_offer.basis) == (Decimal("10.00"), child_price.BASIS_OFFER)
    by_rate = child_price.child_net(adult, child_rate_net=Decimal("30"), offer_pct=Decimal("25"))
    assert by_rate.basis == child_price.BASIS_RATE
    by_contract = child_price.child_net(adult, contract_pct=Decimal("50"))
    assert (by_contract.net, by_contract.basis) == (Decimal("20.00"), child_price.BASIS_CONTRACT)


# ── Activity catalogue workbook ──────────────────────────────────────────────

CONTRACT = {"Title": "2031 excursions", "Valid from": "2031-01-01", "Valid to": "2031-12-31", "Currency": "USD"}
PLACE = "d0000000-0000-4000-8000-000000000001"


def _row(title: str, **over: Any) -> tuple[Any, ...]:
    values = {
        "Title": title,
        "Places": PLACE,
        "Unit": "person",
        "Duration minutes": 180,
        "Max group size": 12,
        "Inclusions": "Guide; Water",
        "Min pax": 2,
        "Cut-off hours": 24,
        "Weekdays": "Mon, Thu",
        "Start times": "08:00",
        "Net adult": 30,
        "Net child": 15,
        "Child age max": 11,
        "Child %": None,
        "Season": None,
        "Valid from": None,
        "Valid to": None,
        **over,
    }
    return tuple(values[c] for c in activity_import.COLUMNS)


def test_five_rows_parse_into_five_activities() -> None:
    titles = ["Lotus lake paddle", "Lantern night walk", "Herb garden class", "Bamboo raft drift", "Market cycle"]
    data = activity_import.build_workbook(CONTRACT, [_row(t) for t in titles])
    sheet = activity_import.parse(data)
    assert sheet.problems == []
    assert [r.title for r in sheet.rows] == titles
    first = sheet.rows[0]
    assert first.rules == {
        "min_pax": 2,
        "cutoff_hours": 24,
        "operating_weekdays": [1, 4],
        "start_times": [time(8, 0)],
    }
    assert first.service["inclusions"] == ["Guide", "Water"] and first.service["duration_minutes"] == 180
    assert first.adult == {"net_amount": Decimal("30")}
    assert first.child == {"pax_type": "child", "net_amount": Decimal("15"), "age_max": 11}
    assert sheet.contract["title"] == "2031 excursions"


def test_row_problems_are_reported_with_row_and_column() -> None:
    rows = [
        _row("No cut-off", **{"Cut-off hours": None}),
        _row("Odd weekday", Weekdays="Funday"),
        _row("Group child", Unit="group"),
        _row("", Places=None, **{"Net adult": None}),
    ]
    sheet = activity_import.parse(activity_import.build_workbook(CONTRACT, rows))
    found = {(p.row, p.column, p.code) for p in sheet.problems}
    assert (2, "Cut-off hours", "required") in found
    assert (3, "Weekdays", "invalid_value") in found
    assert (4, "Net child", "child_rate_per_person") in found
    assert {(5, "Title", "required"), (5, "Places", "required"), (5, "Net adult", "required")} <= found


def test_missing_sheet_and_columns() -> None:
    sheet = activity_import.parse(activity_import.build_workbook(CONTRACT, []))
    assert [p.code for p in sheet.problems] == ["no_rows"]
    with pytest.raises(Invalid):
        activity_import.parse(b"not a workbook")
    assert activity_import.template()[:2] == b"PK"


# ── Contract expiry job, registries ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_expiry_schedule_queues_once_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    keys: list[str] = []

    async def enqueue(_pool: Any, kind: str, payload: dict[str, Any], **kw: Any) -> str:
        assert kind == contract_expiry.KIND and payload == {"trigger": "schedule"}
        keys.append(kw["idempotency_key"])
        return "job-1"

    monkeypatch.setattr(contract_expiry.queue, "enqueue", enqueue)
    clock = [datetime(2031, 3, 3, 0, 30, tzinfo=UTC)]
    hook = contract_expiry.ExpiryDailySchedule(now=lambda: clock[0])
    assert await hook(None) == []  # before the slot
    clock[0] = datetime(2031, 3, 3, 1, 5, tzinfo=UTC)
    assert await hook(None) == ["job-1"]
    assert await hook(None) == []  # once a day per process
    assert keys == ["bk_contract_expiry_watch:2031-03-03"]


def test_registries_know_the_new_kind_and_policy_values() -> None:
    assert kinds.expected_seconds(contract_expiry.KIND) == 120
    for key in ("addon_days_ahead", "contract_expiry_alert_days"):
        param = policy_params.PARAMS[key]
        assert param.section == "operations" and param.group == policy_params.PRICING and param.type == "integer"


def test_role_matrix_for_addons() -> None:
    assert PERMISSIONS["addon.read"] == {"admin", "sales", "contracting", "content_creator", "finance"}
    assert PERMISSIONS["addon.write"] == {"admin", "contracting", "content_creator"}
    assert PERMISSIONS["addon.approve"] == {"admin", "contracting"}
    assert custom_roles.is_approve_class("addon.approve") and not custom_roles.is_approve_class("addon.write")
    assert "addon.read" in PREVIEW_PERMISSIONS and "addon.write" not in PREVIEW_PERMISSIONS
