"""AA-810 maker-checker over HTTP on a real PostgreSQL (bk-007): /booking/pricing/rules and /booking/pricing/fx-rates,
approve by a second person, pending rows never price, every write audited, USD seed quotes unchanged."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration.conftest import MIGRATIONS
from tests.staff_tokens import WORKFORCE_ISSUER, LocalStaff

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK004 = next(p for p in MIGRATIONS if p.name.startswith("004_"))
BK007 = next(p for p in MIGRATIONS if p.name.startswith("007_"))
SEED_DAY = datetime(2026, 10, 14, tzinfo=UTC).date()
MAKER, CHECKER, SALES = "it-maker-api", "it-checker-api", "it-sales-api"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(BK004.read_text(encoding="utf-8"))
    await conn.execute(BK007.read_text(encoding="utf-8"))
    await conn.execute("DELETE FROM booking.pricing_rules WHERE valid_from <> $1", SEED_DAY)
    await conn.execute("DELETE FROM booking.fx_rates")
    for staff_id, role in ((MAKER, "contracting"), (CHECKER, "admin"), (SALES, "sales")):
        # Linked to the local WORKFORCE identity whose sub is the row key (AA-911: WORKFORCE is the only sign-in).
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, cognito_iss, cognito_sub)"
            " VALUES ($1, $2, true, $3, $1)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true,"
            " cognito_iss = EXCLUDED.cognito_iss, cognito_sub = EXCLUDED.cognito_sub",
            staff_id,
            role,
            WORKFORCE_ISSUER,
        )
    try:
        yield conn
    finally:
        await conn.execute("DELETE FROM booking.pricing_rules WHERE valid_from <> $1", SEED_DAY)
        await conn.execute("DELETE FROM booking.fx_rates")
        await conn.close()


async def test_fx_and_rule_maker_checker(admin: asyncpg.Connection, migrated_booking_dsn: str) -> None:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalStaff()
    app = create_app(settings, db, cis.verifier())
    # The WORKFORCE group carries the same role as the fixture row.
    maker = {"Authorization": f"Bearer {cis.token(MAKER, 'contracting')}"}
    checker = {"Authorization": f"Bearer {cis.token(CHECKER, 'admin')}"}
    sales = {"Authorization": f"Bearer {cis.token(SALES, 'sales')}"}
    today = datetime.now(UTC).date()
    preview = {"channel": "b2c_tailor_made", "items": [{"net_amount": "1000", "net_currency": "USD"}]}
    fx = {"currency": "EUR", "rate_per_usd": "0.92", "buffer_pct": "0.02", "as_of": str(today), "source": "manual-dev"}
    rule = {
        "channel": "b2b",
        "target_margin": "0.16",
        "floor_margin": "0.10",
        "card_fee_pct": "0.03",
        "valid_from": str(today + timedelta(days=7)),
        "note": "test",
    }
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking/booking/pricing") as client:
            usd = await client.post("/quote-preview", json=preview, headers=sales)
            eur_before = await client.post("/quote-preview", json={**preview, "currency": "EUR"}, headers=sales)
            sales_write = await client.post("/fx-rates", json=fx, headers=sales)
            created = await client.post("/fx-rates", json=fx, headers=maker)
            fx_id = created.json()["id"]
            duplicate = await client.post("/fx-rates", json=fx, headers=checker)
            usd_row = await client.post("/fx-rates", json={**fx, "currency": "USD"}, headers=maker)
            pending_preview = await client.post("/quote-preview", json={**preview, "currency": "EUR"}, headers=sales)
            own = await client.post(f"/fx-rates/{fx_id}/approve", headers=maker)
            sales_approve = await client.post(f"/fx-rates/{fx_id}/approve", headers=sales)
            approved = await client.post(f"/fx-rates/{fx_id}/approve", headers=checker)
            again = await client.post(f"/fx-rates/{fx_id}/approve", headers=checker)
            missing = await client.post("/fx-rates/00000000-0000-0000-0000-000000000000/approve", headers=checker)
            eur_after = await client.post("/quote-preview", json={**preview, "currency": "EUR"}, headers=sales)
            fx_list = await client.get("/fx-rates", headers=sales)
            fx_list_eur = await client.get("/fx-rates?currency=EUR", headers=maker)

            new_rule = await client.post("/rules", json=rule, headers=checker)
            rule_id = new_rule.json()["id"]
            rule_dup = await client.post("/rules", json=rule, headers=maker)
            bad_rule = await client.post(
                "/rules", json={**rule, "floor_margin": "0.20", "valid_from": "2031-01-01"}, headers=maker
            )
            rules_before = await client.get("/rules?channel=b2b", headers=checker)
            rule_own = await client.post(f"/rules/{rule_id}/approve", headers=checker)
            rule_ok = await client.post(f"/rules/{rule_id}/approve", headers=maker)
            rules_after = await client.get("/rules", headers=sales)
    finally:
        await db.close()

    # Seed rules stay in force (approved as seed:bk-004): 1000 USD / (1 - 0.30 - 0.03) = 1492.54 -> 1495.
    assert usd.status_code == 200, usd.text
    assert D(usd.json()["final_total"]) == D("1495")
    assert eur_before.status_code == 422 and "EUR" in eur_before.text

    assert sales_write.status_code == 403
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "pending" and body["approved_by"] is None and body["can_approve"] is False
    assert body["created_by"]["id"] == MAKER and body["source"] == "manual-dev"
    assert duplicate.status_code == 409 and duplicate.json()["detail"] == "fx_rate_exists"
    assert usd_row.status_code == 422 and usd_row.json()["detail"] == "usd_is_base"
    assert pending_preview.status_code == 422  # a pending rate never prices

    assert own.status_code == 403 and own.json()["detail"] == "own_row"
    assert sales_approve.status_code == 403
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "in_force" and approved.json()["approved_by"]["id"] == CHECKER
    assert again.status_code == 409 and again.json()["detail"] == "already_approved"
    assert missing.status_code == 404 and missing.json()["detail"] == "fx_rate_not_found"
    assert eur_after.status_code == 200, eur_after.text
    assert eur_after.json()["fx_rates"][0]["source"] == "manual-dev"

    assert fx_list.status_code == 200 and [r["id"] for r in fx_list.json()["items"]] == [fx_id]
    assert fx_list.json()["items"][0]["can_approve"] is False  # sales may read, not approve
    assert [r["currency"] for r in fx_list_eur.json()["items"]] == ["EUR"]

    assert new_rule.status_code == 201, new_rule.text
    assert new_rule.json()["status"] == "pending" and D(new_rule.json()["target_margin"]) == D("0.16")
    assert rule_dup.status_code == 409 and rule_dup.json()["detail"] == "rule_exists"
    assert bad_rule.status_code == 422 and bad_rule.json()["detail"] == "invalid_rule"
    b2b = rules_before.json()["items"]
    assert {(r["valid_from"], r["status"]) for r in b2b} == {
        (str(SEED_DAY), "in_force"),
        (rule["valid_from"], "pending"),
    }
    assert [r["can_approve"] for r in b2b if r["id"] == rule_id] == [False]  # the checker created this one
    assert rule_own.status_code == 403
    assert rule_ok.status_code == 200 and rule_ok.json()["status"] == "scheduled"
    statuses = {(r["channel"], r["status"]) for r in rules_after.json()["items"]}
    assert ("b2b", "scheduled") in statuses and ("b2c_tailor_made", "in_force") in statuses

    actions = [
        r["action"]
        for r in await admin.fetch(
            "SELECT action FROM booking.audit_logs WHERE entity_type IN ('fx_rate', 'pricing_rule')"
            " AND entity_id = ANY($1::uuid[]) ORDER BY created_at",
            [fx_id, rule_id],
        )
    ]
    assert actions == ["fx_rate.created", "fx_rate.approved", "pricing_rule.created", "pricing_rule.approved"]
