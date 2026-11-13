"""AA-996 (bk-032): the price matrix rules in Python (row validation, overlaps, pricing from a net cost through
``app.pricing.service``, the floor reason, no write when nothing changed, permissions) and the HTTP wiring. The SQL
runs in tests/integration/test_bk032_trip_plan_prices_api.py against Postgres."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.catalog import trip_plan_prices as prices
from app.errors import Forbidden, Invalid, Unavailable
from app.main import create_app
from app.pricing import engine
from app.pricing import service as pricing
from app.settings import Settings
from tests.unit.fakes import SEED_ROUNDING, ScriptedConn

CONTRACTING = Staff("u-5", None, "Contracts", "contracting", True, "reviewer")
CONTENT = Staff("u-9", None, "Writer", "content_creator", True, "content")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
SOURCE_ID = "aa996032-0000-4000-8000-00000000000a"
DAY = date(2026, 10, 28)


def row(**over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "channel": "b2c_tailor_made",
        "season": " Low ",
        "peak": False,
        "valid_from": date(2027, 1, 1),
        "valid_to": date(2027, 4, 30),
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD",
        "amount": Decimal("1450"),
        "net_amount": None,
        "net_currency": None,
    }
    out.update(over)
    return out


# ── Validation ───────────────────────────────────────────────────────────────


def test_normalise_trims_and_numbers_rows() -> None:
    rows = prices.normalise([row(), row(pax_min=3, pax_max=None, season="  ")])
    assert [(r.position, r.season, r.pax_max) for r in rows] == [(1, "Low", 2), (2, None, None)]


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"valid_to": date(2026, 12, 31)}, "invalid_validity"),
        ({"pax_min": 4, "pax_max": 3}, "invalid_pax_band"),
        ({"currency": "VND"}, "invalid_currency"),
        ({"net_amount": Decimal("10")}, "invalid_net_cost"),
        ({"net_currency": "USD"}, "invalid_net_cost"),
        ({"amount": None}, "price_missing"),
    ],
)
def test_normalise_refuses_bad_rows(over: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as err:
        prices.normalise([row(**over)])
    assert err.value.code == code and err.value.extra["row"] == 1


@pytest.mark.parametrize(
    ("second", "overlaps"),
    [
        (row(pax_min=1, pax_max=2), True),  # same days, bands share 2 pax
        (row(pax_min=3, pax_max=None), False),  # next band
        (row(valid_from=date(2027, 4, 30), valid_to=date(2027, 6, 30)), True),  # shares the last day
        (row(valid_from=date(2027, 5, 1), valid_to=date(2027, 6, 30)), False),  # next season
        (row(currency="EUR"), False),  # another currency
        (row(channel="b2b"), False),  # another channel
        (row(pax_min=1, pax_max=None), True),  # an open band covers everything above
    ],
)
def test_overlap(second: dict[str, Any], overlaps: bool) -> None:
    if overlaps:
        with pytest.raises(Invalid) as err:
            prices.normalise([row(), second])
        assert err.value.code == "overlapping_prices" and err.value.extra["rows"] == [1, 2]
    else:
        assert len(prices.normalise([row(), second])) == 2


def test_too_many_rows() -> None:
    with pytest.raises(Invalid):
        prices.normalise([row()] * (prices.MAX_ROWS + 1))


def test_from_price_shape() -> None:
    assert prices.from_price(None) is None and prices.from_price({"amount": None}) is None
    assert prices.from_price({"amount": "10.00", "currency": "USD", "season": None}) == {
        "amount": "10.00",
        "currency": "USD",
        "basis": "per_person",
        "season": None,
    }


def test_from_price_sql_reads_the_b2b_rrp_for_two_pax() -> None:
    # AA-1029 (root ADR 0019 b15): the from price is the suggested retail price of a b2b row, as booking.v_from_prices.
    sql = " ".join(prices.FROM_PRICE_SQL.format(plan="$1::uuid").split())
    assert "fp.channel = 'b2b' AND fp.rrp_amount IS NOT NULL" in sql
    assert "fp.pax_min <= 2 AND (fp.pax_max IS NULL OR fp.pax_max >= 2)" in sql
    assert "ORDER BY (fp.currency = 'USD') DESC, fp.rrp_amount" in sql


# ── Service ──────────────────────────────────────────────────────────────────

RULE = engine.PricingRule(
    channel=engine.Channel.B2C_TAILOR_MADE,
    target_margin=Decimal("0.30"),
    floor_margin=Decimal("0.20"),
    peak_uplift_pts=Decimal("0.03"),
    card_fee_pct=Decimal("0.03"),
)


def fake_price(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]] | None = None) -> None:
    async def price(conn: Any, **kw: Any) -> pricing.PricedQuote:
        if calls is not None:
            calls.append(kw)
        quote = engine.price_quote(
            kw["lines"],
            RULE,
            currency=kw["currency"],
            peak=kw["peak"],
            override_total=kw["override_total"],
            increments=SEED_ROUNDING,
        )
        return pricing.PricedQuote(quote=quote, rule=None, rates={}, priced_on=kw["on"])  # type: ignore[arg-type]

    monkeypatch.setattr(prices.pricing, "price", price)


def stored(**over: Any) -> dict[str, Any]:
    out = {**row(season="Low"), "position": 1, "amount": "1450.00", "effective_margin": None, "priced_on": None}
    out["overridden"] = False
    out.update(over)
    return out


def conn_with(plan: str | None = "p-1", existing: list[dict[str, Any]] | None = None) -> ScriptedConn:
    return (
        ScriptedConn()
        .on("FROM booking.trip_plans WHERE", {"id": plan} if plan else None)
        .on("FROM booking.trip_plan_prices pr", existing or [])
        .on("FROM booking.trip_plan_prices fp", None)
    )


@pytest.mark.asyncio
async def test_list_needs_pricing_read_and_is_empty_without_a_plan() -> None:
    with pytest.raises(Forbidden):
        await prices.list_prices(conn_with(), CONTENT, SOURCE_ID)
    assert await prices.list_prices(conn_with(plan=None), SALES, SOURCE_ID) == {
        "source_id": SOURCE_ID,
        "items": [],
        "from_price": None,
        "reference": None,  # AA-1029
        "changes": [],
    }


@pytest.mark.asyncio
async def test_store_with_the_same_rows_writes_nothing() -> None:
    conn = conn_with(existing=[stored()])
    final = await prices.price_rows(conn, [row()], on=DAY)
    changed = await prices.store(
        conn, "p-1", SOURCE_ID, final, actor_type="staff", actor_ref="u", created_by="u", action="x"
    )
    assert changed is False
    assert conn.sql("DELETE FROM booking.trip_plan_prices") == [] and conn.sql("INSERT INTO booking.audit_logs") == []


@pytest.mark.asyncio
async def test_price_rows_prices_net_rows_and_store_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    fake_price(monkeypatch, calls)
    conn = conn_with(existing=[stored()])
    net = row(amount=None, net_amount=Decimal("670"), net_currency="USD", pax_min=3, pax_max=None, peak=True)
    final = await prices.price_rows(conn, [row(), net], on=DAY)
    assert len(calls) == 1 and calls[0]["peak"] is True and calls[0]["on"] == DAY
    assert calls[0]["channel"] is engine.Channel.B2C_TAILOR_MADE and calls[0]["override_total"] is None
    by_position = {r["position"]: r for r in final}
    assert by_position[2]["amount"] == Decimal("1050.00")  # 670 / 0.64 rounded up to 5
    assert by_position[2]["effective_margin"] == Decimal("0.3319") and by_position[2]["priced_on"] == DAY
    assert by_position[1]["amount"] == Decimal("1450") and by_position[1]["effective_margin"] is None
    assert by_position[2]["overridden"] is False and by_position[1]["computed"] is False
    await prices.store(conn, "p-1", SOURCE_ID, final, actor_type="staff", actor_ref="u", created_by="u", action="x.y")
    assert conn.sql("DELETE FROM booking.trip_plan_prices") == [("p-1",)]
    (audit,) = conn.sql("INSERT INTO booking.audit_logs")
    assert "x.y" in audit and "Price matrix: 1 -> 2 rows" in audit


@pytest.mark.asyncio
async def test_override_below_floor_needs_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_price(monkeypatch)
    low = row(amount=Decimal("800"), net_amount=Decimal("670"), net_currency="USD")
    with pytest.raises(Invalid) as err:
        await prices.price_rows(conn_with(), [low], on=DAY)
    assert err.value.code == "override_reason_required" and err.value.extra["rows"] == [1]
    (priced,) = await prices.price_rows(conn_with(), [low], below_floor_reason=" Launch ", on=DAY)
    assert priced["amount"] == Decimal("800") and priced["overridden"] is True  # the override is the price, flagged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("over", "code"),
    [
        # A net cost of 0 prices to 0, which the table refuses (amount > 0): a 422, not a 500.
        ({"amount": None, "net_amount": Decimal("0")}, "price_missing"),
        # A typo override (2000 net, 15 sold): margin about -13236 %, too far for a sales decision.
        ({"amount": Decimal("15"), "net_amount": Decimal("2000")}, "override_too_low"),
        # A price NUMERIC(12, 2) cannot hold.
        ({"amount": None, "net_amount": Decimal("9999999999.99")}, "price_too_high"),
    ],
)
async def test_priced_rows_the_table_cannot_hold_are_refused(
    monkeypatch: pytest.MonkeyPatch, over: dict[str, Any], code: str
) -> None:
    fake_price(monkeypatch)
    bad = row(net_currency="USD", **over)
    with pytest.raises(Invalid) as err:
        await prices.price_rows(conn_with(), [bad], below_floor_reason="Typo", on=DAY)
    assert err.value.code == code and err.value.extra["row"] == 1


@pytest.mark.asyncio
async def test_a_deep_but_plausible_override_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_price(monkeypatch)
    deep = row(amount=Decimal("1100"), net_amount=Decimal("2000"), net_currency="USD")  # margin -0.8483
    (priced,) = await prices.price_rows(conn_with(), [deep], below_floor_reason="Write-off", on=DAY)
    assert priced["amount"] == Decimal("1100") and priced["effective_margin"] == Decimal("-0.8482")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind", "code"),
    [
        (pricing.NoRuleInForce(engine.Channel.B2B), Invalid, "pricing_rule_missing"),
        (pricing.MissingFxRates(["THB"]), Invalid, "fx_rate_missing"),
        (engine.PricingError("bad"), Invalid, "pricing_error"),
        (pricing.PricingDataUnavailable("x"), Unavailable, "pricing_data_unavailable"),
    ],
)
async def test_pricing_errors(monkeypatch: pytest.MonkeyPatch, error: Exception, kind: type, code: str) -> None:
    async def price(conn: Any, **_: Any) -> Any:
        raise error

    monkeypatch.setattr(prices.pricing, "price", price)
    net = row(amount=None, net_amount=Decimal("1000"), net_currency="THB")
    with pytest.raises(kind) as err:
        await prices.price_rows(conn_with(), [net], on=DAY)
    assert err.value.code == code


# ── HTTP ─────────────────────────────────────────────────────────────────────


def client_as(staff: Staff, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def read(conn: Any, who: Staff, source_id: str) -> dict[str, Any]:
        return {"source_id": source_id, "items": [], "from_price": None}

    monkeypatch.setattr(prices, "list_prices", read)
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


def test_http_put_prices_is_retired_for_price_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    """AA-1091 (bk-054): a hand price is never overwritten; PUT …/prices answers 409 and points at the versions."""
    body = {"items": [{"channel": "b2b", "valid_from": "2027-01-01", "valid_to": "2027-04-30", "amount": "1450"}]}
    url = f"/booking/catalog/trip-plans/{SOURCE_ID}/prices"
    with client_as(CONTRACTING, monkeypatch) as c:
        put = c.put(url, json=body)
        assert put.status_code == 409 and put.json()["detail"] == "price_versions"
        assert put.json()["versions_url"].endswith("/price-versions")
        assert c.put(url, json={"items": [{**body["items"][0], "pax_min": 0}]}).status_code == 422
    with client_as(SALES, monkeypatch) as c:
        assert c.get(url).status_code == 200
        assert c.put(url, json=body).status_code == 403
    with client_as(CONTENT, monkeypatch) as c:
        assert c.get(url).status_code == 403
