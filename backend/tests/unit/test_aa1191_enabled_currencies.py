"""AA-1191 without a database: the enabled currencies read from CIS (``catalog.v_billing_currencies_v1``, CIS 497), the
SaaS draft of an agency whose CIS billing currency is not enabled (drafted in USD from the USD list), the refused SaaS
currency on the billing terms write (422 ``currency_not_enabled``) and the terms list serving the enabled currencies.
Names are invented."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agency_billing import config, currencies, drafts
from app.auth.staff import Staff, current_staff, get_pool
from app.catalog import liveness
from app.errors import Invalid
from app.main import create_app
from app.settings import Settings
from tests.unit.fakes import VIEW_READY_FRAGMENT, ScriptedConn

FIN = Staff("fin-1", None, "Finance One", "finance", True, None)
T1 = uuid.UUID("00000000-0000-4000-8000-000000000b91")
INV = "10000000-0000-4000-8000-000000000191"
TERMS = {"id": "t", "party_tenant_id": None, "deposit_pct": Decimal("0.3000"), "balance_days_before_departure": 30,
         "saas_due_days": 14, "settlement_due_days": 14, "warn_after_days": 1, "suspend_after_days": 30,
         "saas_currency": "USD"}  # fmt: skip
BODY = {k: v for k, v in TERMS.items() if k not in ("id", "party_tenant_id")} | {"deposit_pct": "0.3"}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    liveness.reset_cache()


# ── pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, ("USD",)),
        ([], ("USD",)),
        (["VND"], ("USD", "VND")),
        (["vnd", "USD", "VND"], ("USD", "VND")),
        (["USD", 5, ""], ("USD",)),
    ],  # fmt: skip
)
def test_normalise(raw: Any, want: tuple[str, ...]) -> None:
    assert currencies.normalise(raw) == want


def test_effective_and_problem() -> None:
    assert currencies.effective("VND", ("USD",)) == "USD"
    assert currencies.effective("VND", ("USD", "VND")) == "VND"
    assert currencies.effective(None, ("USD",)) == "USD"
    assert currencies.problem("USD", ("USD",)) is None
    assert currencies.problem("", ("USD",)) is None
    why = currencies.problem("VND", ("USD",), "saas_currency")
    assert why is not None and why.startswith("saas_currency: VND is not an enabled currency")
    with pytest.raises(Invalid) as e:
        currencies.refuse_unless_enabled("VND", ("USD",), "saas_currency")
    assert e.value.code == currencies.CODE


# ── the CIS read ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usd_only_without_the_cis_view() -> None:
    assert await currencies.enabled(ScriptedConn([(VIEW_READY_FRAGMENT, False)])) == ("USD",)


@pytest.mark.asyncio
async def test_the_value_in_force_from_the_cis_view() -> None:
    conn = ScriptedConn([(VIEW_READY_FRAGMENT, True),
                         ("FROM catalog.v_billing_currencies_v1", {"enabled_currencies": ["USD", "VND"]})])  # fmt: skip
    assert await currencies.enabled(conn) == ("USD", "VND")


# ── the SaaS draft ───────────────────────────────────────────────────────────


def _sub(**over: Any) -> dict[str, Any]:
    row = {"tenant_id": T1, "product": "content", "plan_key": "content.growth", "plan_name": "Growth",
           "price_usd_monthly": Decimal("349"), "price_unit": "tenant", "seats": None, "seats_included": None,
           "price_extra_seat_usd": None, "kind": "demo", "billing_country": "TH", "billing_currency": "VND",
           "price_vnd_monthly": Decimal("8990000"), "price_extra_seat_vnd": None, "agreed_price_monthly": None,
           "agreed_currency": None, "agreed_until": None}  # fmt: skip
    row.update(over)
    return row


def _saas_conn(enabled: list[str] | None) -> ScriptedConn:
    c = ScriptedConn()
    c.on(VIEW_READY_FRAGMENT, lambda *a: enabled is not None or "v_billing_currencies_v1" not in str(a))
    c.on("FROM catalog.v_billing_currencies_v1", {"enabled_currencies": enabled or ["USD"]})
    c.on("FROM catalog.v_tenant_billing_v1", [_sub()])
    c.on("SELECT tenant_id, agency_name FROM booking.b2b_partners", [{"tenant_id": T1, "agency_name": "Zz Juniper"}])
    c.on("FROM booking.billing_terms", TERMS)
    c.on("status FROM booking.agency_invoice WHERE party_tenant_id", None)
    c.on("FROM booking.tax_rule", None)
    c.on("SELECT current_date", date(2026, 11, 5))
    c.on("INSERT INTO booking.agency_invoice (", INV)
    c.on("coalesce(max(line_no), 0) FROM", 0)
    c.on("SELECT tax_rate FROM", Decimal(0))
    c.on("coalesce(sum(amount), 0) FROM booking.agency_invoice_line", Decimal(349))
    return c


def _invoice_currency(c: ScriptedConn) -> str:
    (args,) = c.sql("INSERT INTO booking.agency_invoice (")
    return next(a for a in args if a in ("USD", "VND"))


def _line_prices(c: ScriptedConn) -> list[Decimal]:
    return [a for args in c.sql("INSERT INTO booking.agency_invoice_line") for a in args
            if isinstance(a, Decimal) and a > 1]  # fmt: skip


@pytest.mark.asyncio
async def test_a_vnd_agency_is_drafted_in_usd_while_vnd_is_disabled() -> None:
    c = _saas_conn(["USD"])
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["invoices_created"] == 1 and out["currency_not_enabled"] == 1
    assert _invoice_currency(c) == "USD"
    assert Decimal("349") in _line_prices(c) and Decimal("8990000") not in _line_prices(c)


@pytest.mark.asyncio
async def test_usd_only_when_the_view_is_missing() -> None:
    c = _saas_conn(None)
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["currency_not_enabled"] == 1 and _invoice_currency(c) == "USD"


@pytest.mark.asyncio
async def test_vnd_comes_back_when_cis_enables_it() -> None:
    c = _saas_conn(["USD", "VND"])
    out = await drafts.run_saas(c, period="2026-11", tenant_id=None, actor="job:x")
    assert out["currency_not_enabled"] == 0 and _invoice_currency(c) == "VND"


# ── the billing terms write and read ─────────────────────────────────────────


def _terms_conn(stored: dict[str, Any] | None, enabled: list[str]) -> ScriptedConn:
    c = ScriptedConn()
    c.on(VIEW_READY_FRAGMENT, True)
    c.on("FROM catalog.v_billing_currencies_v1", {"enabled_currencies": enabled})
    c.on("SELECT 1 FROM booking.b2b_partners", 1)
    c.on("FOR UPDATE", stored)
    c.on("INSERT INTO booking.billing_terms", lambda *a: {**TERMS, "saas_currency": a[7]})
    c.on("UPDATE booking.billing_terms", lambda *a: {**TERMS, "saas_currency": a[7]})
    return c


@pytest.mark.asyncio
async def test_put_terms_refuses_a_disabled_currency() -> None:
    body = {**TERMS, "saas_currency": "VND"}
    with pytest.raises(Invalid) as e:
        await config.put_terms(_terms_conn(TERMS, ["USD"]), None, body, "fin-1")
    assert e.value.code == "currency_not_enabled"
    with pytest.raises(Invalid):
        await config.put_terms(_terms_conn(None, ["USD"]), str(T1), body, "fin-1")


@pytest.mark.asyncio
async def test_put_terms_accepts_a_stored_value_sent_back_and_an_enabled_one() -> None:
    stored = {**TERMS, "saas_currency": "VND"}
    out = await config.put_terms(_terms_conn(stored, ["USD"]), None, {**TERMS, "saas_currency": "VND"}, "fin-1")
    assert out["saas_currency"] == "VND"
    out = await config.put_terms(_terms_conn(TERMS, ["USD", "VND"]), None, {**TERMS, "saas_currency": "VND"}, "fin-1")
    assert out["saas_currency"] == "VND"


@pytest.mark.asyncio
async def test_a_save_without_the_currency_keeps_the_stored_vnd() -> None:
    """A deposit-only save from the staff page (no saas_currency sent) leaves a stored VND row in VND."""
    body = {k: v for k, v in TERMS.items() if k != "saas_currency"} | {"deposit_pct": Decimal("0.2500")}
    override = {**TERMS, "party_tenant_id": T1, "saas_currency": "VND"}
    out = await config.put_terms(_terms_conn(override, ["USD"]), str(T1), body, "fin-1")
    assert out["saas_currency"] == "VND"
    out = await config.put_terms(_terms_conn({**TERMS, "saas_currency": "VND"}, ["USD"]), None, body, "fin-1")
    assert out["saas_currency"] == "VND"


@pytest.mark.asyncio
async def test_a_new_override_without_the_currency_takes_the_enabled_default_else_usd() -> None:
    body = {k: v for k, v in TERMS.items() if k != "saas_currency"}
    c = _terms_conn(None, ["USD"]).on("WHERE party_tenant_id IS NULL", "VND")
    assert (await config.put_terms(c, str(T1), body, "fin-1"))["saas_currency"] == "USD"
    c = _terms_conn(None, ["USD", "VND"]).on("WHERE party_tenant_id IS NULL", "VND")
    assert (await config.put_terms(c, str(T1), body, "fin-1"))["saas_currency"] == "VND"
    assert (await config.put_terms(_terms_conn(None, ["USD"]), None, body, "fin-1"))["saas_currency"] == "USD"


def test_the_route_keeps_the_stored_currency_when_the_body_leaves_it_out() -> None:
    stored = {**TERMS, "saas_currency": "VND"}
    c = _client(_terms_conn(stored, ["USD"]))
    body = {k: v for k, v in BODY.items() if k != "saas_currency"}
    resp = c.put("/booking/billing-terms/default", json=body)
    assert resp.status_code == 200 and resp.json()["saas_currency"] == "VND"


def _client(conn: ScriptedConn) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: FIN
    app.dependency_overrides[get_pool] = lambda: conn
    return TestClient(app)


def test_the_route_answers_422_and_the_list_serves_the_enabled_currencies() -> None:
    c = _client(_terms_conn(TERMS, ["USD"]).on("FROM booking.billing_terms t", []))
    resp = c.put("/booking/billing-terms/default", json={**BODY, "saas_currency": "VND"})
    assert resp.status_code == 422 and "currency_not_enabled" in resp.text
    listed = c.get("/booking/billing-terms")
    assert listed.status_code == 200 and listed.json()["enabled_currencies"] == ["USD"]
