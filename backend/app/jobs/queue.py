"""SQL operations on ``booking.job`` (bk-002, ADR 0004 O3).

Same semantics as CIS ``shared/jobs/queue.py`` (ADR 0003): every state change is one UPDATE guarded
by the expected status and, for a running job, by ``locked_by`` — so a worker that lost its lease
can never overwrite what the reaper or another worker already did. Kinds are ``bk_*`` (O9, enforced
by a CHECK in bk-002).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 900


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int


def backoff_seconds(attempt: int) -> int:
    """Delay before retry number ``attempt`` (1-based): 30 s, 60 s, 120 s, ... capped at 15 min."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0), BACKOFF_MAX_SECONDS)


def _payload(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})


async def enqueue(
    conn: asyncpg.Connection | asyncpg.Pool,
    kind: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    delay_seconds: float = 0,
    max_attempts: int = 5,
    created_by: str = "system",
) -> str | None:
    """Queue a job. Returns its id, or None when the idempotency key (or the singleton index for a
    recurring kind) already holds a job. Pass a connection to enqueue inside a caller's transaction."""
    return await conn.fetchval(
        """
        INSERT INTO booking.job (kind, payload, idempotency_key, max_attempts, created_by, run_after)
        VALUES ($1, $2::jsonb, $3, $4, $5, now() + make_interval(secs => $6))
        ON CONFLICT DO NOTHING
        RETURNING id::text
        """,
        kind,
        json.dumps(payload),
        idempotency_key,
        max_attempts,
        created_by,
        float(delay_seconds),
    )


async def claim(pool: asyncpg.Pool, worker_id: str, kinds: list[str], lease_seconds: int) -> Job | None:
    """Claim the next due job of one of ``kinds`` (oldest ``run_after`` first)."""
    row = await pool.fetchrow(
        """
        UPDATE booking.job SET status = 'running', attempt = attempt + 1,
               locked_by = $1, locked_until = now() + make_interval(secs => $3),
               started_at = coalesce(started_at, now())
        WHERE id = (
            SELECT id FROM booking.job
            WHERE status = 'queued' AND run_after <= now() AND kind = ANY($2::text[])
            ORDER BY run_after, created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id::text, kind, payload, attempt, max_attempts
        """,
        worker_id,
        kinds,
        lease_seconds,
    )
    if row is None:
        return None
    return Job(row["id"], row["kind"], _payload(row["payload"]), row["attempt"], row["max_attempts"])


async def heartbeat(pool: asyncpg.Pool, job_id: str, worker_id: str, lease_seconds: int) -> bool:
    """Extend the lease. False when the job is no longer ours (reaped or finished elsewhere)."""
    row = await pool.fetchval(
        """
        UPDATE booking.job SET locked_until = now() + make_interval(secs => $3)
        WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
        RETURNING 1
        """,
        job_id,
        worker_id,
        lease_seconds,
    )
    return row is not None


async def complete(pool: asyncpg.Pool, job_id: str, worker_id: str, result: dict[str, Any] | None) -> bool:
    row = await pool.fetchval(
        """
        UPDATE booking.job SET status = 'succeeded', result = $3::jsonb, error = NULL,
               locked_by = NULL, locked_until = NULL, finished_at = now()
        WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
        RETURNING 1
        """,
        job_id,
        worker_id,
        json.dumps(result) if result is not None else None,
    )
    return row is not None


async def fail(pool: asyncpg.Pool, job: Job, worker_id: str, error: str, *, retryable: bool) -> str:
    """Re-queue with backoff while attempts remain (and the error is retryable), else fail.
    Returns the new status, or '' when the job was no longer ours."""
    if retryable and job.attempt < job.max_attempts:
        row = await pool.fetchval(
            """
            UPDATE booking.job SET status = 'queued', error = $3,
                   run_after = now() + make_interval(secs => $4),
                   locked_by = NULL, locked_until = NULL
            WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
            RETURNING 1
            """,
            job.id,
            worker_id,
            error,
            backoff_seconds(job.attempt),
        )
        return "queued" if row else ""
    row = await pool.fetchval(
        """
        UPDATE booking.job SET status = 'failed', error = $3,
               locked_by = NULL, locked_until = NULL, finished_at = now()
        WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
        RETURNING 1
        """,
        job.id,
        worker_id,
        error,
    )
    return "failed" if row else ""


async def reap(pool: asyncpg.Pool) -> dict[str, int]:
    """Jobs whose lease expired (the worker died): re-queue while attempts remain, else fail."""
    requeued = await pool.fetchval(
        """
        WITH r AS (
            UPDATE booking.job SET status = 'queued', run_after = now(), locked_by = NULL,
                   locked_until = NULL, error = 'lease expired (worker stopped); re-queued'
            WHERE status = 'running' AND locked_until < now() AND attempt < max_attempts
            RETURNING 1
        )
        SELECT count(*) FROM r
        """
    )
    failed = await pool.fetchval(
        """
        WITH f AS (
            UPDATE booking.job SET status = 'failed',
                   error = 'lease expired (worker stopped); no attempts left',
                   locked_by = NULL, locked_until = NULL, finished_at = now()
            WHERE status = 'running' AND locked_until < now()
            RETURNING 1
        )
        SELECT count(*) FROM f
        """
    )
    return {"requeued": int(requeued or 0), "failed": int(failed or 0)}


async def prune(pool: asyncpg.Pool, kind: str, keep_days: int) -> int:
    """Delete finished jobs of a recurring kind older than ``keep_days`` (the pull runs every
    minute; its history is only useful for recent debugging)."""
    deleted = await pool.fetchval(
        """
        WITH d AS (
            DELETE FROM booking.job
            WHERE kind = $1 AND status IN ('succeeded', 'cancelled')
              AND finished_at < now() - make_interval(days => $2)
            RETURNING 1
        )
        SELECT count(*) FROM d
        """,
        kind,
        keep_days,
    )
    return int(deleted or 0)
