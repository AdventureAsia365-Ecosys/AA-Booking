"""booking.job after bk-013 on a real PostgreSQL, as role ``booking`` (AA-751, X6; data standards C6.2c): release on
shutdown, ``cancel_requested`` in claim / reaper / release / retry, and the per-attempt ``progress.attempt_started_at``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import NOT_RELEASED_ERROR, Worker

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KIND = "bk_probe"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute("TRUNCATE booking.job")
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


async def row(admin: asyncpg.Connection, job_id: str) -> dict[str, Any]:
    r = await admin.fetchrow(
        "SELECT status, attempt, locked_by, locked_until, run_after <= now() AS due, started_at, finished_at, error,"
        " progress::text AS progress FROM booking.job WHERE id = $1::uuid",
        job_id,
    )
    out = dict(r)
    out["progress"] = json.loads(out["progress"])
    return out


async def claim(pool: asyncpg.Pool, worker_id: str = "w1") -> Job:
    job = await queue.claim(pool, worker_id, [KIND], 60)
    assert job is not None
    return job


async def test_shutdown_mid_job_releases_it_to_the_queue(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    """The lifespan pattern (app/main.py): stop, wait_for(worker task, timeout) times out, the task is cancelled and
    releases the job before wait_for returns, i.e. before the pool closes."""
    started = asyncio.Event()

    async def slow(_pool: asyncpg.Pool, _job: Job) -> dict:
        started.set()
        await asyncio.sleep(60)
        return {}

    async def provide() -> asyncpg.Pool:
        return pool

    job_id = await queue.enqueue(pool, KIND, {})
    w = Worker(provide, {KIND: slow}, worker_id="w-shutdown", poll_seconds=0.01, maintenance_every=10_000)
    w._ticks = 1  # skip maintenance
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(stop))
    await asyncio.wait_for(started.wait(), timeout=5)
    stop.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=0.2)

    r = await row(admin, job_id)
    assert (r["status"], r["attempt"], r["locked_by"], r["locked_until"], r["due"]) == ("queued", 0, None, None, True)
    assert r["progress"]["releases"] == 1 and r["progress"]["released"] is True
    assert r["finished_at"] is None
    # Claimable again at once, and the interrupted attempt was not counted.
    again = await claim(pool, "w2")
    assert (again.id, again.attempt) == (job_id, 1)


async def test_a_kind_not_released_is_failed_on_shutdown(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    started = asyncio.Event()

    async def slow(_pool: asyncpg.Pool, _job: Job) -> dict:
        started.set()
        await asyncio.sleep(60)
        return {}

    async def provide() -> asyncpg.Pool:
        return pool

    job_id = await queue.enqueue(pool, KIND, {}, max_attempts=1)
    w = Worker(provide, {KIND: slow}, worker_id="w-nr", poll_seconds=0.01, no_release=frozenset({KIND}))
    w._ticks = 1
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(stop))
    await asyncio.wait_for(started.wait(), timeout=5)
    stop.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=0.2)
    r = await row(admin, job_id)
    assert (r["status"], r["error"], r["locked_by"]) == ("failed", NOT_RELEASED_ERROR, None)
    assert r["finished_at"] is not None


async def test_the_third_release_fails_the_job(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    job_id = await queue.enqueue(pool, KIND, {})
    for n in (1, 2):
        await claim(pool)
        assert await queue.release(pool, job_id, "w1") == "queued"
        assert (await row(admin, job_id))["progress"]["releases"] == n
    await claim(pool)
    assert await queue.release(pool, job_id, "w1") == "failed"
    r = await row(admin, job_id)
    assert r["status"] == "failed" and r["finished_at"] is not None and r["locked_by"] is None
    assert r["error"].startswith("stopped mid-run 3 times")
    assert r["progress"]["releases"] == queue.MAX_RELEASES


async def test_release_only_touches_the_owners_running_job(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    job_id = await queue.enqueue(pool, KIND, {})
    assert await queue.release(pool, job_id, "w1") == ""  # still queued
    await claim(pool, "w1")
    assert await queue.release(pool, job_id, "someone-else") == ""
    assert (await row(admin, job_id))["status"] == "running"


async def test_cancel_requested_is_never_claimed(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    job_id = await queue.enqueue(pool, KIND, {})
    await admin.execute("UPDATE booking.job SET cancel_requested = true WHERE id = $1::uuid", job_id)
    assert await queue.claim(pool, "w1", [KIND], 60) is None
    assert (await row(admin, job_id))["status"] == "queued"


async def test_release_of_a_cancel_request_cancels(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    job_id = await queue.enqueue(pool, KIND, {})
    await claim(pool)
    await admin.execute("UPDATE booking.job SET cancel_requested = true WHERE id = $1::uuid", job_id)
    assert await queue.release(pool, job_id, "w1") == "cancelled"
    r = await row(admin, job_id)
    assert r["status"] == "cancelled" and r["finished_at"] is not None and r["locked_by"] is None


async def test_reaper_cancels_an_expired_cancel_request(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    cancel_id = await queue.enqueue(pool, KIND, {}, idempotency_key="cancel")
    plain_id = await queue.enqueue(pool, KIND, {}, idempotency_key="plain")
    await admin.execute(
        "UPDATE booking.job SET status = 'running', attempt = 1, locked_by = 'dead',"
        " locked_until = now() - interval '1 second', cancel_requested = (id = $1::uuid)",
        cancel_id,
    )
    assert await queue.reap(pool) == {"requeued": 1, "failed": 0, "cancelled": 1}
    c, p = await row(admin, cancel_id), await row(admin, plain_id)
    assert c["status"] == "cancelled" and c["finished_at"] is not None and c["locked_by"] is None
    assert p["status"] == "queued"


async def test_a_retryable_failure_of_a_cancel_request_cancels(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    job_id = await queue.enqueue(pool, KIND, {})
    job = await claim(pool)
    await admin.execute("UPDATE booking.job SET cancel_requested = true WHERE id = $1::uuid", job_id)
    assert await queue.fail(pool, job, "w1", "boom", retryable=True) == "cancelled"
    r = await row(admin, job_id)
    assert r["status"] == "cancelled" and r["finished_at"] is not None


async def test_each_claim_stamps_its_attempt_start(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    job_id = await queue.enqueue(pool, KIND, {})
    job = await claim(pool)
    first = await row(admin, job_id)
    stamp1 = first["progress"]["attempt_started_at"]
    assert await queue.fail(pool, job, "w1", "transient", retryable=True) == "queued"
    await admin.execute("UPDATE booking.job SET run_after = now() WHERE id = $1::uuid", job_id)
    await asyncio.sleep(0.01)
    await claim(pool)
    second = await row(admin, job_id)
    stamp2 = second["progress"]["attempt_started_at"]
    assert datetime.fromisoformat(stamp2) > datetime.fromisoformat(stamp1)
    assert second["started_at"] == first["started_at"]  # started_at keeps the first attempt (F18)
