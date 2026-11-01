"""Backlog sample SQL and the S263 alert ledger on a real PostgreSQL, as role ``booking`` (AA-751 B5): due vs not-due vs
cancel-requested queued rows, the per-attempt age of running rows, and the "healthy running job zeroes the queue age"
rule end to end through :func:`app.jobs.metrics.read_sample`."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from app.jobs import ops_checks, queue
from app.jobs.metrics import JobMetricsSampler, read_sample

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


async def queued(admin: asyncpg.Connection, kind: str, age: str, *, cancel: bool = False) -> None:
    await admin.execute(
        "INSERT INTO booking.job (kind, payload, created_by, run_after, cancel_requested)"
        " VALUES ($1, '{}', 'aa-751-test', now() - $2::text::interval, $3)",
        kind,
        age,
        cancel,
    )


async def running(admin: asyncpg.Connection, kind: str, started: str, attempt_started: str | None) -> None:
    progress = "'{}'::jsonb"
    if attempt_started is not None:
        progress = f"jsonb_build_object('attempt_started_at', now() - interval '{attempt_started}')"
    await admin.execute(
        "INSERT INTO booking.job (kind, payload, created_by, status, attempt, started_at, locked_by, locked_until,"
        f" progress) VALUES ($1, '{{}}', 'aa-751-test', 'running', 1, now() - $2::text::interval, 'w-test',"
        f" now() + interval '5 minutes', {progress})",
        kind,
        started,
    )


async def test_empty_queue_is_zero(pool: asyncpg.Pool) -> None:
    s = await read_sample(pool)
    assert (s.oldest_queued_age_s, s.overdue_running, s.running) == (0.0, 0, 0)


async def test_only_due_rows_without_a_cancel_request_age(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await queue.enqueue(pool, "bk_inquiry_pull", {}, delay_seconds=60)  # the recurring pull: not due yet
    await queued(admin, "bk_x6_probe", "2 hours", cancel=True)  # never claimable, not backlog
    await queued(admin, "bk_x6_probe", "20 minutes")  # unhandled kind: counts
    await queued(admin, "bk_inquiry_process", "1 minute")
    s = await read_sample(pool)
    assert 1195 <= s.oldest_queued_age_s <= 1210
    assert (s.overdue_running, s.running) == (0, 0)


async def test_healthy_running_job_zeroes_age_and_overdue_uses_the_attempt_start(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    await queued(admin, "bk_inquiry_process", "20 minutes")
    # First attempt started an hour ago, this attempt 10 s ago: healthy (the per-attempt time wins, F18).
    await running(admin, "bk_retention_job", "1 hour", "10 seconds")
    s = await read_sample(pool)
    assert (s.oldest_queued_age_s, s.overdue_running, s.running) == (0.0, 0, 1)
    assert s.due_queued_age_s >= 1195

    await admin.execute("TRUNCATE booking.job")
    await queued(admin, "bk_inquiry_process", "20 minutes")
    await running(admin, "bk_notify_email", "1 hour", "3 minutes")  # > 2 x 60 s
    await running(admin, "bk_inquiry_pull", "30 minutes", None)  # no attempt stamp: started_at, > 2 x 60 s
    s = await read_sample(pool)
    assert s.overdue_running == 2
    assert s.oldest_queued_age_s >= 1195  # nothing healthy runs, so the queue ages


async def test_claim_stamp_feeds_the_sample(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await queued(admin, "bk_inquiry_process", "20 minutes")
    await queued(admin, "bk_inquiry_process", "19 minutes")
    job = await queue.claim(pool, "w-1", ["bk_inquiry_process"], 60)
    assert job is not None
    s = await read_sample(pool)
    assert (s.oldest_queued_age_s, s.overdue_running, s.running) == (0.0, 0, 1)


async def test_sampler_alerts_once_a_day_with_a_ledger_row(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    """S263: a stuck queue mails the alerts topic once; the ledger row (succeeded, never claimed) holds the day."""
    await queued(admin, "bk_x6_probe", "20 minutes")
    calls: list[dict] = []

    class Sns:
        def publish(self, **kw: object) -> dict:
            calls.append(kw)
            return {}

    async def provide() -> asyncpg.Pool:
        return pool

    pub = ops_checks.AlertPublisher("topic-placeholder", environment="test", client_factory=Sns)
    sampler = JobMetricsSampler(provide, publisher=pub)
    sample = await sampler.sample_once()
    assert sample is not None and sample.oldest_queued_age_s >= 1195
    await sampler.sample_once()
    assert len(calls) == 1 and calls[0]["Subject"] == "[AA-Booking test] Ops check: Job queue stuck"
    row = await admin.fetchrow(
        "SELECT status, idempotency_key, finished_at IS NOT NULL AS finished, result FROM booking.job WHERE kind = $1",
        ops_checks.OPS_ALERT_KIND,
    )
    assert row["status"] == "succeeded" and row["finished"]
    assert row["idempotency_key"] == ops_checks.alert_key("jobs_stuck")
    # never claimable, never counted as backlog
    assert await queue.claim(pool, "w-1", [ops_checks.OPS_ALERT_KIND], 60) is None
    assert (await read_sample(pool)).oldest_queued_age_s >= 1195


async def test_forget_alert_reopens_the_day(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    key = ops_checks.alert_key("jobs_overdue")
    first = await queue.record_alert(pool, ops_checks.OPS_ALERT_KIND, key, {"check": "jobs_overdue"})
    assert first is not None
    assert await queue.record_alert(pool, ops_checks.OPS_ALERT_KIND, key, {}) is None
    await queue.forget_alert(pool, first)
    assert await queue.record_alert(pool, ops_checks.OPS_ALERT_KIND, key, {}) is not None
