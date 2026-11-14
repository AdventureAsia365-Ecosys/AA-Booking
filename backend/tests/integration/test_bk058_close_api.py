"""S280 (bk-058) on a real PostgreSQL through the API: a pending FX rate and pricing rule are rejected by a second
person (with a reason) or withdrawn by their maker; a closed row is listed with its status, never approved, never
closed twice, and never prices anything."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration.test_bk007_policy_api import CHECKER, MAKER, SALES, admin  # noqa: F401 — fixture
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_reject_and_withdraw_pending_rows(admin: asyncpg.Connection, migrated_booking_dsn: str) -> None:  # noqa: F811
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    maker = {"Authorization": f"Bearer {cis.token(sub=MAKER, cis_role='reviewer')}"}
    checker = {"Authorization": f"Bearer {cis.token(sub=CHECKER, cis_role='admin')}"}
    sales = {"Authorization": f"Bearer {cis.token(sub=SALES, cis_role='reviewer')}"}
    today = datetime.now(UTC).date()
    preview = {"channel": "b2c_tailor_made", "currency": "EUR", "items": [{"net_amount": "100", "net_currency": "USD"}]}
    fx = {"currency": "EUR", "rate_per_usd": "0.92", "buffer_pct": "0.02", "as_of": str(today), "source": "it-1096"}
    rule = {"channel": "b2b", "target_margin": "0.16", "floor_margin": "0.10", "valid_from": str(today + timedelta(9))}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking/booking/pricing") as client:
            fx_id = (await client.post("/fx-rates", json=fx, headers=maker)).json()["id"]
            listed = (await client.get("/fx-rates?currency=EUR", headers=checker)).json()["items"][0]
            own_reject = await client.post(f"/fx-rates/{fx_id}/reject", json={"reason": "x"}, headers=maker)
            sales_reject = await client.post(f"/fx-rates/{fx_id}/reject", json={"reason": "x"}, headers=sales)
            other_withdraw = await client.post(f"/fx-rates/{fx_id}/withdraw", headers=checker)
            rejected = await client.post(f"/fx-rates/{fx_id}/reject", json={"reason": "Wrong rate"}, headers=checker)
            again = await client.post(f"/fx-rates/{fx_id}/withdraw", headers=maker)
            approve_closed = await client.post(f"/fx-rates/{fx_id}/approve", headers=checker)
            priced = await client.post("/quote-preview", json=preview, headers=sales)

            rule_id = (await client.post("/rules", json=rule, headers=maker)).json()["id"]
            withdrawn = await client.post(f"/rules/{rule_id}/withdraw", headers=maker)
            rules = (await client.get("/rules?channel=b2b", headers=sales)).json()["items"]
    finally:
        await db.close()

    assert (listed["can_reject"], listed["can_withdraw"], listed["status"]) == (True, False, "pending")
    assert own_reject.status_code == 403 and own_reject.json()["detail"] == "own_row"
    assert sales_reject.status_code == 403
    assert other_withdraw.status_code == 403 and other_withdraw.json()["detail"] == "not_maker"
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "rejected" and body["close_reason"] == "Wrong rate"
    assert body["closed_by"]["id"] == CHECKER and body["can_approve"] is False and body["can_withdraw"] is False
    assert again.status_code == 409 and again.json()["detail"] == "already_decided"
    assert approve_closed.status_code == 409 and approve_closed.json()["detail"] == "already_decided"
    assert priced.status_code == 422  # a rejected rate never prices

    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn" and withdrawn.json()["closed_by"]["id"] == MAKER
    assert [r["status"] for r in rules if r["id"] == rule_id] == ["withdrawn"]
    audit = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_id = ANY($1::uuid[])",
        [fx_id, rule_id],
    )
    actions = [r["action"] for r in audit]
    assert "fx_rate.rejected" in actions and "pricing_rule.withdrawn" in actions
