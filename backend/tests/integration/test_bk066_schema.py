"""AA-1175 (bk-066) on a real PostgreSQL: registration and re-run no-op, and the seed of the pricing policy parameter
``price_pax_bands`` (the group-size bands of the computed price matrix): v1 = the bands the code used until bk-066,
v2 = the business owner's bands replacing it, both made by the migration, submitted and approved by another actor; v2
is in force from 2026-11-23 and v1 before it. The running code ignores the key until the code PR reads it."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "066_price_pax_bands_policy.sql"
OLD = [[1, 1], [2, 2], [3, 4], [5, 6], [7, 10], [11, None]]
SEED = [[1, 1], [2, 2], [3, 3], [4, 4], [5, 7], [8, 10], [11, None]]
SEEDED = (
    "SELECT p.id::text AS id, p.version_no, p.value::text AS value, p.unit, p.effective_from,"
    " p.replaces_id::text AS replaces_id, p.diff::text AS diff, e.actor AS approved_by"
    " FROM booking.pricing_policy p"
    " JOIN booking.pricing_policy_events e ON e.policy_id = p.id AND e.event = 'approved'"
    " WHERE p.param_key = 'price_pax_bands' AND p.created_by = 'seed:bk-066' ORDER BY p.version_no"
)
# The value in force on a day, as app.pricing.policy_values.IN_FORCE_SQL picks it.
IN_FORCE = (
    "SELECT p.value::text FROM booking.pricing_policy p"
    " JOIN booking.pricing_policy_events a ON a.policy_id = p.id AND a.event = 'approved'"
    " WHERE p.param_key = 'price_pax_bands' AND p.effective_from <= $1"
    " ORDER BY p.effective_from DESC, a.created_at DESC, p.version_no DESC LIMIT 1"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_registered_once_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    before = [tuple(r) for r in await admin.fetch(SEEDED)]
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert [tuple(r) for r in await admin.fetch(SEEDED)] == before
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-066'") == 1


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-066")
    assert "-- migrate:" not in text


async def test_two_seeded_versions_v2_replaces_v1(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(SEEDED)
    assert [r["version_no"] for r in rows] == [1, 2]
    v1, v2 = rows
    assert json.loads(v1["value"]) == OLD and v1["replaces_id"] is None and v1["diff"] is None
    assert json.loads(v2["value"]) == SEED and v2["replaces_id"] == v1["id"]
    assert json.loads(v2["diff"]) == {"from": OLD, "to": SEED}
    assert (v1["effective_from"], v2["effective_from"]) == (date(2026, 1, 1), date(2026, 11, 23))
    assert {r["unit"] for r in rows} == {"travellers"}
    assert all(r["approved_by"] != "seed:bk-066" for r in rows)  # maker-checker: another actor approved
    assert json.loads(await admin.fetchval(IN_FORCE, date(2026, 11, 22))) == OLD
    assert json.loads(await admin.fetchval(IN_FORCE, date(2026, 11, 23))) == SEED
