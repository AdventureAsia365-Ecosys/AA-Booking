"""AA-1091 (bk-050, root ADR 0005b d15) on a real PostgreSQL: the pricing policy parameters over HTTP.

The bk-050 seed is in force; a change is a new version (draft or pending) that a second person of the parameter's
approver group approves; it never edits a value, never applies backwards and never passes its own maker. The tables
refuse an UPDATE and a self-approval from role booking. Every test leaves the values in force as the seed has them
(a change it approves is changed back), so the other modules keep the seed policy. Staff ids are invented."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.pricing import policy_params, policy_values
from app.settings import Settings
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER, CHECKER, SALES, FINANCE, FINANCE2, CONTENT = (
    "it-1091-maker",
    "it-1091-checker",
    "it-1091-sales",
    "it-1091-finance",
    "it-1091-finance2",
    "it-1091-content",
)
ROLES = {
    MAKER: "contracting",
    CHECKER: "admin",
    SALES: "sales",
    FINANCE: "finance",
    FINANCE2: "finance",
    CONTENT: "content_creator",
}


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    for staff_id, role in ROLES.items():
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ($1, $2, true)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
            staff_id,
            role,
        )
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def api(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[tuple[httpx.AsyncClient, dict]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    headers = {who: {"Authorization": f"Bearer {cis.token(sub=who, cis_role='reviewer')}"} for who in ROLES}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking/booking/pricing") as client:
            yield client, headers
    finally:
        await db.close()


def _item(listing: dict, key: str) -> dict:
    return next(i for i in listing["items"] if i["key"] == key)


async def test_seed_is_in_force_and_readable(api: tuple[httpx.AsyncClient, dict], admin: asyncpg.Connection) -> None:
    client, h = api
    listing = await client.get("/policy", headers=h[SALES])
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert [i["key"] for i in body["items"]] == list(policy_params.PARAMS)
    validity = _item(body, "quote_validity_days")
    assert validity["current"]["value"] == 14 and validity["current"]["status"] == "in_force"
    assert validity["current"]["approved_by"]["id"] == "tech lead S278"
    assert validity["current"]["created_by"]["id"] == "seed:bk-050"
    assert validity["in_use"] is True and validity["unit"] == "days"
    assert _item(body, "surcharge_causes")["current"]["value"] == ["fx", "tax"]
    assert _item(body, "agency_currency_by_country")["current"]["value"] == {"*": "USD", "VN": "VND"}
    assert all(i["current"] is not None for i in body["items"])
    # Every seeded value is valid for its parameter (the registry and the migration agree).
    policy = await policy_values.load(admin, datetime.now(UTC).date())
    for key, value in policy.values.items():
        assert policy_params.normalise(key, value, policy.values) == value, key
    # A role without the permission does not read it.
    assert (await client.get("/policy", headers=h[CONTENT])).status_code == 403


async def test_change_needs_a_second_person_and_never_applies_backwards(
    api: tuple[httpx.AsyncClient, dict], admin: asyncpg.Connection
) -> None:
    client, h = api
    today = datetime.now(UTC).date()
    change = {"key": "quote_validity_days", "value": 10, "effective_from": str(today), "reason": "Shorter quotes"}

    assert (await client.post("/policy", json=change, headers=h[SALES])).status_code == 403
    out_of_range = await client.post("/policy", json={**change, "value": 45}, headers=h[MAKER])
    assert out_of_range.status_code == 422 and out_of_range.json()["detail"] == "invalid_value"
    past = await client.post(
        "/policy", json={**change, "effective_from": str(today - timedelta(days=1))}, headers=h[MAKER]
    )
    assert past.status_code == 422 and past.json()["detail"] == "effective_in_past"
    same = await client.post("/policy", json={**change, "value": 14}, headers=h[MAKER])
    assert same.status_code == 422 and same.json()["detail"] == "no_change"
    festive = await client.post(
        "/policy", json={**change, "key": "quote_validity_festive_days", "value": 20}, headers=h[MAKER]
    )
    assert festive.status_code == 422 and "quote_validity_days" in festive.text
    no_reason = await client.post("/policy", json={**change, "reason": "  "}, headers=h[MAKER])
    assert no_reason.status_code == 422

    created = await client.post("/policy", json=change, headers=h[MAKER])
    assert created.status_code == 201, created.text
    version = created.json()
    assert version["status"] == "pending" and version["version_no"] == 2
    assert version["diff"] == {"from": 14, "to": 10} and version["can_approve"] is False
    second = await client.post("/policy", json={**change, "value": 12}, headers=h[CHECKER])
    assert second.status_code == 409 and second.json()["detail"] == "open_version"
    # Still the seed until approved.
    assert (await policy_values.load(admin, today)).quote_validity_days == 14

    own = await client.post(f"/policy/{version['id']}/approve", headers=h[MAKER])
    assert own.status_code == 403 and own.json()["detail"] == "own_row"
    finance = await client.post(f"/policy/{version['id']}/approve", headers=h[FINANCE])
    assert finance.status_code == 403 and finance.json()["detail"] == "approver_group"
    approved = await client.post(f"/policy/{version['id']}/approve", headers=h[CHECKER])
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "in_force"
    again = await client.post(f"/policy/{version['id']}/approve", headers=h[CHECKER])
    assert again.status_code == 409
    assert (await policy_values.load(admin, today)).quote_validity_days == 10

    listing = (await client.get("/policy", headers=h[SALES])).json()
    item = _item(listing, "quote_validity_days")
    assert item["current"]["version_no"] == 2
    assert [v["status"] for v in item["history"]] == ["in_force", "superseded"]
    audit = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_type = 'pricing_policy' AND entity_id = $1::uuid"
        " ORDER BY created_at",
        version["id"],
    )
    assert [r["action"] for r in audit] == ["pricing_policy.submitted", "pricing_policy.approved"]

    # Back to the seed value (a new version; the later approval of the same day wins).
    back = await client.post("/policy", json={**change, "value": 14, "reason": "Back to 14"}, headers=h[CHECKER])
    assert back.status_code == 201, back.text
    restored = await client.post(f"/policy/{back.json()['id']}/approve", headers=h[MAKER])
    assert restored.status_code == 200, restored.text
    assert (await policy_values.load(admin, today)).quote_validity_days == 14


async def test_draft_submit_withdraw_reject_and_finance_group(api: tuple[httpx.AsyncClient, dict]) -> None:
    client, h = api
    later = str(datetime.now(UTC).date() + timedelta(days=30))
    change = {"key": "surcharge_notice_days", "value": 25, "effective_from": later, "reason": "More notice"}

    draft = await client.post("/policy", json={**change, "submit": False}, headers=h[FINANCE])
    assert draft.status_code == 201 and draft.json()["status"] == "draft" and draft.json()["can_submit"]
    # Saving again replaces the maker's own draft (withdrawn), never edits it.
    redraft = await client.post("/policy", json={**change, "value": 30, "submit": False}, headers=h[FINANCE])
    assert redraft.status_code == 201, redraft.text
    listing = (await client.get("/policy", headers=h[FINANCE])).json()
    statuses = {v["id"]: v["status"] for v in _item(listing, "surcharge_notice_days")["history"]}
    assert statuses[draft.json()["id"]] == "withdrawn" and statuses[redraft.json()["id"]] == "draft"
    vid = redraft.json()["id"]

    assert (await client.post(f"/policy/{vid}/submit", headers=h[FINANCE2])).status_code == 403  # not the maker
    submitted = await client.post(f"/policy/{vid}/submit", headers=h[FINANCE])
    assert submitted.status_code == 200 and submitted.json()["status"] == "pending"
    pricing_lead = await client.post(f"/policy/{vid}/approve", headers=h[MAKER])  # contracting: not finance's group
    assert pricing_lead.status_code == 403 and pricing_lead.json()["detail"] == "approver_group"
    no_reason = await client.post(f"/policy/{vid}/reject", json={}, headers=h[FINANCE2])
    assert no_reason.status_code == 422
    rejected = await client.post(
        f"/policy/{vid}/reject", json={"reason": "Keep the legal minimum"}, headers=h[FINANCE2]
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert rejected.json()["reject_reason"] == "Keep the legal minimum"

    pending = await client.post("/policy", json=change, headers=h[FINANCE])
    assert pending.status_code == 201
    assert (await client.post(f"/policy/{pending.json()['id']}/withdraw", headers=h[FINANCE2])).status_code == 403
    withdrawn = await client.post(f"/policy/{pending.json()['id']}/withdraw", headers=h[FINANCE])
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    final = (await client.get("/policy", headers=h[FINANCE])).json()
    item = _item(final, "surcharge_notice_days")
    assert item["open"] is None and item["current"]["value"] == 20


async def test_tables_are_append_only_and_refuse_self_approval(migrated_booking_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():  # a savepoint: the error leaves the outer transaction usable
                await conn.execute(
                    "UPDATE booking.pricing_policy SET value = '30' WHERE param_key = 'quote_validity_days'"
                )
        vid = await conn.fetchval(
            "INSERT INTO booking.pricing_policy (param_key, version_no, value, effective_from, reason, replaces_id,"
            " created_by) SELECT 'foc_one_per_pax', 99, '20', current_date, 'test', id, 'it-1091-db'"
            " FROM booking.pricing_policy WHERE param_key = 'foc_one_per_pax' AND version_no = 1 RETURNING id"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor)"
                " VALUES ($1, 'it-1091-db', 'approved', 'it-1091-db')",
                vid,
            )
    finally:
        await tx.rollback()
        await conn.close()
