"""AA-1030 G3 agency tariff (``app.b2b.tariff``) without a database: the grid, the pricing through the quote engine's
own functions, the rows left unpriced and why, the CSV and the agency view. End to end (the tariff equals a real
quote for several partners, tiers and cards): ``tests/integration/test_aa1030_tariff.py``."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from app.b2b import agency_pricing, quotes, tariff
from app.b2b.components import MAX_RATE_LOOKUPS, PLAN_COMPONENTS_SQL, LineAsk, LineCost
from app.b2b.errors import B2BError
from app.catalog import cost_sheet, liveness
from app.pricing import engine, repository, seasons

ON = date(2026, 11, 5)
PLAN_A = "11111111-1111-4111-8111-111111111111"
PLAN_B = "22222222-2222-4222-8222-222222222222"
RULE = repository.DatedRule(
    rule=engine.PricingRule(
        channel=engine.Channel.B2B,
        target_margin=D("0.15"),
        floor_margin=D("0.10"),
        peak_uplift_pts=D("0"),
        card_fee_pct=D("0.03"),
    ),
    valid_from=date(2026, 1, 1),
    rrp_markup_pct=D("0.25"),
)


def _row(plan: str, source_id: str, pax_min: int, pax_max: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "plan_id": plan,
        "source_id": source_id,
        "title": "Invented Lagoon Loop",
        "country": "Invented Land",
        "duration_days": 3,
        "components": 2,
        "season": "Green",
        "peak": False,
        "valid_from": date(2026, 10, 1),
        "valid_to": date(2027, 3, 31),
        "pax_min": pax_min,
        "pax_max": pax_max,
        "rrp_amount": D("150.00"),
        "rrp_currency": "USD",
        **extra,
    }


class Conn:
    """The matrix rows, and (optional) plan components and rate spans for the cuts at rate changes."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        parts: list[dict[str, Any]] | None = None,
        spans: list[dict[str, Any]] = (),
        fx: dict[str, D] | None = None,
    ) -> None:
        self.rows = rows
        self.parts = parts or []
        self.spans = list(spans)
        self.fx = fx or {}  # approved FX rates in force: {currency: rate per USD}
        self.asked: list[tuple[Any, ...]] = []
        self.fx_asked: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if sql == repository.FX_SQL:
            self.fx_asked.append(args)
            return [
                {"currency": cur, "rate_per_usd": rate, "buffer_pct": D("0"), "as_of": ON, "source": "invented"}
                for cur, rate in self.fx.items()
                if cur in args[0]
            ]
        if sql == PLAN_COMPONENTS_SQL:
            return [p for p in self.parts if p["trip_plan_id"] in args[0]]
        if sql == cost_sheet.SPANS_SQL:
            return self.spans
        if sql == seasons.PLAN_CALENDARS_SQL:  # AA-1091 (bk-055): no plan has a season calendar here (low season)
            return [{"plan_id": p, "calendar_id": None} for p in args[0]]
        assert sql == tariff.ROWS_SQL
        self.asked.append(args)
        return self.rows[: args[2]]


def _basis(terms: agency_pricing.AgencyTerms, currency: str = "USD", rates: dict | None = None) -> quotes.PriceBasis:
    return quotes.PriceBasis(
        on=ON,
        currency=currency,
        rule=RULE,
        rates=rates or {},
        fx=quotes.FxLock(currency, D("1"), D("0"), ON, "base"),
        terms=terms,
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """price_lines: 82 USD net per traveller (a sell of 100 USD each), except where ``refuse`` names the party."""
    state: dict[str, Any] = {"refuse": set(), "calls": 0, "terms": agency_pricing.NO_TERMS, "served": None}

    async def price_lines(conn: Any, asks: list[LineAsk]) -> list[LineCost]:
        state["calls"] += 1
        for ask in asks:
            if ask.adult in state["refuse"]:
                raise B2BError("invalid", f"line {ask.line_ref}: no rate in force on {ask.service_date} for 1 per room")
        # AA-1075: every traveller in a single room adds 16.40 USD net each (a sell of 20 USD).
        return [
            LineCost(kind="trip_plan", costs=[((D("82") + (D("16.40") if ask.rooms else 0)) * ask.adult, "USD")])
            for ask in asks
        ]

    async def states(conn: Any, keys: Any, *, strict: bool = False) -> dict[str, liveness.KeyState]:
        served = state["served"]
        return {k: liveness.KeyState(k, liveness.SERVED if served is None or k in served else liveness.RETIRED)
                for k in keys}  # fmt: skip

    async def price_basis(conn: Any, **kw: Any) -> quotes.PriceBasis:
        state["basis_kw"] = kw
        return _basis(state["terms"], kw["currency"])

    monkeypatch.setattr(tariff, "price_lines", price_lines)
    monkeypatch.setattr(liveness, "states", states)
    monkeypatch.setattr(quotes, "price_basis", price_basis)
    return state


@pytest.mark.asyncio
async def test_every_band_is_priced_by_the_quote_formula_with_the_partner_discount(wired: dict[str, Any]) -> None:
    card = agency_pricing.Card("card-1", PLAN_A, D("0.10"))
    wired["terms"] = agency_pricing.AgencyTerms("silver", D("0.03"), (card,))
    rows = [_row(PLAN_A, "tour-a", 1, 1), _row(PLAN_A, "tour-a", 3, 4), _row(PLAN_B, "tour-b", 2, 2)]
    out = await tariff.build(Conn(rows), partner_id="p", currency="USD", on=ON)

    assert wired["basis_kw"]["require_all"] is False and wired["basis_kw"]["cost_currencies"] == {"USD"}
    assert wired["calls"] == 1  # one price_lines call for the whole grid (within the lookup budget)
    got = [(i["tour"]["source_id"], i["priced_for_pax"], i["price"]["amount"], i["agency_discount_pct"]) for i in
           out["items"]]  # fmt: skip
    # 100 USD a traveller x party, less the plan card (10 %) on tour-a, less the tier (3 %) on tour-b.
    assert got == [
        ("tour-a", 1, "90.00", "0.1000"),
        ("tour-a", 3, "270.00", "0.1000"),
        ("tour-b", 2, "194.00", "0.0300"),
    ]
    assert out["items"][1]["price_per_person"] == {"amount": "90.00", "currency": "USD"}
    assert [i["rate_card_id"] for i in out["items"]] == ["card-1", "card-1", None]
    assert out["items"][0]["priced_for_date"] == ON  # the window has started: priced today
    assert (out["items"][0]["valid_from"], out["items"][0]["valid_to"]) == (ON, date(2027, 3, 31))  # from today
    assert out["tier_code"] == "silver" and out["unpriced"] == [] and out["truncated"] is False


@pytest.mark.asyncio
async def test_the_tariff_line_is_the_quote_line(wired: dict[str, Any]) -> None:
    """The same basis and cost through ``quotes.price_line`` (what create_quote calls) give the tariff's price."""
    terms = agency_pricing.AgencyTerms("gold", D("0.06"))
    wired["terms"] = terms
    out = await tariff.build(Conn([_row(PLAN_B, "tour-b", 5, 6)]), partner_id="p", currency="USD", on=ON)
    line = quotes.price_line(_basis(terms), LineCost(kind="trip_plan", costs=[(D("410"), "USD")]), PLAN_B)
    assert out["items"][0]["price"] == quotes.money(line.b2b_price, "USD") == {"amount": "470.00", "currency": "USD"}


@pytest.mark.asyncio
async def test_rows_that_cannot_be_quoted_are_listed_with_the_reason(wired: dict[str, Any]) -> None:
    wired["refuse"] = {1}
    rows = [_row(PLAN_A, "tour-a", 1, 1), _row(PLAN_A, "tour-a", 2, 2), _row(PLAN_B, "tour-b", 2, 2, components=0)]
    out = await tariff.build(Conn(rows), partner_id="p", currency="USD", on=ON)
    assert [(i["pax_min"], i["tour"]["source_id"]) for i in out["items"]] == [(2, "tour-a")]
    reasons = {(u["tour"]["source_id"], u["pax_min"]): u["reason"] for u in out["unpriced"]}
    assert reasons == {
        ("tour-b", 2): "the trip plan has no bookable components",
        ("tour-a", 1): "no rate in force on 2026-11-05 for 1 per room",  # the internal line ref is cut off
    }
    assert wired["calls"] == 5  # the chunk failed, then one call per cell (2 rows + their 2 single-room cells)


@pytest.mark.asyncio
async def test_a_missing_cost_fx_rate_leaves_only_that_row_unpriced(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def price_lines(conn: Any, asks: list[LineAsk]) -> list[LineCost]:
        return [LineCost(kind="trip_plan", costs=[(D("82"), "THB" if a.adult == 1 else "USD")]) for a in asks]

    monkeypatch.setattr(tariff, "price_lines", price_lines)
    out = await tariff.build(
        Conn([_row(PLAN_A, "tour-a", 1, 1), _row(PLAN_A, "tour-a", 2, 2)]), partner_id="p", currency="USD", on=ON
    )
    assert [i["pax_min"] for i in out["items"]] == [2]
    assert out["unpriced"][0]["pax_min"] == 1 and "THB" in out["unpriced"][0]["reason"]
    assert wired["basis_kw"]["cost_currencies"] == {"THB", "USD"}


@pytest.mark.asyncio
async def test_tours_not_served_are_left_out_and_a_long_grid_is_truncated(wired: dict[str, Any]) -> None:
    wired["served"] = {"tour-b"}
    rows = [_row(PLAN_A, "tour-a", 2, 2), _row(PLAN_B, "tour-b", 2, 2), _row(PLAN_B, "tour-b", 3, 4)]
    conn = Conn(rows)
    out = await tariff.build(conn, partner_id="p", currency="USD", on=ON, source_id=None, limit=2)
    assert conn.asked == [(ON, None, 3)]  # one row more than the limit tells truncation
    assert out["truncated"] is True and [i["tour"]["source_id"] for i in out["items"]] == ["tour-b"]


@pytest.mark.asyncio
async def test_a_currency_that_is_not_a_quote_currency_is_refused(wired: dict[str, Any]) -> None:
    with pytest.raises(B2BError) as err:
        await tariff.build(Conn([]), partner_id="p", currency="XYZ", on=ON)
    assert err.value.error == "invalid"


@pytest.mark.asyncio
async def test_a_quote_currency_without_an_approved_fx_rate_today_is_the_callers_choice(wired: dict[str, Any]) -> None:
    """S270 (AA-1029 / AA-1030 follow-up): AUD with no rate in force is 422 ``invalid`` before any row is read (never
    503 ``unavailable``, which the CRM shows as "AA cannot be reached"); the message lists what does price today."""
    conn = Conn([_row(PLAN_A, "tour-a", 2, 2)], fx={"EUR": D("0.92"), "VND": D("25000")})
    with pytest.raises(B2BError) as err:
        await tariff.build(conn, partner_id="p", currency="AUD", on=ON)
    assert err.value.error == "invalid" and err.value.status_code == 422
    assert err.value.message == "no approved FX rate for AUD today; AA prices this tariff in USD, EUR, VND"
    assert conn.asked == [] and wired["calls"] == 0 and "basis_kw" not in wired
    assert conn.fx_asked == [(sorted(quotes.QUOTE_CURRENCIES - {"USD"}), ON)]  # one read for every quote currency


@pytest.mark.asyncio
async def test_a_quote_currency_with_a_rate_and_usd_are_priced(wired: dict[str, Any]) -> None:
    eur = Conn([_row(PLAN_A, "tour-a", 2, 2)], fx={"EUR": D("0.92")})
    out = await tariff.build(eur, partner_id="p", currency="EUR", on=ON)
    assert out["currency"] == "EUR" and [i["pax_min"] for i in out["items"]] == [2]
    usd = Conn([_row(PLAN_A, "tour-a", 2, 2)])
    assert (await tariff.build(usd, partner_id="p", currency="USD", on=ON))["items"]
    assert usd.fx_asked == []  # USD is the base currency: nothing to check


@pytest.mark.asyncio
async def test_unreadable_fx_rates_are_aas_side(wired: dict[str, Any]) -> None:
    class Broken(Conn):
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            if sql == repository.FX_SQL:
                raise ConnectionError("db down")
            return await super().fetch(sql, *args)

    with pytest.raises(B2BError) as err:
        await tariff.build(Broken([]), partner_id="p", currency="EUR", on=ON)
    assert (err.value.error, err.value.status_code) == ("unavailable", 503)


@pytest.mark.asyncio
async def test_quotes_still_answer_unavailable_for_a_currency_without_a_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the tariff turns the asked currency's missing rate into 422: ``price_basis`` (``POST /quotes``) is
    unchanged."""

    async def rule_in_force(conn: Any, channel: Any, on: date) -> repository.DatedRule:
        return RULE

    async def fx_rates_in_force(conn: Any, currencies: Any, on: date) -> dict[str, repository.DatedFxRate]:
        return {}

    monkeypatch.setattr(repository, "rule_in_force", rule_in_force)
    monkeypatch.setattr(repository, "fx_rates_in_force", fx_rates_in_force)
    with pytest.raises(B2BError) as err:
        await quotes.price_basis(None, partner_id="p", currency="AUD", cost_currencies=set(), on=ON, require_all=False)
    assert (err.value.error, err.value.status_code) == ("unavailable", 503)


def test_chunks_stay_within_the_quote_lookup_budget() -> None:
    big = _row(PLAN_A, "tour-a", 2, 2, components=MAX_RATE_LOOKUPS // 4)  # 500 lookups a cell
    cells = [tariff.Cell(no=n, row=big, party=2, on=ON, end=ON) for n in range(1, 6)]
    assert [len(group) for group in tariff.chunks(cells)] == [2, 2, 1]
    huge = tariff.Cell(no=9, row=_row(PLAN_A, "t", 2, 2, components=MAX_RATE_LOOKUPS), party=2, on=ON, end=ON)
    assert [len(g) for g in tariff.chunks([huge, *cells[:1]])] == [1, 1]  # alone; price_lines then refuses it


def test_per_person_rounds_up_like_a_quote_line() -> None:
    assert tariff.per_person(D("100.01"), 2, "USD") == D("50.01")
    assert tariff.per_person(D("3001000"), 3, "VND") == D("1001000.00")


def test_the_csv_has_one_line_per_row_and_neutralises_formulas() -> None:
    item = {
        "tour": {"source_system": "cis", "source_id": "tour-a"},
        "title": "=HYPERLINK(1)",
        "country": "Invented Land",
        "duration_days": 3,
        "season": "-Green",
        "valid_from": date(2027, 1, 1),
        "valid_to": date(2027, 3, 31),
        "pax_min": 11,
        "pax_max": None,
        "priced_for_pax": 11,
        "priced_for_date": date(2027, 1, 1),
        "price": {"amount": "1100.00", "currency": "USD"},
        "price_per_person": {"amount": "100.00", "currency": "USD"},
        "agency_discount_pct": "0.0300",
        "rate_card_id": None,
        "rrp_per_person": None,
        "child_price_per_person": {"amount": "70.00", "currency": "USD"},
        "infant_price": {"amount": "0.00", "currency": "USD"},
        "single_supplement_per_person": None,
    }
    text = tariff.to_csv({"items": [item]})
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(tariff.CSV_COLUMNS)
    assert rows[1] == ["tour-a", "'=HYPERLINK(1)", "Invented Land", "3", "'-Green", "2027-01-01", "2027-03-31", "11",
                       "", "11", "USD", "100.00", "1100.00", "0.0300", "", "", "70.00", "0.00", ""]  # fmt: skip
    assert text.endswith("\r\n")
    view = tariff.agency_view(
        {"currency": "USD", "priced_on": ON, "basis": tariff.BASIS, "tier_code": "standard", "fx": {},
         "truncated": False, "items": [item], "unpriced": [{"reason": "x"}]}
    )  # fmt: skip
    assert "unpriced" not in view and "rate_card_id" not in view["items"][0]


def test_the_file_name_is_ascii() -> None:
    assert (
        tariff.filename("Invented Lotus & Co. (Đà Lạt)", "VND", ON)
        == "aa-tariff-invented-lotus-co-l-t-vnd-2026-11-05.csv"
    )
    assert tariff.filename("!!!", "USD", ON) == "aa-tariff-agency-usd-2026-11-05.csv"


ROOM = "33333333-3333-4333-8333-333333333333"
TRANSFER = "44444444-4444-4444-8444-444444444444"
PARTS = [
    {"trip_plan_id": PLAN_A, "day_no": 1, "room_type_id": ROOM, "service_id": None},
    {"trip_plan_id": PLAN_A, "day_no": 2, "room_type_id": None, "service_id": TRANSFER},
]


def _span(item: str, start: date, stop: date) -> dict[str, Any]:
    return {"room_type_id": item if item == ROOM else None, "service_id": item if item == TRANSFER else None,
            "start": start, "stop": stop}  # fmt: skip


@pytest.mark.asyncio
async def test_a_hand_window_over_a_rate_change_is_cut_there_and_each_piece_is_priced_on_its_first_day(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand (or overridden) b2b row is never re-cut by the cost sheet: one year over a room rate change on
    2027-06-01 (and a transfer rate on day 2 that changes on 2027-09-01, so departures from 2027-08-31 use it; the
    31 December departure needs a transfer on 1 January 2028, which has no rate). Each piece is priced on its own
    first day, where a quote of any departure in it answers the same price."""
    net = {date(2027, 1, 1): D("82"), date(2027, 6, 1): D("164"), date(2027, 8, 31): D("164")}
    asked: list[date] = []

    async def price_lines(conn: Any, asks: list[LineAsk]) -> list[LineCost]:
        asked.extend(a.service_date for a in asks)
        for a in asks:
            if a.service_date not in net:
                raise B2BError("invalid", f"line {a.line_ref}: no rate in force on {a.service_date + timedelta(1)}")
        return [LineCost(kind="trip_plan", costs=[(net[a.service_date] * a.adult, "USD")]) for a in asks]

    monkeypatch.setattr(tariff, "price_lines", price_lines)
    spans = [
        _span(ROOM, date(2027, 1, 1), date(2027, 5, 31)),
        _span(ROOM, date(2027, 6, 1), date(2027, 12, 31)),
        _span(TRANSFER, date(2027, 1, 1), date(2027, 8, 31)),
        _span(TRANSFER, date(2027, 9, 1), date(2027, 12, 31)),
    ]
    hand = _row(PLAN_A, "tour-a", 2, 2, valid_from=date(2027, 1, 1), valid_to=date(2027, 12, 31), season="Hand")
    out = await tariff.build(Conn([hand], PARTS, spans), partner_id="p", currency="USD", on=ON)

    assert sorted(set(asked)) == [date(2027, 1, 1), date(2027, 6, 1), date(2027, 8, 31), date(2027, 12, 31)]
    got = [(i["valid_from"], i["valid_to"], i["priced_for_date"], i["price"]["amount"], i["season"])
           for i in out["items"]]  # fmt: skip
    # 2027-06-01 .. 2027-08-30 and 2027-08-31 .. 2027-12-30 price the same (equal transfer nets): joined back.
    assert got == [
        (date(2027, 1, 1), date(2027, 5, 31), date(2027, 1, 1), "200.00", "Hand"),
        (date(2027, 6, 1), date(2027, 12, 30), date(2027, 6, 1), "400.00", "Hand"),
    ]
    assert [(u["valid_from"], u["valid_to"], u["reason"]) for u in out["unpriced"]] == [
        (date(2027, 12, 31), date(2027, 12, 31), "no rate in force on 2028-01-01")
    ]
    rows = list(csv.DictReader(io.StringIO(tariff.to_csv(out))))
    assert [(r["valid_from"], r["valid_to"], r["price_for_party"]) for r in rows] == [
        ("2027-01-01", "2027-05-31", "200.00"),
        ("2027-06-01", "2027-12-30", "400.00"),
    ]


@pytest.mark.asyncio
async def test_a_piece_that_cannot_be_quoted_is_listed_alone_and_the_cells_are_capped(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def price_lines(conn: Any, asks: list[LineAsk]) -> list[LineCost]:
        for a in asks:
            if a.service_date >= date(2027, 6, 1):
                raise B2BError("invalid", f"line {a.line_ref}: no rate in force on {a.service_date}")
        return [LineCost(kind="trip_plan", costs=[(D("82") * a.adult, "USD")]) for a in asks]

    monkeypatch.setattr(tariff, "price_lines", price_lines)
    spans = [_span(ROOM, date(2027, 1, 1), date(2027, 5, 31)), _span(TRANSFER, date(2027, 1, 1), cost_sheet.FOREVER)]
    hand = _row(PLAN_A, "tour-a", 2, 2, valid_from=date(2027, 1, 1), valid_to=date(2027, 12, 31))
    out = await tariff.build(Conn([hand], PARTS, spans), partner_id="p", currency="USD", on=ON)
    assert [(i["valid_from"], i["valid_to"]) for i in out["items"]] == [(date(2027, 1, 1), date(2027, 5, 31))]
    assert [(u["valid_from"], u["valid_to"]) for u in out["unpriced"]] == [(date(2027, 6, 1), date(2027, 12, 31))]

    monkeypatch.setattr(tariff, "MAX_CELLS", 1)
    out = await tariff.build(Conn([hand], PARTS, spans), partner_id="p", currency="USD", on=ON)
    assert len(out["items"]) == 1 and out["unpriced"] == [] and out["truncated"] is True


def test_pieces_cut_a_window_at_the_cuts_inside_it() -> None:
    start, end = date(2027, 1, 1), date(2027, 12, 31)
    assert tariff.pieces(start, end, []) == [(start, end)]
    cuts = [date(2026, 12, 1), start, date(2027, 6, 1), date(2027, 6, 1), date(2028, 1, 1), end]
    assert tariff.pieces(start, end, cuts) == [
        (start, date(2027, 5, 31)),
        (date(2027, 6, 1), end - timedelta(days=1)),
        (end, end),
    ]


def test_the_cost_sheet_windows_use_the_same_cuts() -> None:
    parts = [cost_sheet.Part(1, ROOM, None), cost_sheet.Part(2, None, TRANSFER)]
    spans = [
        cost_sheet.Span(ROOM, date(2027, 1, 1), date(2027, 5, 31)),
        cost_sheet.Span(TRANSFER, date(2027, 1, 1), cost_sheet.FOREVER),
    ]
    assert cost_sheet.rate_cuts(parts, spans) == {date(2027, 1, 1), date(2027, 6, 1), date(2026, 12, 31)}
    wins = cost_sheet.windows(ON, parts, spans)
    assert {w.start for w in wins} <= cost_sheet.rate_cuts(parts, spans)


# ── AA-1075 (bk-15): child, infant and single prices on the tariff ──────────


@pytest.mark.asyncio
async def test_child_infant_and_single_prices_come_from_the_quote_engine(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    rule = repository.DatedRule(
        rule=RULE.rule, valid_from=RULE.valid_from, child_pct=D("0.70"), infant_flat_usd=D("16.40")
    )

    async def price_basis(conn: Any, **kw: Any) -> quotes.PriceBasis:
        return quotes.PriceBasis(
            on=ON, currency="USD", rule=rule, rates={}, fx=quotes.FxLock("USD", D("1"), D("0"), ON, "base"),
            terms=agency_pricing.NO_TERMS,
        )  # fmt: skip

    monkeypatch.setattr(quotes, "price_basis", price_basis)
    out = await tariff.build(Conn([_row(PLAN_A, "tour-a", 2, 2)]), partner_id="p", currency="USD", on=ON)
    (item,) = out["items"]
    assert item["price_per_person"] == {"amount": "100.00", "currency": "USD"}
    # A child pays 70 % of the adult price; an infant adds its net sold at the rule (16.40 / 0.82 = 20).
    assert item["child_price_per_person"] == {"amount": "70.00", "currency": "USD"}
    assert item["infant_price"] == {"amount": "20.00", "currency": "USD"}
    # Every traveller in a single room: 120 per person, so 20 more than twin share.
    assert item["single_supplement_per_person"] == {"amount": "20.00", "currency": "USD"}
    # The same as a quote of that party asks the engine.
    cost = LineCost(kind="trip_plan", costs=[(D("164"), "USD")])
    basis = await price_basis(None)
    assert quotes.price_line(basis, cost, PLAN_A, pax=(0, 2, 0)).b2b_price == D("140.00")
    assert quotes.price_line(basis, cost, PLAN_A, pax=(2, 0, 1)).b2b_price == D("220.00")
    # Sell side only: no net or margin field on a tariff row.
    assert not [k for k in item if "net" in k or "margin" in k]


@pytest.mark.asyncio
async def test_without_child_or_infant_rule_a_child_pays_like_an_adult_and_an_infant_is_free(
    wired: dict[str, Any],
) -> None:
    out = await tariff.build(Conn([_row(PLAN_B, "tour-b", 3, 4)]), partner_id="p", currency="USD", on=ON)
    (item,) = out["items"]
    assert item["child_price_per_person"] == item["price_per_person"] == {"amount": "100.00", "currency": "USD"}
    assert item["infant_price"] == {"amount": "0.00", "currency": "USD"}
    assert item["single_supplement_per_person"] == {"amount": "20.00", "currency": "USD"}


@pytest.mark.asyncio
async def test_a_single_room_without_a_rate_empties_only_the_supplement(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def price_lines(conn: Any, asks: list[LineAsk]) -> list[LineCost]:
        if any(ask.rooms for ask in asks):
            raise B2BError("invalid", f"line {asks[0].line_ref}: no rate in force for 1 per room")
        return [LineCost(kind="trip_plan", costs=[(D("82") * ask.adult, "USD")]) for ask in asks]

    monkeypatch.setattr(tariff, "price_lines", price_lines)
    out = await tariff.build(Conn([_row(PLAN_A, "tour-a", 2, 2)]), partner_id="p", currency="USD", on=ON)
    (item,) = out["items"]
    assert item["price"] == {"amount": "200.00", "currency": "USD"} and item["single_supplement_per_person"] is None
    assert out["unpriced"] == []
    lines = list(csv.reader(io.StringIO(tariff.to_csv(out))))
    assert lines[0][-3:] == ["child_price_per_person", "infant_price", "single_supplement_per_person"]
    assert lines[1][-3:] == ["100.00", "0.00", ""]
