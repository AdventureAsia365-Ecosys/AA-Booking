"""AA-1030 G3 agency tariff (``app.b2b.tariff``) without a database: the grid, the pricing through the quote engine's
own functions, the rows left unpriced and why, the CSV and the agency view. End to end (the tariff equals a real
quote for several partners, tiers and cards): ``tests/integration/test_aa1030_tariff.py``."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest

from app.b2b import agency_pricing, quotes, tariff
from app.b2b.components import MAX_RATE_LOOKUPS, LineAsk, LineCost
from app.b2b.errors import B2BError
from app.catalog import liveness
from app.pricing import engine, repository

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
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.asked: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
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
        return [LineCost(kind="trip_plan", costs=[(D("82") * ask.adult, "USD")]) for ask in asks]

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
    assert wired["calls"] == 3  # the chunk failed, then one call per cell


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


def test_chunks_stay_within_the_quote_lookup_budget() -> None:
    big = _row(PLAN_A, "tour-a", 2, 2, components=MAX_RATE_LOOKUPS // 4)  # 500 lookups a cell
    cells = [tariff.Cell(no=n, row=big, party=2, on=ON) for n in range(1, 6)]
    assert [len(group) for group in tariff.chunks(cells)] == [2, 2, 1]
    huge = tariff.Cell(no=9, row=_row(PLAN_A, "t", 2, 2, components=MAX_RATE_LOOKUPS), party=2, on=ON)
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
    }
    text = tariff.to_csv({"items": [item]})
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(tariff.CSV_COLUMNS)
    assert rows[1] == ["tour-a", "'=HYPERLINK(1)", "Invented Land", "3", "'-Green", "2027-01-01", "2027-03-31", "11",
                       "", "11", "USD", "100.00", "1100.00", "0.0300", "", ""]  # fmt: skip
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
