"""AA-993 (bk-034) on a real PostgreSQL: the partial index on ``trip_case_version_itinerary.source_tour_id`` exists
with its predicate, is owned with its table, is not unique (an expand file), and a second run of the file is a no-op."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "034_itinerary_source_tour_idx.sql"
INDEX = "booking.trip_case_version_itinerary_source_tour_idx"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_index_shape(admin: asyncpg.Connection) -> None:
    row = await admin.fetchrow(
        "SELECT i.indisunique, i.indisvalid, pg_get_indexdef(i.indexrelid) AS def,"
        " c.relowner::regrole::text AS owner"
        " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indexrelid = $1::regclass",
        INDEX,
    )
    assert row is not None
    assert not row["indisunique"]  # a unique index on an existing table is contract, never expand
    assert row["indisvalid"]
    assert "(source_tour_id)" in row["def"]
    assert "WHERE (source_tour_id IS NOT NULL)" in row["def"]
    assert row["owner"] == "bk_owner"


async def test_registered_and_rerunnable(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-034'") == 1
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'booking' AND tablename = 'trip_case_version_itinerary'"
            " AND indexdef LIKE '%(source_tour_id)%'"
        )
        == 1
    )
