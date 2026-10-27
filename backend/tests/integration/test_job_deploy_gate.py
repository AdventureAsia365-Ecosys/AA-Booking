"""Deploy gate count on a real PostgreSQL, as role ``booking`` (AA-751 B3, plan §3): only ``queued`` / ``running`` rows
count, with no time filter (a future ``run_after`` still counts), and the exempt ``bk_inquiry_pull`` singleton never
does."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from app.jobs import queue
from app.jobs.kinds import DEPLOY_BLOCKING_KINDS, DEPLOY_GATE_EXEMPT

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute("TRUNCATE booking.job")
    try:
        yield conn
    finally:
        await conn.execute("TRUNCATE booking.job")
        await conn.close()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


async def job(admin: asyncpg.Connection, kind: str, status: str = "queued", *, run_after: str = "0 seconds") -> None:
    if status == "running":
        await admin.execute(
            "INSERT INTO booking.job (kind, payload, created_by, status, attempt, started_at, locked_by, locked_until)"
            " VALUES ($1, '{}', 'aa-751-test', 'running', 1, now(), 'w-test', now() + interval '5 minutes')",
            kind,
        )
    elif status == "queued":
        await admin.execute(
            "INSERT INTO booking.job (kind, payload, created_by, run_after)"
            " VALUES ($1, '{}', 'aa-751-test', now() + $2::text::interval)",
            kind,
            run_after,
        )
    else:
        await admin.execute(
            "INSERT INTO booking.job (kind, payload, created_by, status, finished_at)"
            " VALUES ($1, '{}', 'aa-751-test', $2, now())",
            kind,
            status,
        )


async def test_idle_queue_with_only_the_pull_is_safe(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await job(admin, "bk_inquiry_pull", run_after="60 seconds")
    await job(admin, "bk_notify_email", "succeeded")
    await job(admin, "bk_notify_email", "failed")
    await job(admin, "bk_notify_email", "cancelled")
    assert await queue.active_count(pool, DEPLOY_GATE_EXEMPT) == {"active": 0, "by_status": {}, "by_kind": {}}


async def test_queued_and_running_rows_count_by_status_and_kind(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await job(admin, "bk_inquiry_pull", "running")
    await job(admin, "bk_x6_probe")
    await job(admin, "bk_notify_email", run_after="1 hour")  # not due for an hour: still counted (no time filter)
    await job(admin, "bk_notify_email", "running")
    await job(admin, "bk_notify_email", "succeeded")
    counts = await queue.active_count(pool, DEPLOY_GATE_EXEMPT)
    assert counts == {
        "active": 3,
        "by_status": {"queued": 2, "running": 1},
        "by_kind": {"bk_notify_email": 2, "bk_x6_probe": 1},
    }


async def test_without_exemptions_the_pull_counts(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await job(admin, "bk_inquiry_pull")
    counts = await queue.active_count(pool)
    assert counts == {"active": 1, "by_status": {"queued": 1}, "by_kind": {"bk_inquiry_pull": 1}}


async def test_only_running_rows_of_the_blocking_kinds_count(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    """AA-993 G5: the gate's blocker query: running retention rows count, a queued one and ordinary kinds do not."""
    await job(admin, "bk_retention_job")
    await job(admin, "bk_notify_email", "running")
    await job(admin, "bk_retention_inbox_redact", "succeeded")
    assert await queue.running_count(pool, DEPLOY_BLOCKING_KINDS) == {}
    await job(admin, "bk_retention_job", "running")
    await job(admin, "bk_retention_inbox_redact", "running")
    assert await queue.running_count(pool, DEPLOY_BLOCKING_KINDS) == {
        "bk_retention_inbox_redact": 1,
        "bk_retention_job": 1,
    }
