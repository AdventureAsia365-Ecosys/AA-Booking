"""SQL operations on ``booking.job`` (bk-002, ADR 0004 O3).

Same semantics as CIS ``shared/jobs/queue.py`` (ADR 0003): every state change is one UPDATE guarded
by the expected status and, for a running job, by ``locked_by`` — so a worker that lost its lease
can never overwrite what the reaper or another worker already did. Kinds are ``bk_*`` (O9, enforced
by a CHECK in bk-002).

Since bk-013 (AA-751, X6) the table has the template's ``cancel_requested`` / ``progress`` columns: a job with
``cancel_requested`` is never claimed, and the reaper, :func:`release` and a retryable :func:`fail` end it as
``cancelled`` instead of putting it back in the queue. Each claim records ``progress.attempt_started_at`` (the
first attempt's ``started_at`` is kept), and a graceful shutdown releases a running job at once
(:func:`release`, the port of CIS ``release``) instead of leaving it to the lease expiry.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import asyncpg

BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 900
# A job whose worker keeps stopping mid-run (e.g. the task is replaced each time the job runs) is failed after this
# many releases instead of looping forever: a release does not count as an attempt (same cap as CIS MAX_RELEASES).
MAX_RELEASES = 3


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
    """Claim the next due job of one of ``kinds`` (oldest ``run_after`` first), never one with a cancel request.
    ``started_at`` keeps the first attempt's time; ``progress.attempt_started_at`` is this attempt's (F18)."""
    row = await pool.fetchrow(
        """
        UPDATE booking.job SET status = 'running', attempt = attempt + 1,
               locked_by = $1, locked_until = now() + make_interval(secs => $3),
               started_at = coalesce(started_at, now()),
               progress = progress || jsonb_build_object('attempt_started_at', now())
        WHERE id = (
            SELECT id FROM booking.job
            WHERE status = 'queued' AND run_after <= now() AND NOT cancel_requested
              AND kind = ANY($2::text[])
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
    """Re-queue with backoff while attempts remain (and the error is retryable), else fail. A retryable failure of a
    job with a cancel request ends it as ``cancelled`` (the claim would never pick it up again).
    Returns the new status, or '' when the job was no longer ours."""
    if retryable and job.attempt < job.max_attempts:
        status = await pool.fetchval(
            """
            UPDATE booking.job SET status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,
                   error = $3,
                   run_after = now() + make_interval(secs => $4),
                   finished_at = CASE WHEN cancel_requested THEN now() ELSE finished_at END,
                   locked_by = NULL, locked_until = NULL
            WHERE id = $1::uuid AND status = 'running' AND locked_by = $2
            RETURNING status
            """,
            job.id,
            worker_id,
            error,
            backoff_seconds(job.attempt),
        )
        return status or ""
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


async def release(pool: asyncpg.Pool, job_id: str, worker_id: str) -> str:
    """Graceful shutdown (port of CIS ``shared/jobs/queue.py`` ``release``): back to the queue at once, due now, and
    the interrupted attempt is not counted. Returns the new status: 'queued', 'cancelled' (a cancel was requested),
    'failed' (released ``MAX_RELEASES`` times), or '' when the job was no longer ours."""
    status = await pool.fetchval(
        """
        WITH cur AS (
            SELECT coalesce((progress->>'releases')::int, 0) + 1 AS releases, cancel_requested
            FROM booking.job WHERE id = $1::uuid
        )
        UPDATE booking.job j SET
            status = CASE WHEN cur.cancel_requested THEN 'cancelled'
                          WHEN cur.releases >= $3 THEN 'failed' ELSE 'queued' END,
            attempt = greatest(j.attempt - 1, 0),
            error = CASE WHEN cur.cancel_requested THEN 'cancelled (cancel requested; released on shutdown)'
                         WHEN cur.releases >= $3 THEN 'stopped mid-run ' || cur.releases
                              || ' times (the worker process keeps stopping while this job runs)'
                         ELSE j.error END,
            finished_at = CASE WHEN cur.cancel_requested OR cur.releases >= $3 THEN now() ELSE j.finished_at END,
            run_after = now(), locked_by = NULL, locked_until = NULL,
            progress = j.progress || jsonb_build_object('released', true, 'releases', cur.releases)
        FROM cur
        WHERE j.id = $1::uuid AND j.status = 'running' AND j.locked_by = $2
        RETURNING j.status
        """,
        job_id,
        worker_id,
        MAX_RELEASES,
    )
    return status or ""


async def reap(pool: asyncpg.Pool) -> dict[str, int]:
    """Jobs whose lease expired (the worker died): re-queue while attempts remain, else fail; a job with a cancel
    request is cancelled instead of re-queued (as in CIS)."""
    requeued = await pool.fetchval(
        """
        WITH r AS (
            UPDATE booking.job SET status = 'queued', run_after = now(), locked_by = NULL,
                   locked_until = NULL, error = 'lease expired (worker stopped); re-queued'
            WHERE status = 'running' AND locked_until < now() AND attempt < max_attempts
              AND NOT cancel_requested
            RETURNING 1
        )
        SELECT count(*) FROM r
        """
    )
    ended = await pool.fetchrow(
        """
        WITH f AS (
            UPDATE booking.job SET status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
                   error = CASE WHEN cancel_requested THEN 'cancelled (cancel requested; lease expired)'
                                ELSE 'lease expired (worker stopped); no attempts left' END,
                   locked_by = NULL, locked_until = NULL, finished_at = now()
            WHERE status = 'running' AND locked_until < now()
            RETURNING status
        )
        SELECT count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'cancelled') AS cancelled
        FROM f
        """
    )
    return {
        "requeued": int(requeued or 0),
        "failed": int(ended["failed"] or 0) if ended else 0,
        "cancelled": int(ended["cancelled"] or 0) if ended else 0,
    }


async def active_count(pool: asyncpg.Pool, exempt_kinds: Iterable[str] = ()) -> dict[str, Any]:
    """Queued + running jobs right now, with no time filter, for the deploy gate (AA-751 B3; same contract as CIS
    ``shared/jobs/queue.py`` ``active_count`` plus ``by_kind``). A deploy stops the task, so any such job would be
    interrupted. Rows of ``exempt_kinds`` are not counted. One GROUP BY query."""
    rows = await pool.fetch(
        """
        SELECT status, kind, count(*)::int AS n
        FROM booking.job
        WHERE status IN ('queued', 'running') AND NOT (kind = ANY($1::text[]))
        GROUP BY status, kind
        """,
        sorted(exempt_kinds),
    )
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + r["n"]
    return {"active": sum(by_status.values()), "by_status": by_status, "by_kind": dict(sorted(by_kind.items()))}


async def running_count(pool: asyncpg.Pool, kinds: Iterable[str]) -> dict[str, int]:
    """``running`` rows of ``kinds``, per kind (AA-993 G5: the deploy-blocking kinds the deploy gate checks). A queued
    row is not counted: no work has started, and whichever task is live claims it. One GROUP BY query."""
    rows = await pool.fetch(
        """
        SELECT kind, count(*)::int AS n
        FROM booking.job
        WHERE status = 'running' AND kind = ANY($1::text[])
        GROUP BY kind
        """,
        sorted(kinds),
    )
    return {r["kind"]: r["n"] for r in sorted(rows, key=lambda r: r["kind"]) if r["n"]}


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
