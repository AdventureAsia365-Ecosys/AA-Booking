"""AA-680 Phase 2 on a real PostgreSQL, as role ``booking``: Cognito WORKFORCE sign-in through the real app and pool —
the first sign-in creates a ``cog:`` row, the role follows the booking-* group, a backfilled bridge-era row is found by
its identity, the identity is never rebound, and an admin cannot change a linked row's role."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import WORKFORCE_ISSUER, LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        await conn.execute("TRUNCATE booking.audit_logs, booking.notification_reads")
        await conn.execute("DELETE FROM booking.staff_users WHERE cis_user_id LIKE 'cog:%' OR cis_user_id LIKE 'b16-%'")
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def http(migrated_booking_dsn: str) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking") as client:
            yield client, cis
    finally:
        await db.close()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_first_sign_in_creates_a_cog_row_then_the_role_follows_the_group(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    sub = str(uuid.uuid4())
    first = await client.get("/booking/staff/me", headers=bearer(cis.workforce.token(["booking-sales"], sub=sub)))
    again = await client.get("/booking/staff/me", headers=bearer(cis.workforce.token(["booking-sales"], sub=sub)))
    promoted = await client.get(
        "/booking/staff/me", headers=bearer(cis.workforce.token(["booking-admin", "cis-reviewer"], sub=sub))
    )
    assert first.status_code == 200 and again.status_code == 200 and promoted.status_code == 200
    assert first.json()["cis_user_id"] == f"cog:{sub}" and first.json()["role"] == "sales"
    assert promoted.json()["role"] == "admin" and promoted.json()["cis_role"] == "reviewer"
    row = await admin.fetchrow("SELECT * FROM booking.staff_users WHERE cis_user_id = $1", f"cog:{sub}")
    assert (row["cognito_iss"], row["cognito_sub"], row["email"], row["name"], row["active"]) == (
        WORKFORCE_ISSUER,
        sub,
        "thu@adventure.asia",
        "thu",
        True,
    )
    actions = [
        r["action"] for r in await admin.fetch("SELECT action FROM booking.audit_logs ORDER BY created_at, action")
    ]
    assert actions == ["staff.provisioned", "staff.role_synced"]


async def test_no_booking_group_is_refused_and_writes_nothing(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    r = await client.get("/booking/staff/me", headers=bearer(cis.workforce.token(["cis-admin"], sub=str(uuid.uuid4()))))
    assert r.status_code == 403 and r.json()["detail"] == "no_booking_role"
    assert await admin.fetchval("SELECT count(*) FROM booking.staff_users WHERE cis_user_id LIKE 'cog:%'") == 0


async def test_backfilled_bridge_row_is_found_by_identity_and_the_bridge_still_works(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    """The row the Infra backfill linked keeps its CIS key (and so its history): no cog: row is created."""
    client, cis = http
    sub, legacy = str(uuid.uuid4()), "b16-" + str(uuid.uuid4())[:8]
    await admin.execute(
        "INSERT INTO booking.staff_users (cis_user_id, name, role, active, cis_role, cognito_iss, cognito_sub)"
        " VALUES ($1, 'Legacy Sales', 'contracting', true, 'reviewer', $2, $3)",
        legacy,
        WORKFORCE_ISSUER,
        sub,
    )
    cognito = await client.get("/booking/staff/me", headers=bearer(cis.workforce.token(["booking-sales"], sub=sub)))
    bridge = await client.get("/booking/staff/me", headers=bearer(cis.token(sub=legacy, cis_role="reviewer")))
    assert cognito.status_code == 200 and cognito.json()["cis_user_id"] == legacy
    assert cognito.json()["role"] == "sales" and cognito.json()["name"] == "Legacy Sales"
    assert bridge.status_code == 200 and bridge.json()["role"] == "sales"  # the bridge never changes the role
    assert await admin.fetchval("SELECT count(*) FROM booking.staff_users WHERE cis_user_id = $1", f"cog:{sub}") == 0


async def test_inactive_linked_row_is_refused(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    sub = str(uuid.uuid4())
    await admin.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, cognito_iss, cognito_sub)"
        " VALUES ($1, 'sales', false, $2, $3)",
        "b16-off",
        WORKFORCE_ISSUER,
        sub,
    )
    r = await client.get("/booking/staff/me", headers=bearer(cis.workforce.token(["booking-sales"], sub=sub)))
    assert r.status_code == 403 and r.json()["detail"] == "staff_inactive"


async def test_identity_is_never_rebound(http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection) -> None:
    """cog:<sub> exists but is bound to another pool's issuer: the WORKFORCE identity cannot take it over."""
    client, cis = http
    sub = str(uuid.uuid4())
    await admin.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, cognito_iss, cognito_sub)"
        " VALUES ($1, 'admin', true, 'https://cognito-idp.us-west-1.amazonaws.com/us-west-1_OTHER', $2)",
        f"cog:{sub}",
        sub,
    )
    r = await client.get("/booking/staff/me", headers=bearer(cis.workforce.token(["booking-admin"], sub=sub)))
    assert r.status_code == 403 and r.json()["detail"] == "identity_conflict"
    iss = await admin.fetchval("SELECT cognito_iss FROM booking.staff_users WHERE cis_user_id = $1", f"cog:{sub}")
    assert iss.endswith("us-west-1_OTHER")


async def test_concurrent_first_sign_ins_create_one_row(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    sub = str(uuid.uuid4())
    token = cis.workforce.token(["booking-contracting"], sub=sub)
    results = await asyncio.gather(*(client.get("/booking/staff/me", headers=bearer(token)) for _ in range(4)))
    assert {r.status_code for r in results} == {200}
    assert await admin.fetchval("SELECT count(*) FROM booking.staff_users WHERE cognito_sub = $1", sub) == 1
    assert await admin.fetchval("SELECT count(*) FROM booking.audit_logs WHERE action = 'staff.provisioned'") == 1


async def test_admin_cannot_change_a_linked_role_but_can_suspend(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    client, cis = http
    boss_sub, sales_sub = str(uuid.uuid4()), str(uuid.uuid4())
    boss = bearer(cis.workforce.token(["booking-admin"], sub=boss_sub))
    sales = bearer(cis.workforce.token(["booking-sales"], sub=sales_sub, email="sales@adventure.asia"))
    assert (await client.get("/booking/staff/me", headers=boss)).status_code == 200
    assert (await client.get("/booking/staff/me", headers=sales)).status_code == 200
    role = await client.patch(f"/booking/staff/cog:{sales_sub}", json={"role": "contracting"}, headers=boss)
    suspend = await client.patch(f"/booking/staff/cog:{sales_sub}", json={"active": False}, headers=boss)
    refused = await client.get("/booking/staff/me", headers=sales)
    listed = await client.get("/booking/staff", headers=boss)
    assert role.status_code == 409 and role.json()["detail"] == "managed_in_directory"
    assert suspend.status_code == 200 and suspend.json()["active"] is False
    assert refused.status_code == 403 and refused.json()["detail"] == "staff_inactive"
    managed = {s["cis_user_id"]: s["managed_in_directory"] for s in listed.json()["items"]}
    assert managed[f"cog:{boss_sub}"] is True and managed[f"cog:{sales_sub}"] is True
