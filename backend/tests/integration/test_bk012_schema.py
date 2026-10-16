"""bk-012 (AA-825) on a real PostgreSQL: inbox_events.redacted_at + payload pii tag, the redaction and finished-job
indexes. Schema only; the retention kinds that use them are tested in test_bk012_retention.py."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_bk012_is_registered_with_column_indexes_and_pii_tag(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-012'") == 1
    assert (
        await admin.fetchval(
            "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'booking'"
            " AND table_name = 'inbox_events' AND column_name = 'redacted_at'"
        )
        == "YES"
    )
    indexes = await admin.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'booking'"
        " AND indexname IN ('job_finished_idx', 'inbox_events_redact_due_idx')"
    )
    defs = {r["indexname"]: r["indexdef"] for r in indexes}
    assert "(finished_at, id) WHERE (finished_at IS NOT NULL)" in defs["job_finished_idx"]
    assert "(received_at, id) WHERE (redacted_at IS NULL)" in defs["inbox_events_redact_due_idx"]
    comment = await admin.fetchval(
        "SELECT col_description(attrelid, attnum) FROM pg_attribute"
        " WHERE attrelid = 'booking.inbox_events'::regclass AND attname = 'payload'"
    )
    assert comment.startswith("pii:contact")
