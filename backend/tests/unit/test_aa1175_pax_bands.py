"""AA-1175 (bk-066): the group-size bands of the computed price matrix as the pricing policy parameter
``price_pax_bands`` (Pricing > Policy): the value check (contiguous from 1, no overlap, only the last band open, inside
the range, at most 8 bands), the band lookup, the ``Policy`` accessor, the registry entry and its guide, and the cost
sheet priced on the bands in force (rows per band, the season label from the band of 2, the single supplement whatever
the bands, no row without bands). The same on PostgreSQL: tests/integration/test_aa1175_pax_bands.py."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest

from app.catalog import cost_sheet as cs
from app.catalog import price_import as pi
from app.pricing import pax_bands, policy_guides, policy_params, policy_values
from app.supply.rates import RateAsk
from tests.unit.fakes import OLD_BANDS, POLICY_FRAGMENT, POLICY_SEED, SEED_BANDS, ScriptedConn, policy_rows

KEY = "price_pax_bands"
OWNER = [[1, 1], [2, 2], [3, 3], [4, 4], [5, 7], [8, 10], [11, None]]
DAY = date(2027, 1, 20)
ROOM, CAR = "r-1", "s-car"


def _norm(value: Any) -> Any:
    return policy_params.normalise(KEY, value)


# ── The value check ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        OWNER,
        [[1, 1], [2, 2], [3, 4], [5, 6], [7, 10], [11, None]],  # the bands the code used before bk-066
        [[1, 2], [3, 10]],  # a capped top band: no computed row above 10
        [[1, None]],  # one band for every group
        [[1, 1], [2, 2], [3, 3], [4, 4], [5, 5], [6, 6], [7, 7], [8, 60]],  # 8 bands, up to the maximum
    ],
)
def test_valid_bands_are_stored_as_pairs(value: Any) -> None:
    assert _norm(value) == [list(b) for b in value]


@pytest.mark.parametrize(
    ("value", "says"),
    [
        ("1, 2, 4", "a list of 1 to 8 bands"),
        ([], "a list of 1 to 8 bands"),
        ([[n, n] for n in range(1, 10)], "a list of 1 to 8 bands"),  # 9 bands
        ([[2, 2], [3, None]], "band 1 must start at 1"),
        ([[1, 1], [2, 2], [4, 4]], "band 3 must start at 3"),  # a gap: 3 is in no band
        ([[1, 2], [2, 4]], "band 2 must start at 3"),  # an overlap: 2 in two bands
        ([[1, None], [2, 4]], "only the last band may be open"),
        ([[1, 1], [3, 2]], "band 2: the largest group 2 is smaller than the smallest 3"),
        ([[1, 4], [5, 3]], "band 2: the largest group 3 is smaller than the smallest 5"),
        ([[1, 1], [2]], "band 2: a pair"),
        ([[1, 1], "2-4"], "band 2: a pair"),
        ([[1, "1"]], "band 1: whole numbers"),
        ([[1, 1.5]], "band 1: whole numbers"),
        ([[True, 1]], "band 1: whole numbers"),
        ([[1, 1], [2, 61]], "band 2: at most 60"),
        ([[1, 60], [61, None]], "band 2: at most 60"),  # an open band starting above the range
    ],
)
def test_invalid_bands_are_refused_with_the_reason(value: Any, says: str) -> None:
    with pytest.raises(policy_params.PolicyValueError, match=says) as err:
        _norm(value)
    assert str(err.value).startswith(f"{KEY}: ")


def test_the_parameter_is_registered_with_its_guide() -> None:
    param = policy_params.PARAMS[KEY]
    assert (param.type, param.unit, param.group, param.section) == ("pax_bands", "travellers", "pricing", "prices")
    assert param.used_by == "app.catalog.cost_sheet" and param.type in policy_params.TYPES
    assert policy_params.describe(param)["minimum"] == "1" and policy_params.describe(param)["maximum"] == "60"
    guide = policy_guides.guide(KEY)
    assert guide is not None
    assert "next recompute" in guide["en"]["when"] and "tính lại" in guide["vi"]["when"]
    assert guide["en"]["example"].startswith("Starting value 1, 2, 3, 4, 5-7, 8-10, 11+")
    assert _norm(json.loads(POLICY_SEED[KEY])) == OWNER  # the unit-test seed is a valid value (bk-066 v2)


# ── The band lookup ─────────────────────────────────────────────────────────────


def test_band_for_a_party() -> None:
    bands = pax_bands.as_bands(OWNER)
    assert bands == SEED_BANDS
    assert pax_bands.band_for(bands, 4) == (4, 4)
    assert pax_bands.band_for(bands, 6) == (5, 7)
    assert pax_bands.band_for(bands, 9) == (8, 10)
    assert pax_bands.band_for(bands, 12) == (11, None)
    assert pax_bands.band_for(pax_bands.as_bands([[1, 2], [3, 10]]), 13) is None  # above a capped top band
    assert pax_bands.index_for(bands, 2) == 1 and pax_bands.index_for(bands, 0) is None


def test_labels_and_the_workbook_line() -> None:
    assert [pax_bands.label(b) for b in SEED_BANDS] == ["1", "2", "3", "4", "5–7", "8–10", "11+"]
    assert pax_bands.describe(SEED_BANDS) == "1, 2, 3, 4, 5–7, 8–10, 11+"
    line = pax_bands.workbook_line(OWNER)
    assert "1, 2, 3, 4, 5–7, 8–10, 11+" in line and "Pax min / Pax max" in line
    assert "none are computed" in pax_bands.workbook_line(None)
    data = pi.build_workbook(notes=(line,))
    assert [p.code for p in pi.parse(data).problems] == ["no_rows"]  # only "empty": an extra README line is no problem


def test_the_policy_accessor() -> None:
    policy = policy_values.from_rows(DAY, policy_rows())
    assert policy.pax_bands == SEED_BANDS
    assert policy_values.from_rows(DAY, policy_rows(price_pax_bands="[[1, null]]")).pax_bands == ((1, None),)


# ── The cost sheet on the bands in force ───────────────────────────────────────


def rate(net: str, unit: str = "person", season: str | None = "Low") -> dict[str, Any]:
    return {"id": "x", "unit": unit, "net_amount": net, "currency": "USD", "season": season}


def _pick(ask: RateAsk) -> Any:
    if ask.room_type_id:  # a double 120, a single 90 per room night
        return rate("120" if ask.pax == 2 else "90", "room_night")
    return rate("200", "vehicle")  # one car for the group


def _rows(bands: tuple[pax_bands.Band, ...], parts: list[cs.Part]) -> list[cs.NetRow]:
    wins = [cs.Window(DAY, date(2027, 3, 31))]
    found = {ask: _pick(ask) for ask in cs.asks(wins, parts, bands)}
    singles = [cs.single_supplement(w, parts, found, {}) for w in wins]
    rows, unpriced = cs.net_rows(wins, cs.cells(wins, parts, found, bands), {}, bands, singles)
    assert unpriced == 0
    return rows


def test_one_row_per_band_priced_at_its_smallest_group() -> None:
    rows = _rows(SEED_BANDS, [cs.Part(1, ROOM, None, 2), cs.Part(1, None, CAR)])
    assert [(r.pax_min, r.pax_max) for r in rows] == list(SEED_BANDS)
    net = {r.pax_min: r.net_amount for r in rows}
    # 3 pax: a double 120 + a single 90 + car 200 = 410 / 3; 4 pax: 2 doubles 240 + car 200 = 440 / 4
    assert net[3] == D("136.67") and net[4] == D("110.00")
    assert net[4] < net[3]  # the band of 4 on its own: cheaper than the old 3-4 band priced for 3
    old = {r.pax_min: r.net_amount for r in _rows(OLD_BANDS, [cs.Part(1, ROOM, None, 2), cs.Part(1, None, CAR)])}
    assert old[3] == net[3] and 4 not in old  # the old 3-4 band priced every party of 4 at the 3-traveller net


def test_the_season_label_comes_from_the_band_that_holds_two() -> None:
    def pick(ask: RateAsk) -> Any:
        return rate("50", season="Shoulder" if (ask.pax or 0) <= 2 else "Low")

    parts = [cs.Part(1, None, CAR)]
    wins = [cs.Window(DAY, DAY)]
    bands = pax_bands.as_bands([[1, 2], [3, None]])
    found = {ask: pick(ask) for ask in cs.asks(wins, parts, bands)}
    rows, _ = cs.net_rows(wins, cs.cells(wins, parts, found, bands), {}, bands)
    assert {r.season for r in rows} == {"Shoulder"}  # the 1-2 band (priced for 1) carries the label
    capped = pax_bands.as_bands([[1, 1]])  # no band holds 2: the only band gives the label
    found = {ask: pick(ask) for ask in cs.asks(wins, parts, capped)}
    rows, _ = cs.net_rows(wins, cs.cells(wins, parts, found, capped), {}, capped)
    assert [(r.pax_min, r.pax_max, r.season) for r in rows] == [(1, 1, "Shoulder")]


def test_the_single_supplement_is_read_whatever_the_bands() -> None:
    parts = [cs.Part(1, ROOM, None, 2)]
    rows = _rows(pax_bands.as_bands([[1, None]]), parts)  # one band priced for 1: only singles in the room mix
    assert [(r.pax_min, r.pax_max) for r in rows] == [(1, None)]
    assert rows[0].single_net == D("30.00")  # single 90 - double 120 / 2
    asked = cs.asks([cs.Window(DAY, DAY)], parts, pax_bands.as_bands([[1, None]]))
    assert {a.pax for a in asked} == {1, 2}


@pytest.mark.asyncio
async def test_load_pricing_reads_the_bands_in_force() -> None:
    pricing = await cs.load_pricing(ScriptedConn(), DAY)  # type: ignore[arg-type]
    assert pricing.bands == SEED_BANDS


@pytest.mark.asyncio
async def test_without_bands_in_force_no_row_is_computed(caplog: pytest.LogCaptureFixture) -> None:
    rows = [r for r in policy_rows() if r["param_key"] != KEY]
    conn = ScriptedConn().on(POLICY_FRAGMENT, rows)
    pricing = await cs.load_pricing(conn, DAY)  # type: ignore[arg-type]
    assert pricing.bands == () and "cost_sheet_pax_bands_unavailable" in caplog.text
    rule = object()
    with_rule = cs.Pricing(on=DAY, rules={cs.engine.Channel.B2B: rule}, fx={})  # type: ignore[dict-item]
    assert with_rule.can_price is False
    assert cs.Pricing(on=DAY, rules={cs.engine.Channel.B2B: rule}, fx={}, bands=SEED_BANDS).can_price is True  # type: ignore[dict-item]
    conn.on("FROM booking.trip_plan_components c", [{"day_no": 1, "room_type_id": None, "service_id": CAR,
                                                     "max_occupancy": None, "status": "active",
                                                     "hotel_status": "active"}])  # fmt: skip
    out, info = await cs.compute(conn, "plan-1", with_rule)  # type: ignore[arg-type]
    assert out == [] and info["reason"] == cs.NO_RULE


def test_an_approved_policy_change_moves_the_recompute_watermark() -> None:
    assert "FROM booking.pricing_policy_events" in cs.WATERMARK_SQL and "event = 'approved'" in cs.WATERMARK_SQL
