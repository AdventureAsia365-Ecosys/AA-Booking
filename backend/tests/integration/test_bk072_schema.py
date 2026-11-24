"""AA-1153 part 2 (bk-072) on a real PostgreSQL: registration and re-run no-op, and the seed of the two pricing policy
parameters ``allotment_release_days_default`` (7 days) and ``rate_sheet_max_rows`` (2000 rows), each a version 1
made by the migration, submitted and approved by another actor. The running code ignores both keys until the code PR
reads them."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "072_allotment_rate_sheet_policy.sql"
SEEDED = (
    "SELECT p.param_key, p.version_no, p.value::text AS value, p.unit, p.effective_from, p.replaces_id,"
    " e.actor AS approved_by"
    " FROM booking.pricing_policy p"
    " JOIN booking.pricing_policy_events e ON e.policy_id = p.id AND e.event = 'approved'"
    " WHERE p.created_by = 'seed:bk-072' ORDER BY p.param_key"
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
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-072'") == 1


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-072")
    assert "-- migrate:" not in text


async def test_two_parameters_seeded_and_approved_by_another_actor(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(SEEDED)
    got = {r["param_key"]: (json.loads(r["value"]), r["unit"], r["version_no"]) for r in rows}
    assert got == {
        "allotment_release_days_default": (7, "days", 1),
        "rate_sheet_max_rows": (2000, "rows", 1),
    }
    assert all(r["effective_from"] == date(2026, 1, 1) and r["replaces_id"] is None for r in rows)
    assert all(r["approved_by"] != "seed:bk-072" for r in rows)  # maker-checker: another actor approved
