"""AA-996 (bk-029) on a real PostgreSQL: registration and re-run no-op, owner, grants (role ``booking`` inserts and
updates the Booking-owned columns, never deletes, never re-keys; ``cis_app`` reads nothing), the CHECKs, the
``updated_at`` trigger and the DM-6.1 tags of the free-text columns. The HTTP flows are in
``test_bk029_geo_api.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "029_catalog_geo_profiles.sql"
STAFF = "it-996-geo"
PLACE = "d0000000-0000-4000-8000-000000000001"  # stub_catalog.sql: Hanoi


async def _purge(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.execute("DELETE FROM booking.country_profiles")
        await conn.execute("DELETE FROM booking.destination_profiles")
    finally:
        await conn.execute("RESET ROLE")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (STAFF, "admin"))
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_registered_rerunnable_and_owned(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-029'") == 1
    for rel in ("country_profiles", "destination_profiles"):
        owner = await admin.fetchval(
            f"SELECT relowner::regrole::text FROM pg_class WHERE oid = 'booking.{rel}'::regclass"
        )
        assert owner == "bk_owner", rel


async def test_grants(admin: asyncpg.Connection) -> None:
    for rel in ("country_profiles", "destination_profiles"):
        table = f"booking.{rel}"
        assert await admin.fetchval("SELECT has_table_privilege('booking', $1, 'SELECT')", table)
        assert await admin.fetchval("SELECT has_table_privilege('booking', $1, 'INSERT')", table)
        assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, 'DELETE')", table)
        assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, 'TRUNCATE')", table)
        assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", table)
        for col in ("status", "internal_notes", "status_reason", "updated_by"):
            assert await admin.fetchval("SELECT has_column_privilege('booking', $1, $2, 'UPDATE')", table, col)
    assert not await admin.fetchval(
        "SELECT has_column_privilege('booking', 'booking.country_profiles', 'country_code', 'UPDATE')"
    )
    assert not await admin.fetchval(
        "SELECT has_column_privilege('booking', 'booking.destination_profiles', 'destination_id', 'UPDATE')"
    )


async def test_app_writes_soft_state_only(app_conn: asyncpg.Connection) -> None:
    row = await app_conn.fetchrow(
        "INSERT INTO booking.country_profiles (country_code, status, area, updated_by)"
        " VALUES ('VN', 'inactive', 'Indochina', $1)"
        " RETURNING id, status, created_at, updated_at",
        STAFF,
    )
    assert row["status"] == "inactive" and row["id"] is not None
    await app_conn.execute(
        "UPDATE booking.country_profiles SET status = 'active', status_reason = NULL WHERE country_code = 'VN'"
    )
    after = await app_conn.fetchrow("SELECT status, updated_at FROM booking.country_profiles WHERE country_code = 'VN'")
    assert after["status"] == "active" and after["updated_at"] >= row["updated_at"]

    await app_conn.execute(
        "INSERT INTO booking.destination_profiles (destination_id, status, status_reason, updated_by)"
        " VALUES ($1::uuid, 'inactive', 'season closed', $2)",
        PLACE,
        STAFF,
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.destination_profiles WHERE destination_id = $1::uuid", PLACE)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.country_profiles")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE booking.country_profiles SET country_code = 'KH'")


async def test_checks(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute("INSERT INTO booking.country_profiles (country_code, status) VALUES ('KH', 'deleted')")
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute("INSERT INTO booking.country_profiles (country_code, area) VALUES ('KH', '  ')")
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO booking.destination_profiles (destination_id, status_reason) VALUES ($1::uuid, $2)",
            PLACE,
            "x" * 501,
        )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute("INSERT INTO booking.country_profiles (country_code) VALUES ('ZZ')")


async def test_free_text_columns_are_tagged(admin: asyncpg.Connection) -> None:
    for rel in ("country_profiles", "destination_profiles"):
        for col in ("internal_notes", "status_reason"):
            comment = await admin.fetchval(
                "SELECT col_description($1::regclass, (SELECT attnum FROM pg_attribute"
                " WHERE attrelid = $1::regclass AND attname = $2))",
                f"booking.{rel}",
                col,
            )
            assert comment and comment.startswith("conf:commercial; "), (rel, col, comment)
