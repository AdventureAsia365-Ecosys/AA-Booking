"""bk-011 (AA-830) on a real PostgreSQL: the catalog tour key columns of booking.trip_case_legs.

Schema-level only (no new API code): it ships with the migration-only PR so the DB rules are proven before apply. The
legs are opened by the existing bk-002 intake (which leaves the new columns null).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from tests import inquiry_samples as samples
from tests.integration.test_bk002_intake import INTAKE_TABLES, drain, publish

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(f"TRUNCATE {INTAKE_TABLES} CASCADE")
    await conn.execute("TRUNCATE tripplanner.outbox RESTART IDENTITY")
    await conn.execute("TRUNCATE catalog.v_tour, catalog.v_tour_day, catalog.v_tour_photo")
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


# ── Schema ───────────────────────────────────────────────────────────────────


async def test_bk011_is_registered_and_the_columns_are_nullable(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-011'") == 1
    cols = await admin.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema = 'booking'"
        " AND table_name = 'trip_case_legs' AND column_name IN ('source_system', 'source_id')"
    )
    assert {(c["column_name"], c["is_nullable"]) for c in cols} == {("source_system", "YES"), ("source_id", "YES")}


@pytest.mark.parametrize(
    ("system", "source_id"),
    [("cis", None), (None, "src-1"), ("manual", "src-1"), ("cis", "  ")],
    ids=["id-missing", "system-missing", "other-system", "blank-id"],
)
async def test_key_check_refuses_half_or_foreign_keys(
    admin: asyncpg.Connection, pool: asyncpg.Pool, system: str | None, source_id: str | None
) -> None:
    await publish(admin, samples.v1_1())
    await drain(pool)
    leg_id = await admin.fetchval("SELECT id::text FROM booking.trip_case_legs LIMIT 1")
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE booking.trip_case_legs SET source_system = $2, source_id = $3 WHERE id = $1::uuid",
                leg_id,
                system,
                source_id,
            )


async def test_a_full_key_is_accepted_and_indexed(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await publish(admin, samples.v1_1())
    await drain(pool)
    async with pool.acquire() as conn:
        status = await conn.execute(
            "UPDATE booking.trip_case_legs SET source_system = 'cis', source_id = 'src-1' WHERE position = 1"
        )
    assert status == "UPDATE 1"
    assert await admin.fetchval("SELECT to_regclass('booking.trip_case_legs_tour_key_idx') IS NOT NULL")
