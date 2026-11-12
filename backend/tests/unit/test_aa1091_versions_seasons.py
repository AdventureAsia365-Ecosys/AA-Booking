"""AA-1091 (bk-052 / bk-053, root ADR 0005b d1, d2, d4, d9, d10) units: the one season function (fixed ranges,
festive ranges from holiday event rows with days before and after, stop-sale overlay, segments and cuts), the overlap
rule of price list versions (winning dates, derived superseded, uncovered dates, pieces cut by season), the tariff year
from the policy start, the frozen diff, and the B2B quote's season (peak uplift, stop-sale on request). Names and dates
are invented."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D

from app.b2b import departures, quotes
from app.b2b.components import LineCost
from app.catalog import price_versions, version_matrix
from app.pricing import engine, repository, seasons

Y = 2031


def _d(month: int, day: int, year: int = Y) -> date:
    return date(year, month, day)


CAL = seasons.Calendar(
    id="c-1",
    fixed=(seasons.Span(_d(1, 1), _d(4, 1), "high"), seasons.Span(_d(7, 1), _d(9, 1), "peak")),
    festive=tuple(
        seasons.event_spans(
            [("VN", "invented_fest", 1, 2, "r-9")],
            [{"country": "VN", "code": "invented_fest", "date_from": _d(2, 10), "date_to": _d(2, 13)}],
        )
    ),
    stop_sales=(seasons.Span(_d(7, 10), _d(7, 12)),),
)


def test_the_season_function() -> None:
    assert [CAL.season_on(_d(m, d)).code for m, d in ((1, 5), (2, 8), (2, 9), (2, 14), (2, 15), (5, 1), (7, 4))] == [
        "high", "high", "festive", "festive", "high", "low", "peak",
    ]  # fmt: skip
    assert CAL.season_on(_d(7, 4)).peak and CAL.season_on(_d(2, 9)).peak and not CAL.season_on(_d(1, 5)).peak
    assert CAL.season_on(_d(2, 9)).festive and not CAL.season_on(_d(7, 4)).festive
    # Stop-sale is an overlay: availability only, never the season.
    day = CAL.season_on(_d(7, 11))
    assert day.stop_sale and day.code == "peak" and not CAL.season_on(_d(7, 12)).stop_sale
    # No calendar: every date is low season, no stop-sale.
    assert seasons.EMPTY.season_on(_d(7, 4)) == seasons.SeasonDay("low", False)
    assert seasons.Calendar(default="high").season_on(_d(5, 1)).code == "high"


def test_a_calendar_counts_once_it_says_something_about_seasons() -> None:
    # Until then the old peak flags stay (no price moves on deploy): legacy_code keeps a row's flag as its season.
    assert not seasons.EMPTY.configured
    assert not seasons.Calendar(id="c-2", stop_sales=(seasons.Span(_d(7, 10), _d(7, 12)),)).configured
    assert seasons.Calendar(id="c-3", default="high").configured
    assert CAL.configured
    assert (seasons.legacy_code(True), seasons.legacy_code(False)) == ("peak", "low")


def test_segments_cuts_and_overlap() -> None:
    segs = CAL.segments(_d(1, 20), _d(3, 1))
    assert [(s.start, s.end, s.code) for s in segs] == [
        (_d(1, 20), _d(2, 9), "high"),
        (_d(2, 9), _d(2, 15), "festive"),
        (_d(2, 15), _d(3, 1), "high"),
    ]
    assert CAL.cuts(_d(1, 20), _d(3, 1)) == [_d(2, 9), _d(2, 15)]
    assert seasons.EMPTY.segments(_d(1, 1), _d(1, 1)) == []
    clash = seasons.overlapping(seasons.Span(_d(3, 15), _d(4, 15), "low"), CAL.fixed)
    assert [c.code for c in clash] == ["high"]
    assert seasons.overlapping(seasons.Span(_d(4, 1), _d(7, 1), "low"), CAL.fixed) == []  # half-open: touching is ok


def _v(no: int, start: date, end: date, *, at: int, provisional: bool = False, sale_to: date | None = None):
    return version_matrix.Version(
        id=f"v{no}",
        version_no=no,
        travel_from=start,
        travel_to=end,
        approved_at=datetime(2030, 1, at, tzinfo=UTC),
        provisional=provisional,
        sale_to=sale_to,
    )


def test_overlap_rule_superseded_and_uncovered() -> None:
    v2 = _v(2, _d(11, 1, Y - 1), _d(11, 1), at=1)
    v3 = _v(3, _d(4, 1), _d(11, 1), at=2)
    wins = version_matrix.winning([v2, v3], _d(1, 1, Y - 1))
    assert wins == {"v3": [(_d(4, 1), _d(11, 1))], "v2": [(_d(11, 1, Y - 1), _d(4, 1))]}
    assert version_matrix.superseded([v2, v3]) == set()
    v4 = _v(4, _d(11, 1, Y - 1), _d(4, 1), at=3)
    assert version_matrix.superseded([v2, v3, v4]) == {"v2"}
    # A provisional version loses to a non-provisional one whatever the approval order.
    prov = _v(5, _d(1, 1), _d(12, 1), at=9, provisional=True)
    assert version_matrix.winning([v3, prov], _d(1, 1))["v3"] == [(_d(4, 1), _d(11, 1))]
    assert version_matrix.superseded([v3, _v(6, _d(4, 1), _d(11, 1), at=1, provisional=True)]) == {"v6"}
    # A version with an end to its sale window never supersedes; outside it, it prices nothing.
    offer = _v(7, _d(1, 1), _d(12, 1), at=10, sale_to=_d(3, 1, Y - 1))
    assert version_matrix.superseded([v3, offer]) == set()
    assert version_matrix.winning([v3, offer], _d(5, 1, Y - 1))["v7"] == []
    # Dates no version prices are not quotable.
    assert version_matrix.uncovered((_d(1, 1), _d(12, 1)), [v3], _d(1, 1)) == [
        (_d(1, 1), _d(4, 1)),
        (_d(11, 1), _d(12, 1)),
    ]
    # A row cut to its version's dates and where the season changes.
    assert version_matrix.pieces((_d(1, 20), _d(5, 1)), [(_d(2, 1), _d(4, 1))], CAL) == [
        (_d(2, 1), _d(2, 9), "high"),
        (_d(2, 9), _d(2, 15), "festive"),
        (_d(2, 15), _d(4, 1), "high"),
    ]


def test_tariff_year_from_the_policy_start() -> None:
    assert price_versions.tariff_year_of(_d(10, 31), "11-01") == Y
    assert price_versions.tariff_year_of(_d(11, 1), "11-01") == Y + 1
    assert price_versions.tariff_year_window(Y, "11-01") == (_d(11, 1, Y - 1), _d(11, 1))
    assert price_versions.tariff_year_of(_d(6, 1), "01-01") == Y
    assert price_versions.tariff_year_window(Y, "01-01") == (_d(1, 1), _d(1, 1, Y + 1))


def _old(start: date, end: date, amount: str, pax: int = 2) -> dict:
    return {"channel": "b2b", "currency": "USD", "pax_min": pax, "pax_max": pax, "valid_from": start,
            "valid_to": end, "amount": amount}  # fmt: skip


def test_diff_rows() -> None:
    old = [_old(_d(1, 1), _d(12, 31), "500.00"), _old(_d(1, 1), _d(12, 31), "450.00", pax=3)]
    new = [
        {**_old(_d(1, 1), _d(6, 30), "560.00"), "position": 1},
        {**_old(_d(7, 1), _d(12, 31), "505.00"), "position": 2},
        {**_old(_d(1, 1), _d(12, 31), "300.00", pax=1), "position": 3, "effective_margin": "0.05"},
    ]
    diff = price_versions.diff_rows(old, new, (_d(1, 1), _d(1, 1, Y + 1)), D("10"), {"b2b": D("0.12")})
    rows = {r["position"]: r for r in diff["rows"]}
    assert (rows[1]["old_amount"], rows[1]["change_pct"], rows[1]["highlight"]) == ("500.00", "12.0", True)
    assert (rows[2]["change_pct"], rows[2]["highlight"], rows[2]["kind"]) == ("1.0", False, "changed")
    assert (rows[3]["kind"], rows[3]["below_floor"]) == ("added", True)
    assert [r["pax_min"] for r in diff["removed"]] == [3]
    assert diff["summary"] == {"added": 1, "changed": 2, "unchanged": 0, "removed": 1, "up": 2, "down": 0,
                               "highlighted": 1}  # fmt: skip
    assert price_versions.sha256(diff) == price_versions.sha256(dict(diff))


def _basis() -> quotes.PriceBasis:
    rule = repository.DatedRule(engine.PricingRule(engine.Channel.B2B, D("0.20"), D("0.10"), D("0.05"), D("0")),
                                date(2026, 1, 1))  # fmt: skip

    class Terms:
        tier_code = None
        tier_discount = D("0")

        def discount_for(self, plan_id: str | None) -> tuple[D, str | None]:
            return D("0"), None

    return quotes.PriceBasis(
        on=date(2026, 11, 12),
        currency="USD",
        rule=rule,
        rates={},
        fx=quotes.FxLock("USD", D("1"), D("0"), date(2026, 11, 12), "base"),
        terms=Terms(),  # type: ignore[arg-type]
    )


COST = LineCost(kind="trip_plan", costs=[(D("240"), "USD")])
PLAN = "tp:00000000-0000-4000-8000-0000000c1091"


def test_quote_line_takes_the_peak_uplift_from_the_calendar() -> None:
    low = quotes.price_line(_basis(), COST, "plan-1", season=CAL.season_on(_d(5, 1)))
    peak = quotes.price_line(_basis(), COST, "plan-1", season=CAL.season_on(_d(7, 4)))
    assert (low.b2b_price, low.note) == (D("300.00"), None)  # 240 / 0.80
    assert peak.b2b_price == D("320.00") and peak.note == {"season": {"code": "peak"}}  # 240 / 0.75
    # A component line (no plan) never takes it.
    assert quotes.price_line(_basis(), COST, None, season=CAL.season_on(_d(7, 4))).b2b_price == D("300.00")


def test_stop_sale_puts_a_tour_line_on_request() -> None:
    stop, free = CAL.season_on(_d(7, 11)), CAL.season_on(_d(7, 4))
    assert quotes.season_availability(departures.AVAILABLE, PLAN, stop) == departures.ON_REQUEST
    assert quotes.season_availability(departures.AVAILABLE, PLAN, free) == departures.AVAILABLE
    assert quotes.season_availability(departures.SOLD_OUT, PLAN, stop) == departures.SOLD_OUT
    assert quotes.season_availability(departures.AVAILABLE, "svc:00000000-0000-4000-8000-000000000001", stop) == (
        departures.AVAILABLE
    )
