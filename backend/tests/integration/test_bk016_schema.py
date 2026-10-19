"""bk-016 (AA-680 Phase 2) on a real PostgreSQL: the Cognito identity columns of booking.staff_users, their checks and
the unique identity index. Schema only; the sign-in that writes them is tested in test_bk016_staff_identity.py.

The session DB has already had every migration applied (conftest, and test_bk001 re-runs them); the convergence test
applies them once more itself, so it does not depend on test order."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKFORCE = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_TESTPOOL"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _shape(conn: asyncpg.Connection) -> dict[str, object]:
    columns = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema = 'booking' AND table_name = 'staff_users' AND column_name LIKE 'cognito_%'"
        )
    }
    checks = {
        r["conname"]: r["def"]
        for r in await conn.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint"
            " WHERE conrelid = 'booking.staff_users'::regclass AND conname LIKE 'staff_users_cognito_%'"
        )
    }
    index = await conn.fetchval(
        "SELECT indexdef FROM pg_indexes"
        " WHERE schemaname = 'booking' AND indexname = 'staff_users_cognito_identity_key'"
    )
    return {"columns": columns, "checks": checks, "index": index}


async def test_bk016_is_registered_with_columns_checks_and_index(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-016'") == 1
    shape = await _shape(admin)
    assert shape["columns"] == {"cognito_iss": ("text", "YES"), "cognito_sub": ("text", "YES")}
    assert set(shape["checks"]) == {"staff_users_cognito_pair_chk", "staff_users_cognito_key_chk"}
    assert "UNIQUE INDEX" in shape["index"] and "(cognito_iss, cognito_sub)" in shape["index"]
    owner = await admin.fetchval(
        "SELECT pg_get_userbyid(relowner) FROM pg_class"
        " WHERE oid = 'booking.staff_users_cognito_identity_key'::regclass"
    )
    assert owner == "bk_owner"
    comment = await admin.fetchval("SELECT col_description('booking.staff_users'::regclass, 1)")
    assert comment.startswith("The Booking staff key (legacy name")


async def test_bk016_converges_when_every_migration_runs_again(admin: asyncpg.Connection) -> None:
    before = await _shape(admin)
    await apply_migrations(admin)
    assert await _shape(admin) == before


async def _insert(conn: asyncpg.Connection, key: str, iss: str | None, sub: str | None) -> None:
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, cognito_iss, cognito_sub)"
        " VALUES ($1, 'sales', true, $2, $3)",
        key,
        iss,
        sub,
    )


@pytest.mark.parametrize(
    ("iss", "sub"),
    [
        (WORKFORCE, None),
        (None, "abc"),
        ("http://cognito.test/pool", "abc"),
        (WORKFORCE, ""),
        (WORKFORCE, "s" * 129),
    ],
    ids=["iss-only", "sub-only", "not-https", "empty-sub", "long-sub"],
)
async def test_pair_check_refuses_half_or_bad_identities(app_conn: asyncpg.Connection, iss, sub) -> None:
    async with app_conn.transaction():
        with pytest.raises(asyncpg.CheckViolationError, match="staff_users_cognito_pair_chk"):
            await _insert(app_conn, f"t-{uuid.uuid4()}", iss, sub)


async def test_cog_key_must_carry_its_own_sub(app_conn: asyncpg.Connection) -> None:
    sub = str(uuid.uuid4())
    async with app_conn.transaction():
        with pytest.raises(asyncpg.CheckViolationError, match="staff_users_cognito_key_chk"):
            await _insert(app_conn, f"cog:{sub}", WORKFORCE, str(uuid.uuid4()))
    async with app_conn.transaction():
        with pytest.raises(asyncpg.CheckViolationError, match="staff_users_cognito_key_chk"):
            await _insert(app_conn, f"cog:{sub}", None, None)


async def test_role_booking_links_and_the_identity_is_unique(app_conn: asyncpg.Connection) -> None:
    """Role booking (the app) can create a cog: row and link a bridge-era row; one identity binds one row only.
    Several unlinked rows (NULL pairs) coexist."""
    sub, legacy = str(uuid.uuid4()), str(uuid.uuid4())
    tx = app_conn.transaction()
    await tx.start()
    try:
        await _insert(app_conn, f"cog:{sub}", WORKFORCE, sub)
        await _insert(app_conn, legacy, None, None)
        await _insert(app_conn, str(uuid.uuid4()), None, None)
        with pytest.raises(asyncpg.UniqueViolationError, match="staff_users_cognito_identity_key"):
            async with app_conn.transaction():
                await app_conn.execute(
                    "UPDATE booking.staff_users SET cognito_iss = $2, cognito_sub = $3 WHERE cis_user_id = $1",
                    legacy,
                    WORKFORCE,
                    sub,
                )
        other = str(uuid.uuid4())
        await app_conn.execute(
            "UPDATE booking.staff_users SET cognito_iss = $2, cognito_sub = $3 WHERE cis_user_id = $1",
            legacy,
            WORKFORCE,
            other,
        )
        assert (
            await app_conn.fetchval(
                "SELECT cis_user_id FROM booking.staff_users WHERE cognito_iss = $1 AND cognito_sub = $2",
                WORKFORCE,
                other,
            )
            == legacy
        )
    finally:
        await tx.rollback()
