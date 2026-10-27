"""AA-993 G4 proof — ``bk_noop_sleep``, a Dev-only test job kind for the worker-drain live proof (AA-993 design D1).

The Booking port of CIS ``services/jobs/noop_sleep_job.py`` (AA-993 G1). Registered only when ``JOB_TEST_KINDS`` is
truthy on the process (:attr:`app.settings.Settings.job_test_kinds`; AA-CIS-Infra sets it on the Dev task only). It
sleeps ``payload.seconds`` (1..3600) in steps of :data:`STEP_SECONDS` and writes ``progress.slept_seconds`` after each
step, so a job released on shutdown resumes where it stopped (``result.resumed_at_seconds`` shows it). It touches no
other data and calls nothing paid. It is in :data:`app.jobs.kinds.DEPLOY_GATE_EXEMPT` (like the CIS kind's
``deploy_gate_exempt``) and not in ``DEPLOY_BLOCKING_KINDS``, so the drain proof needs no deploy-guard override:

    enqueue bk_noop_sleep {"seconds": 300} (max_attempts 1) -> Deploy Dev -> the old task's lifespan stops the worker,
    the handler is cancelled after WORKER_STOP_TIMEOUT_S and the row is released (queued, attempt back to 0,
    progress.releases = 1) -> the new task re-claims it -> succeeded with resumed_at_seconds > 0

Cancellation:
- **Shutdown** (``asyncio.CancelledError`` from the lifespan's ``wait_for``) is never caught here: it reaches the
  worker, which releases the row (:func:`app.jobs.queue.release`).
- **A cancel request** (``booking.job.cancel_requested``, set in SQL; Booking has no cancel route) is read back by
  every progress write. The handler then ends its own row as ``cancelled`` (guarded by ``status = 'running'``) and
  returns; the worker's ``complete`` then matches no row, so the row stays ``cancelled`` (the worker's log line still
  says "succeeded" with ``cancelled: true`` in the result).

Note: the Booking worker runs one job at a time, so other Booking jobs wait behind this one while it sleeps.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg

from app.jobs.queue import Job
from app.jobs.worker import Handler, PermanentError

KIND = "bk_noop_sleep"
MAX_SECONDS = 3600
STEP_SECONDS = 5.0

_SLEPT_SQL = "SELECT coalesce((progress->>'slept_seconds')::float8, 0) FROM booking.job WHERE id = $1::uuid"

# Merges the step into progress and reads the cancel request back; no row = the job is no longer running.
_PROGRESS_SQL = """
UPDATE booking.job
SET progress = progress || jsonb_build_object('slept_seconds', $2::float8, 'total_seconds', $3::float8)
WHERE id = $1::uuid AND status = 'running'
RETURNING cancel_requested
"""

_CANCEL_SQL = """
UPDATE booking.job SET status = 'cancelled', error = 'cancelled (cancel requested; stopped by the handler)',
       finished_at = now(), locked_by = NULL, locked_until = NULL
WHERE id = $1::uuid AND status = 'running' AND cancel_requested
"""


class JobLost(RuntimeError):
    """The row is no longer ``running`` (reaped or ended elsewhere): stop sleeping."""


def _seconds(payload: dict[str, Any]) -> float:
    seconds = payload.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= MAX_SECONDS:
        raise PermanentError(f"payload.seconds must be a number from 1 to {MAX_SECONDS}")
    return float(seconds)


async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    seconds = _seconds(job.payload)
    slept = float(await pool.fetchval(_SLEPT_SQL, job.id) or 0.0)
    resumed_at = slept
    while slept < seconds:
        step = min(STEP_SECONDS, seconds - slept)
        await asyncio.sleep(step)
        slept += step
        cancel = await pool.fetchval(_PROGRESS_SQL, job.id, round(slept, 3), seconds)
        if cancel is None:
            raise JobLost(f"{KIND} {job.id} is no longer running")
        if cancel:
            await pool.execute(_CANCEL_SQL, job.id)
            return {"slept_seconds": round(slept, 3), "resumed_at_seconds": round(resumed_at, 3), "cancelled": True}
    return {"slept_seconds": round(slept, 3), "resumed_at_seconds": round(resumed_at, 3)}


def build_handlers(enabled: bool) -> dict[str, Handler]:
    """``{KIND: handle}`` when the test kinds are on (``JOB_TEST_KINDS``), else nothing."""
    return {KIND: handle} if enabled else {}
