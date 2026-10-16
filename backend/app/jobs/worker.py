"""The ``booking.job`` worker loop (bk-002, ADR 0004 O3).

One asyncio task, one job at a time. On Dev it runs inside the API process (started from the app
lifespan when ``WORKER_ENABLED`` is on), never from a request path: requests only read and write
rows, the loop does the deferred work. Several processes may run it at once (a rolling deploy
overlaps two tasks): claims use ``FOR UPDATE SKIP LOCKED`` and every handler is idempotent.

Each tick: every ``maintenance_every`` ticks reap expired leases, schedule recurring kinds and prune
their history, and call the maintenance hooks (the daily retention schedule, AA-825); then claim one
due job and run its handler under a heartbeat. A handler returns a result dict (stored on the job) or
raises — :class:`PermanentError` fails the job at once, anything else is retried with backoff until
``max_attempts``.

Shutdown (AA-751, data standards C6.2c): the lifespan sets ``stop`` and gives the loop
``WORKER_STOP_TIMEOUT_S``; a job still running then is cancelled and **released** back to the queue at once
(:func:`app.jobs.queue.release`: due now, the interrupted attempt not counted, ``cancelled`` when a cancel was
requested, ``failed`` after ``MAX_RELEASES``), instead of staying ``running`` until its lease expires. A kind
in ``no_release`` (it keeps no progress checkpoint, so running it again could repeat work an approval capped, e.g.
the retention kinds) is failed at once instead, which is what the reaper did to it before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.jobs import queue
from app.jobs.queue import Job

log = logging.getLogger(__name__)

Handler = Callable[[asyncpg.Pool, Job], Awaitable[dict[str, Any] | None]]
PoolProvider = Callable[[], Awaitable[asyncpg.Pool]]
# Called on every maintenance pass (e.g. the daily retention schedule, AA-825); must be cheap and idempotent.
MaintenanceHook = Callable[[asyncpg.Pool], Awaitable[Any]]

ERROR_MAX_CHARS = 2000
NOT_RELEASED_ERROR = "stopped mid-run on shutdown; this kind is not run again (no progress checkpoint)"


class PermanentError(Exception):
    """The job can never succeed as it is (bad input): fail it without retrying."""


@dataclass(frozen=True)
class Recurring:
    """A kind that always has exactly one job scheduled; the next run is queued ``every_seconds``
    after the previous one finished. The kind needs a singleton unique index in booking.job
    (bk-002 ``job_active_pull_uq``) so two workers cannot both schedule it."""

    kind: str
    every_seconds: float
    keep_days: int = 7


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        pool: PoolProvider,
        handlers: dict[str, Handler],
        *,
        recurring: tuple[Recurring, ...] = (),
        hooks: tuple[MaintenanceHook, ...] = (),
        no_release: frozenset[str] = frozenset(),
        worker_id: str | None = None,
        poll_seconds: float = 2.0,
        lease_seconds: int = 300,
        maintenance_every: int = 15,
    ) -> None:
        unknown = {r.kind for r in recurring} - set(handlers)
        if unknown:
            raise ValueError(f"recurring kinds without a handler: {sorted(unknown)}")
        self._pool = pool
        self._handlers = handlers
        self._recurring = recurring
        self._hooks = hooks
        self._no_release = no_release
        self.worker_id = worker_id or default_worker_id()
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._maintenance_every = maintenance_every
        self._ticks = 0

    async def maintenance(self, pool: asyncpg.Pool) -> None:
        reaped = await queue.reap(pool)
        if any(reaped.values()):
            log.warning("job reaper: %s", reaped)
        for rec in self._recurring:
            if await queue.enqueue(pool, rec.kind, {}, delay_seconds=rec.every_seconds, max_attempts=3):
                log.debug("scheduled %s in %.0fs", rec.kind, rec.every_seconds)
            await queue.prune(pool, rec.kind, rec.keep_days)
        for hook in self._hooks:
            try:
                await hook(pool)
            except Exception as exc:  # noqa: BLE001 — a failing hook must not stop the queue
                log.warning("maintenance hook %r failed (%s: %s)", hook, type(exc).__name__, exc)

    async def run_once(self) -> bool:
        """One tick. Returns True when a job was run (the caller then polls again at once)."""
        pool = await self._pool()
        if self._ticks % self._maintenance_every == 0:
            await self.maintenance(pool)
        self._ticks += 1
        job = await queue.claim(pool, self.worker_id, sorted(self._handlers), self._lease_seconds)
        if job is None:
            return False
        await self._run(pool, job)
        return True

    async def _run(self, pool: asyncpg.Pool, job: Job) -> None:
        beat = asyncio.create_task(self._heartbeat(pool, job.id))
        try:
            result = await self._handlers[job.kind](pool, job)
        except asyncio.CancelledError:
            # Shutdown: the lifespan's wait_for timed out and cancelled this task mid-job. Release the row before the
            # pool closes (wait_for waits for this task to finish), shielded so a second cancel cannot drop the
            # UPDATE half-way; then let the cancellation through.
            if job.kind in self._no_release:
                end = queue.fail(pool, job, self.worker_id, NOT_RELEASED_ERROR, retryable=False)
            else:
                end = queue.release(pool, job.id, self.worker_id)
            try:
                status = await asyncio.shield(end)
            except Exception as exc:  # noqa: BLE001 — the reaper ends it when the lease expires
                log.warning("job %s %s: stop on shutdown failed (%s)", job.kind, job.id, type(exc).__name__)
            else:
                log.warning("job %s %s stopped on shutdown -> %s", job.kind, job.id, status or "not ours")
            raise
        except PermanentError as exc:
            status = await queue.fail(pool, job, self.worker_id, _err(exc), retryable=False)
            log.warning("job %s %s failed permanently: %s", job.kind, job.id, _err(exc))
        except Exception as exc:  # noqa: BLE001 — every other error is retried with backoff
            status = await queue.fail(pool, job, self.worker_id, _err(exc), retryable=True)
            log.warning("job %s %s attempt %d error -> %s: %s", job.kind, job.id, job.attempt, status, _err(exc))
        else:
            await queue.complete(pool, job.id, self.worker_id, result)
            log.info("job %s %s succeeded: %s", job.kind, job.id, result)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def _heartbeat(self, pool: asyncpg.Pool, job_id: str) -> None:
        while True:
            await asyncio.sleep(self._lease_seconds / 3)
            try:
                if not await queue.heartbeat(pool, job_id, self.worker_id, self._lease_seconds):
                    log.warning("job %s: lease lost", job_id)
                    return
            except Exception as exc:  # noqa: BLE001 — a missed beat is not fatal; the lease is long
                log.warning("job %s: heartbeat failed (%s)", job_id, type(exc).__name__)

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("job worker %s started (kinds: %s)", self.worker_id, sorted(self._handlers))
        delay = self._poll_seconds
        while not stop.is_set():
            try:
                busy = await self.run_once()
                delay = self._poll_seconds
            except Exception as exc:  # noqa: BLE001 — DB down, schema not migrated yet: back off, keep going
                busy = False
                delay = min(max(delay * 2, self._poll_seconds), 60.0)
                log.warning("job worker tick failed (%s); retrying in %.0fs", type(exc).__name__, delay)
            if busy:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
        log.info("job worker %s stopped", self.worker_id)


def _err(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:ERROR_MAX_CHARS]
