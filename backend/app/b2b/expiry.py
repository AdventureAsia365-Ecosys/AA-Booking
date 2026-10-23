"""Hold expiry on ``booking.job`` (AA-973, bk-020): kind ``bk_b2b_hold_expire`` and its schedule hook.

Nothing waits for this job to be correct: every read and every seat count already treats an ``active`` hold past
``expires_at`` as expired (``app.b2b.holds``, ``app.b2b.departures``). The job writes the status down (``expired`` +
``expired_at``) so the table says what is true, staff screens and reports need no ``now()`` arithmetic, and AA-975
can emit ``hold.expired`` from one place (this handler).

- **Handler**: batches of :data:`BATCH` due holds (``FOR UPDATE SKIP LOCKED``: a hold being confirmed or released right
  now is skipped and handled by that request), at most :data:`MAX_BATCHES` per run; result ``{"expired": n}``.
  Idempotent (``WHERE status = 'active'``), so a released or retried run is harmless.
- **Schedule** (:class:`ExpirySchedule`, a worker maintenance hook): on each maintenance pass, one cheap ``EXISTS`` on
  the partial index ``b2b_holds_due_idx``; only when a hold is due it queues one run with the idempotency key
  ``bk_b2b_hold_expire:<UTC minute>``, so no job row is written while nothing is due and two workers never queue twice
  in the same minute. Not exempt from the deploy gate (a run takes seconds; DM-11.4 stays the default).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.jobs import queue
from app.jobs.queue import Job

log = logging.getLogger(__name__)

KIND = "bk_b2b_hold_expire"
BATCH = 500
MAX_BATCHES = 20
MAX_ATTEMPTS = 3
SCHEDULER = "system:b2b_hold_expiry"

DUE_SQL = "SELECT EXISTS (SELECT 1 FROM booking.b2b_holds WHERE status = 'active' AND expires_at <= now())"
EXPIRE_SQL = """
WITH due AS (
    SELECT id FROM booking.b2b_holds
    WHERE status = 'active' AND expires_at <= now()
    ORDER BY expires_at
    LIMIT $1
    FOR UPDATE SKIP LOCKED
)
UPDATE booking.b2b_holds h SET status = 'expired', expired_at = now()
FROM due WHERE h.id = due.id
RETURNING h.id::text
"""


async def expire_due(pool: asyncpg.Pool, *, batch: int = BATCH, max_batches: int = MAX_BATCHES) -> int:
    total = 0
    for _ in range(max_batches):
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(EXPIRE_SQL, batch)
        total += len(rows)
        if len(rows) < batch:
            break
    return total


async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
    expired = await expire_due(pool)
    if expired:
        log.info("b2b_holds_expired count=%s", expired)
    return {"expired": expired}


HANDLERS = {KIND: handle}


def schedule_key(at: datetime) -> str:
    return f"{KIND}:{at.astimezone(UTC):%Y%m%dT%H%M}"


class ExpirySchedule:
    """Worker maintenance hook: queue one expiry run when a hold is due (at most one per UTC minute)."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now

    async def __call__(self, pool: asyncpg.Pool) -> str | None:
        async with pool.acquire() as conn:
            if not await conn.fetchval(DUE_SQL):
                return None
        return await queue.enqueue(
            pool,
            KIND,
            {"trigger": "schedule"},
            idempotency_key=schedule_key(self._now()),
            max_attempts=MAX_ATTEMPTS,
            created_by=SCHEDULER,
        )
