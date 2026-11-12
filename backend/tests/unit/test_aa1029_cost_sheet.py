"""The cost sheet without a database (AA-1029 R4, root ADR 0005 d3): windows cut at rate boundaries shifted by the day
offset, room mixes, the net per person per band, mixed currencies in USD, unpriced cells, merged windows, the RRP of
b2b rows, hand rows kept and computed rows that overlap them left out, the row cap, and the schedule key. The same on
PostgreSQL with the rate-sheet import: tests/integration/test_aa1029_price_chain.py."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from app.catalog import cost_sheet as cs
from app.catalog import trip_plan_prices as prices
from app.pricing import engine, repository, seasons
from app.supply.rates import RateAsk

DAY = date(2027, 3, 1)
ROOM, GUIDE, JEEP = "r-1", "s-guide", "s-jeep"


def rate(net: str, unit: str = "person", currency: str = "USD", season: str | None = "Low") -> dict[str, Any]:
    return {"id": "x", "unit": unit, "net_amount": net, "currency": currency, "season": season}


def test_room_mix() -> None:
    assert cs.room_mix(1, 2) == [(1, 1)]
    assert cs.room_mix(2, 2) == [(2, 1)]
    assert cs.room_mix(5, None) == [(2, 2), (1, 1)]
    assert cs.room_mix(3, 1) == [(1, 3)]  # a one-person room type: singles only


def test_windows_cut_at_rate_bounds_shifted_by_the_day() -> None:
    parts = [cs.Part(1, None, GUIDE), cs.Part(3, ROOM, None)]
    spans = [
        cs.Span(GUIDE, date(2027, 1, 1), date(2027, 3, 31)),
        cs.Span(ROOM, date(2027, 1, 1), date(2027, 3, 10)),  # day 3 night: a start on 03-08 is its last night
        cs.Span(ROOM, date(2027, 3, 11), date(2027, 6, 30)),
    ]
    wins = cs.windows(DAY, parts, spans)
    # Anchored on the rate validity (not on DAY): the window running on DAY starts where its rates start.
    assert [(w.start, w.end) for w in wins] == [
        (date(2027, 1, 1), date(2027, 3, 8)),
        (date(2027, 3, 9), date(2027, 3, 31)),
        (date(2027, 4, 1), date(2027, 6, 28)),
        (date(2027, 6, 29), date(2027, 6, 30)),  # day 3 is past the room rates: priced as incomplete, no row
    ]


def test_windows_do_not_move_with_the_day() -> None:
    parts = [cs.Part(1, None, GUIDE), cs.Part(2, ROOM, None)]
    spans = [cs.Span(GUIDE, date(2027, 1, 1), cs.FOREVER), cs.Span(ROOM, date(2027, 1, 1), date(2027, 5, 31))]
    day_one = cs.windows(DAY, parts, spans)
    assert cs.windows(DAY + timedelta(days=1), parts, spans) == day_one  # same inputs next day: same windows
    assert day_one[-1].end == cs.FOREVER  # an open-ended rate: no overflow, no end derived from the day
    # Only a window that is over drops out.
    later = cs.windows(date(2027, 6, 1), parts, spans)
    assert later == [w for w in day_one if w.end >= date(2027, 6, 1)] and len(later) < len(day_one)


def test_horizon() -> None:
    assert cs.horizon(DAY, []) is None
    assert cs.horizon(DAY, [cs.Span(ROOM, DAY, DAY - timedelta(days=1))]) is None
    assert cs.horizon(DAY, [cs.Span(ROOM, DAY, date(2027, 4, 1))]) == date(2027, 4, 1)
    assert cs.horizon(DAY, [cs.Span(ROOM, DAY, cs.FOREVER)]) == cs.FOREVER
    assert cs.windows(DAY, [cs.Part(1, ROOM, None)], [cs.Span(ROOM, date(2026, 1, 1), date(2026, 12, 31))]) == []


def _found(parts: list[cs.Part], wins: list[cs.Window], pick: Any) -> dict[RateAsk, Any]:
    return {ask: pick(ask) for ask in cs.asks(wins, parts)}


def test_net_per_person_per_band() -> None:
    parts = [cs.Part(1, ROOM, None, 3), cs.Part(1, None, GUIDE), cs.Part(2, None, JEEP)]
    wins = [cs.Window(DAY, date(2027, 3, 31))]

    def pick(ask: RateAsk) -> Any:
        if ask.room_type_id:  # a double 100, a single 80 per room night
            return rate("100" if ask.pax == 2 else "80", "room_night")
        if ask.service_id == GUIDE:
            return rate("60", "group")
        return rate("20", "person")

    grid = cs.cells(wins, parts, _found(parts, wins, pick))
    rows, unpriced = cs.net_rows(wins, grid, {})
    assert unpriced == 0
    by_band = {(r.pax_min, r.pax_max): r.net_amount for r in rows}
    # 1 pax: single 80 + guide 60 + jeep 20 = 160; 2 pax: (100 + 60 + 40) / 2 = 100; 3 pax: (100 + 80 + 60 + 60) / 3
    assert by_band[(1, 1)] == D("160.00")
    assert by_band[(2, 2)] == D("100.00")
    assert by_band[(3, 4)] == D("100.00")
    assert by_band[(11, None)] == D("78.18")  # 5 doubles + 1 single 580, guide 60, jeep 220: 860 / 11
    assert {r.net_currency for r in rows} == {"USD"}
    assert rows[0].season == "Low" and rows[0].peak is False


def test_mixed_currencies_are_summed_in_usd_and_a_missing_rate_leaves_the_cell_unpriced() -> None:
    parts = [cs.Part(1, None, GUIDE), cs.Part(1, None, JEEP)]
    wins = [cs.Window(DAY, DAY)]

    def pick(ask: RateAsk) -> Any:
        if ask.service_id == GUIDE:
            return rate("2400000", "group", "VND", season="Peak")
        return rate("50", "group", "USD") if (ask.pax or 0) <= 6 else None

    grid = cs.cells(wins, parts, _found(parts, wins, pick))
    fx = {"VND": engine.FxRate("VND", D("24000"))}
    rows, unpriced = cs.net_rows(wins, grid, fx)
    assert unpriced == 2  # bands 7-10 and 11+ have no jeep rate
    two = next(r for r in rows if r.pax_min == 2)
    assert (two.net_amount, two.net_currency) == (D("75.00"), "USD")  # (100 + 50) / 2
    # AA-1091 (bk-053): with no configured calendar (no codes) the old rule stays, so no price moves on deploy: a
    # label saying "Peak" makes the row peak.
    assert two.season == "Low / Peak" and two.peak is True and two.season_code == "peak"
    # Once the plan's calendar is configured, the calendar decides and the label is only shown.
    calendar_rows, _ = cs.net_rows(wins, grid, fx, codes=["low"])
    two = next(r for r in calendar_rows if r.pax_min == 2)
    assert two.season == "Low / Peak" and two.peak is False and two.season_code == "low"
    assert cs.net_rows(wins, grid, {})[0] == []  # no VND rate: nothing can be priced


def test_a_peak_calendar_season_makes_the_row_peak_and_windows_cut_where_it_changes() -> None:
    parts = [cs.Part(1, None, GUIDE)]
    spans = [cs.Span(GUIDE, DAY, DAY + timedelta(days=29))]
    peak = seasons.Calendar(id="c", fixed=(seasons.Span(DAY + timedelta(days=10), DAY + timedelta(days=20), "peak"),))
    wins = cs.windows(DAY, parts, spans, peak)
    assert [(w.start, w.end) for w in wins] == [
        (DAY, DAY + timedelta(days=9)),
        (DAY + timedelta(days=10), DAY + timedelta(days=19)),
        (DAY + timedelta(days=20), DAY + timedelta(days=29)),
    ]
    grid = cs.cells(wins, parts, _found(parts, wins, lambda a: rate("30")))
    codes = [peak.season_on(w.start).code for w in wins]
    rows, _ = cs.net_rows(wins, grid, {}, codes=codes)
    two = [r for r in rows if r.pax_min == 2]
    assert [(r.season_code, r.peak) for r in two] == [("low", False), ("peak", True), ("low", False)]
    # Without a calendar the same rate is one window, low season.
    assert len(cs.windows(DAY, parts, spans)) == 1


def test_equal_neighbour_windows_merge() -> None:
    parts = [cs.Part(1, None, GUIDE)]
    wins = [cs.Window(DAY, DAY), cs.Window(DAY + timedelta(days=1), DAY + timedelta(days=9))]
    grid = cs.cells(wins, parts, _found(parts, wins, lambda a: rate("30")))
    rows, _ = cs.net_rows(wins, grid, {})
    assert {(r.valid_from, r.valid_to) for r in rows} == {(DAY, DAY + timedelta(days=9))}
    assert len(rows) == len(cs.PAX_BANDS)


RULE_B2B = repository.DatedRule(
    engine.PricingRule(engine.Channel.B2B, D("0.15"), D("0.10"), D("0.03"), D("0.03")), DAY, D("0.25")
)


def _pricing(**rules: repository.DatedRule) -> cs.Pricing:
    return cs.Pricing(on=DAY, rules={engine.Channel(k): v for k, v in rules.items()}, fx={})


def test_priced_rows_carry_the_rrp_on_b2b_only() -> None:
    fd = repository.DatedRule(engine.PricingRule(engine.Channel.FIXED_DEPARTURE, D("0.25"), D("0.18")), DAY)
    nets = [cs.NetRow(DAY, DAY, 2, 2, D("100.00"), "USD", "Low", False)]
    rows, refused = cs.priced_rows(nets, _pricing(b2b=RULE_B2B, fixed_departure=fd))
    assert refused == 0
    b2b = next(r for r in rows if r["channel"] == "b2b")
    # 100 / (1 - 0.15 - 0.03) = 121.95 -> 125 (up to 5); RRP 125 x 1.25 = 156.25 -> 160
    assert (b2b["amount"], b2b["rrp_amount"], b2b["computed"]) == (D("125.00"), D("160.00"), True)
    fixed = next(r for r in rows if r["channel"] == "fixed_departure")
    assert fixed["rrp_amount"] is None and fixed["amount"] == D("135.00")
    peak = cs.priced_rows([cs.NetRow(DAY, DAY, 2, 2, D("100.00"), "USD", "Peak", True)], _pricing(b2b=RULE_B2B))[0]
    assert peak[0]["amount"] == D("130.00")  # +3 points peak uplift: 100 / 0.79 = 126.58 -> 130


def _stored(**over: Any) -> dict[str, Any]:
    row = {
        "position": 1,
        "channel": "b2b",
        "season": "Low",
        "peak": False,
        "valid_from": DAY,
        "valid_to": DAY + timedelta(days=30),
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD",
        "amount": "200.00",
        "net_amount": None,
        "net_currency": None,
        "effective_margin": None,
        "priced_on": None,
        "overridden": False,
        "computed": False,
        "rrp_amount": None,
        "created_by": "staff-1",
    }
    row.update(over)
    return row


def test_assemble_keeps_hand_rows_and_drops_overlapping_computed_rows() -> None:
    hand = _stored()
    overridden = _stored(position=2, pax_min=3, pax_max=4, computed=True, overridden=True, net_amount="90.00",
                         net_currency="USD", priced_on=DAY, effective_margin="0.1000")  # fmt: skip
    stale = _stored(position=3, pax_min=5, pax_max=6, computed=True, net_amount="1", net_currency="USD", priced_on=DAY)
    nets = [cs.NetRow(DAY, DAY + timedelta(days=60), lo, hi, D("100"), "USD", "Low", False) for lo, hi in cs.PAX_BANDS]
    computed, _ = cs.priced_rows(nets, _pricing(b2b=RULE_B2B))
    final, counts = cs.assemble([hand, overridden, stale], computed, _pricing(b2b=RULE_B2B))
    assert counts == {"kept": 2, "computed": 4, "shadowed": 2, "trimmed": 0, "truncated": 0}
    assert [r["position"] for r in final] == list(range(1, 7))
    assert final[0]["created_by"] == "staff-1" and final[0]["amount"] == D("200.00")
    assert final[0]["rrp_amount"] == D("250.00")  # a hand b2b row gets today's RRP too
    assert all(r["created_by"] is None for r in final[2:])
    assert {(r["pax_min"], r["pax_max"]) for r in final[2:]} == {(1, 1), (5, 6), (7, 10), (11, None)}


def test_a_hand_row_that_is_over_trims_a_computed_row_instead_of_hiding_it() -> None:
    # Computed windows start where the rates start, so a computed row may begin before today.
    past = _stored(valid_from=DAY - timedelta(days=40), valid_to=DAY - timedelta(days=11))
    ahead = _stored(
        position=2, pax_min=3, pax_max=4, valid_from=DAY + timedelta(days=5), valid_to=DAY + timedelta(days=9)
    )
    nets = [cs.NetRow(DAY - timedelta(days=30), DAY + timedelta(days=60), lo, hi, D("100"), "USD", "Low", False)
            for lo, hi in ((2, 2), (3, 4))]  # fmt: skip
    computed, _ = cs.priced_rows(nets, _pricing(b2b=RULE_B2B))
    final, counts = cs.assemble([past, ahead], computed, _pricing(b2b=RULE_B2B))
    assert counts == {"kept": 2, "computed": 1, "shadowed": 1, "trimmed": 1, "truncated": 0}
    two = next(r for r in final if r["computed"])
    assert (two["pax_min"], two["valid_from"], two["valid_to"]) == (
        2,
        DAY - timedelta(days=10),
        DAY + timedelta(days=60),
    )
    assert computed[0]["valid_from"] == DAY - timedelta(days=30)  # the input row is not changed in place


def test_assemble_caps_the_set_at_max_rows() -> None:
    nets = [
        cs.NetRow(DAY + timedelta(days=i * 2), DAY + timedelta(days=i * 2 + 1), 2, 2, D("100"), "USD", None, False)
        for i in range(prices.MAX_ROWS + 5)
    ]
    computed, _ = cs.priced_rows(nets, _pricing(b2b=RULE_B2B))
    final, counts = cs.assemble([_stored(valid_to=DAY - timedelta(days=1), valid_from=DAY - timedelta(days=9))],
                                computed, _pricing(b2b=RULE_B2B))  # fmt: skip
    assert len(final) == prices.MAX_ROWS and counts["truncated"] == 6
    assert max(r["valid_from"] for r in final if r["computed"]) < nets[-1].valid_from  # the latest left out


def test_rrp_price_rounds_up() -> None:
    assert engine.rrp_price(D("800"), D("0.25"), "USD") == D("1000.00")
    assert engine.rrp_price(D("801"), D("0.25"), "USD") == D("1005.00")
    with pytest.raises(engine.PricingError):
        engine.rrp_price(D("800"), D("-0.1"), "USD")
    assert prices.rrp_for("fixed_departure", D("800"), "USD", D("0.25")) is None
    assert prices.rrp_for("b2b", D("800"), "USD", None) is None


class FakePool:
    def __init__(self, mark: str) -> None:
        self.mark = mark
        self.enqueued: list[str] = []

    def acquire(self) -> FakePool:
        return self

    async def __aenter__(self) -> FakePool:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "INSERT INTO booking.job" in sql:
            key = args[2]
            if key in self.enqueued:
                return None
            self.enqueued.append(key)
            return "job-1"
        return self.mark


@pytest.mark.asyncio
async def test_schedule_enqueues_once_per_watermark() -> None:
    pool = FakePool("a")
    hook = cs.RecomputeSchedule()
    assert await hook(pool) == "job-1"  # type: ignore[arg-type]
    assert await hook(pool) is None  # type: ignore[arg-type]
    pool.mark = "b"
    assert await hook(pool) == "job-1"  # type: ignore[arg-type]
    assert len(pool.enqueued) == 2 and all(k.startswith("bk_price_recompute:wm:") for k in pool.enqueued)
