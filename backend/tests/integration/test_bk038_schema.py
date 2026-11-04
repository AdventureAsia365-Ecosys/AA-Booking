"""bk-038 (AA-1035 R2, root ADR 0019 d3 / d9): booking.staff_users.role accepts 'finance'.

A WORKFORCE sign-in with the group booking-finance writes role 'finance' to its staff row (app/auth/staff.py
sync_cognito_staff); before bk-038 the bk-003 CHECK refused it. The replay applies every file; the test re-runs
bk-038 as the migration role (re-runnable) and checks the rule both ways.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "038_staff_users_finance_role.sql"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _insert(conn: asyncpg.Connection, role: str) -> str:
    key = f"aa1035-{uuid.uuid4().hex[:12]}"
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, email, name, role, active) VALUES ($1, $2, $3, $4, true)",
        key,
        f"{key}@example.test",
        "Juniper Ledger",
        role,
    )
    return key


async def test_bk038_is_registered_and_re_runnable(admin: asyncpg.Connection) -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(body)
    finally:
        await admin.execute("RESET ROLE")
    count = await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-038'")
    assert count == 1


@pytest.mark.parametrize("role", ["admin", "sales", "contracting", "content_creator", "finance"])
async def test_every_booking_role_is_accepted(admin: asyncpg.Connection, role: str) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        await _insert(admin, role)
    finally:
        await tx.rollback()


async def test_an_unknown_role_is_still_refused(admin: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(admin, "accountant")
