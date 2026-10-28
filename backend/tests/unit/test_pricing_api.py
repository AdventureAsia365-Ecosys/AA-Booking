"""POST /booking/pricing/quote-preview and the pricing repository, with a scripted connection.

Real SQL against bk-004 is covered by tests/integration/test_bk004_pricing.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff
from app.db import Database
from app.main import create_app
from app.pricing import engine, repository
from app.pricing.engine import Channel
from app.settings import Settings
from tests.unit.fakes import ScriptedConn

URL = "/booking/pricing/quote-preview"
STAFF = Staff("7", "sales@example.com", "Sales", "sales", True, "reviewer")

B2C_ROW = {
    "channel": "b2c_tailor_made",
    "target_margin": D("0.3000"),
    "floor_margin": D("0.2000"),
    "peak_uplift_pts": D("0.0300"),
    "card_fee_pct": D("0.0300"),
    "valid_from": date(2026, 10, 14),
}
FX_ROWS = [
    {"currency": "EUR", "rate_per_usd": D("0.92000000"), "buffer_pct": D("0.0200"), "as_of": date(2026, 10, 13),
     "source": "ECB"},
    {"currency": "VND", "rate_per_usd": D("25400.00000000"), "buffer_pct": D("0"), "as_of": date(2026, 10, 13),
     "source": "Vietcombank"},
]  # fmt: skip


class ScriptedDatabase(Database):
    def __init__(self, conn: Any, configured: bool = True) -> None:
        super().__init__(Settings(database_url="postgresql://x/y" if configured else None, environment="test"))
        self.conn = conn

    async def pool(self):  # type: ignore[override]
        if isinstance(self.conn, BaseException):
            raise self.conn
        return self.conn

    async def close(self) -> None:
        return None


def _conn(rule: Any = B2C_ROW, fx: Any = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("FROM booking.pricing_rules", rule)
    conn.on("FROM booking.fx_rates", lambda currencies, on: [r for r in (fx or FX_ROWS) if r["currency"] in currencies])
    return conn


def _client(db: Database, *, authed: bool = True, staff: Staff = STAFF) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"), db)
    if authed:
        app.dependency_overrides[current_staff] = lambda: staff
    return TestClient(app)


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"channel": "b2c_tailor_made", "items": [{"net_amount": "700", "net_currency": "USD"}]}
    body.update(overrides)
    return body


# ── Auth ─────────────────────────────────────────────────────────────────────


def test_rejects_without_staff_auth() -> None:
    conn = _conn()
    with _client(ScriptedDatabase(conn), authed=False) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert conn.calls == []


def test_content_creator_is_forbidden() -> None:
    conn = _conn()
    writer = Staff("9", None, "Writer", "content_creator", True, "content")
    with _client(ScriptedDatabase(conn), staff=writer) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 403
    assert conn.calls == []


@pytest.mark.parametrize("role", ["admin", "sales", "contracting"])
def test_every_role_that_edits_or_prices_a_version_may_quote(role: str) -> None:
    """AA-996 (Figma audit item 11): the builder's live quote must work for the roles that build proposals (ui-spec
    §4.8 / Q8: admin, sales, contracting). The 403 the audit saw was its read-only E2E session, not the role matrix."""
    conn = _conn()
    with _client(ScriptedDatabase(conn), staff=Staff("8", None, role.title(), role, True, "reviewer")) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 200, r.text


# ── Happy paths ──────────────────────────────────────────────────────────────


def test_usd_quote_preview() -> None:
    conn = _conn()
    with _client(ScriptedDatabase(conn)) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channel"] == "b2c_tailor_made"
    assert body["currency"] == "USD"
    assert body["fx_rates"] == []
    assert D(body["total"]) == D("1045")
    assert D(body["final_total"]) == D("1045")
    assert D(body["margin_applied"]) == D("0.30")
    assert D(body["effective_margin"]) == D("0.3001")
    assert body["below_floor"] is False
    assert body["rule"]["valid_from"] == "2026-10-14"
    assert D(body["lines"][0]["sell_usd"]) == D("1044.78")
    # Only USD involved: the FX table is not queried.
    assert conn.sql("FROM booking.fx_rates") == []
    (args,) = conn.sql("FROM booking.pricing_rules")
    assert args[0] == "b2c_tailor_made" and isinstance(args[1], date)


def test_eur_quote_with_vnd_cost_and_peak() -> None:
    conn = _conn()
    body = _body(currency="EUR", peak=True, items=[{"net_amount": "17780000", "net_currency": "VND"}])
    with _client(ScriptedDatabase(conn)) as client:
        r = client.post(URL, json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    # 700 USD / 0.64 = 1093.75 USD × 0.92 × 1.02 = 1026.38 EUR → 1030.
    assert D(out["lines"][0]["net_usd"]) == D("700")
    assert D(out["subtotal"]) == D("1026.38")
    assert D(out["total"]) == D("1030")
    assert D(out["margin_applied"]) == D("0.33")
    assert [fx["currency"] for fx in out["fx_rates"]] == ["EUR", "VND"]
    assert out["fx_rates"][0]["source"] == "ECB"
    (args,) = conn.sql("FROM booking.fx_rates")
    assert args[0] == ["EUR", "VND"]


def test_override_below_floor_flagged() -> None:
    with _client(ScriptedDatabase(_conn())) as client:
        r = client.post(URL, json=_body(override_total="900"))
    assert r.status_code == 200
    out = r.json()
    assert D(out["final_total"]) == D("900")
    assert D(out["total"]) == D("1045")
    assert out["below_floor"] is True


# ── Unprocessable ────────────────────────────────────────────────────────────


def test_no_rule_in_force() -> None:
    with _client(ScriptedDatabase(_conn(rule=None))) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 422
    assert r.json()["detail"] == "no pricing rule in force for channel b2c_tailor_made"


def test_missing_fx_rates_listed() -> None:
    body = _body(currency="GBP", items=[{"net_amount": "1000", "net_currency": "THB"}])
    with _client(ScriptedDatabase(_conn())) as client:
        r = client.post(URL, json=body)
    assert r.status_code == 422
    assert r.json()["detail"] == "no FX rate for GBP, THB"


def test_non_selling_currency() -> None:
    conn = _conn()
    with _client(ScriptedDatabase(conn)) as client:
        r = client.post(URL, json=_body(currency="VND"))
    assert r.status_code == 422
    assert r.json()["detail"] == "VND is not a selling currency"
    assert conn.calls == []


@pytest.mark.parametrize(
    "body",
    [
        _body(items=[]),
        _body(channel="retail"),
        _body(items=[{"net_amount": "-1", "net_currency": "USD"}]),
        _body(items=[{"net_amount": "1.001", "net_currency": "USD"}]),
        _body(items=[{"net_amount": "1", "net_currency": "usd"}]),
        _body(items=[{"net_amount": "NaN", "net_currency": "USD"}]),
        _body(override_total="0"),
        _body(unknown=True),
    ],
)
def test_request_validation(body: dict[str, Any]) -> None:
    with _client(ScriptedDatabase(_conn())) as client:
        r = client.post(URL, json=body)
    assert r.status_code == 422


def test_engine_error_maps_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    # The request model and the FX check catch every input the engine refuses today; the mapping
    # stays as a guard for future engine rules.
    def refuse(*_: Any, **__: Any) -> None:
        raise engine.PricingError("margin + card fee must be below 100%")

    monkeypatch.setattr(engine, "price_quote", refuse)
    with _client(ScriptedDatabase(_conn())) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 422
    assert r.json()["detail"] == "margin + card fee must be below 100%"


def test_zero_cost_quote_is_flagged_below_floor() -> None:
    with _client(ScriptedDatabase(_conn())) as client:
        r = client.post(URL, json=_body(items=[{"net_amount": "0", "net_currency": "USD"}]))
    assert r.status_code == 200
    assert r.json()["below_floor"] is True


# ── Database unavailable ─────────────────────────────────────────────────────


def test_db_not_configured() -> None:
    with _client(ScriptedDatabase(_conn(), configured=False)) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 503
    assert r.json()["detail"] == "database not configured"


def test_db_error_is_not_leaked() -> None:
    with _client(ScriptedDatabase(OSError("password=secret host=db"))) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 503
    assert r.json()["detail"] == "pricing data unavailable"
    assert "secret" not in r.text


def test_invalid_rule_row_is_unavailable_not_500() -> None:
    bad = {**B2C_ROW, "floor_margin": D("0.5")}
    with _client(ScriptedDatabase(_conn(rule=bad))) as client:
        r = client.post(URL, json=_body())
    assert r.status_code == 503


# ── Repository ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fx_rates_skips_query_for_usd_only() -> None:
    conn = _conn()
    assert await repository.fx_rates_in_force(conn, ["USD"], date(2026, 10, 14)) == {}
    assert conn.calls == []


@pytest.mark.asyncio
async def test_fx_rates_strip_char_padding() -> None:
    conn = ScriptedConn()
    conn.on("FROM booking.fx_rates", [{**FX_ROWS[0], "currency": "EUR "}])
    rates = await repository.fx_rates_in_force(conn, ["EUR", "USD"], date(2026, 10, 14))
    assert list(rates) == ["EUR"]
    assert rates["EUR"].rate.rate_per_usd == D("0.92")
    assert rates["EUR"].as_of == date(2026, 10, 13)


@pytest.mark.asyncio
async def test_rule_in_force_maps_row() -> None:
    dated = await repository.rule_in_force(_conn(), Channel.B2C_TAILOR_MADE, date(2026, 10, 14))
    assert dated is not None
    assert dated.rule.channel is Channel.B2C_TAILOR_MADE
    assert dated.rule.card_fee_pct == D("0.03")
    assert dated.valid_from == date(2026, 10, 14)
