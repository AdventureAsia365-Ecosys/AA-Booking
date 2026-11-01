"""``booking.job`` backlog sample and its in-app checks (AA-751 B5; S263 alert diet, AA-1025).

:class:`JobMetricsSampler` is one asyncio task of the API process, started by the lifespan next to the worker (only
when the worker is enabled and the database is configured) and independent of it: the sample never goes through
``booking.job`` itself, so a stuck or dead worker loop still shows. Every ``every_s`` seconds (default 300) it reads
two numbers:

- ``oldest_queued_age_s``: ``now() - run_after`` of the oldest **due** queued row (``run_after <= now()``,
  no cancel request: the claim never takes one), any kind, so a kind no worker handles shows too. It is **0 while a
  non-overdue job is running**: the worker is serial, so a due row waiting behind a healthy job is normal; an
  unhealthy one shows as overdue instead. 0 when nothing is due.
- ``overdue_running``: running rows whose current attempt
  (``coalesce(progress->>'attempt_started_at', started_at)``, written by every claim since AA-751 B2) has run longer
  than :func:`app.jobs.kinds.overdue_after_seconds` (2 x ``EXPECTED_SECONDS[kind]``, default 300 s).

S263: these were the CloudWatch metrics ``AA/Jobs/Booking`` with three Infra alarms; nothing is published to
CloudWatch any more. The sample goes to :mod:`app.jobs.ops_checks` (``jobs_stuck`` > 900 s, ``jobs_overdue`` > 0),
which e-mails the platform alerts topic at most once per check per UTC day. The line ``ops_metrics_sampled`` logs the
values and the check results.

Never raises: a failed read or alert is logged and the next tick tries again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import asyncpg

from app.jobs import ops_checks
from app.jobs.kinds import overdue_after_seconds

log = logging.getLogger(__name__)

SAMPLE_EVERY_S = 300.0

PoolProvider = Callable[[], Awaitable[asyncpg.Pool]]

OLDEST_DUE_QUEUED_SQL = """
SELECT extract(epoch FROM now() - min(run_after))::float8
FROM booking.job
WHERE status = 'queued' AND run_after <= now() AND NOT cancel_requested
"""

RUNNING_SQL = """
SELECT kind,
       extract(epoch FROM now() - coalesce((progress->>'attempt_started_at')::timestamptz, started_at))::float8
           AS attempt_age_s
FROM booking.job
WHERE status = 'running'
"""


@dataclass(frozen=True)
class Sample:
    oldest_queued_age_s: float
    overdue_running: int
    running: int
    due_queued_age_s: float  # raw age of the oldest due queued row, before the "healthy job running" rule


def backlog_sample(oldest_due_age_s: float | None, running: Iterable[tuple[str, float | None]]) -> Sample:
    """The two metric values from the raw reads (pure; plan §5 "Booking").

    ``running`` is ``(kind, attempt age in seconds)`` per running row; an unknown age (no start time at all) counts as
    healthy, never as overdue."""
    rows = list(running)
    overdue = sum(1 for kind, age in rows if age is not None and age > overdue_after_seconds(kind))
    healthy_running = len(rows) - overdue > 0
    due_age = max(float(oldest_due_age_s or 0.0), 0.0)
    return Sample(
        oldest_queued_age_s=0.0 if healthy_running else due_age,
        overdue_running=overdue,
        running=len(rows),
        due_queued_age_s=due_age,
    )


async def read_sample(pool: asyncpg.Pool) -> Sample:
    """Two SELECTs on one connection; the app pool's ``command_timeout`` (5 s, ``app.db``) bounds each."""
    async with pool.acquire() as conn:
        oldest = await conn.fetchval(OLDEST_DUE_QUEUED_SQL)
        rows = await conn.fetch(RUNNING_SQL)
    return backlog_sample(oldest, [(r["kind"], r["attempt_age_s"]) for r in rows])


class JobMetricsSampler:
    def __init__(
        self,
        pool: PoolProvider,
        every_s: float = SAMPLE_EVERY_S,
        *,
        publisher: ops_checks.AlertPublisher | None = None,
    ) -> None:
        self._pool = pool
        self._every_s = every_s
        self._publisher = publisher if publisher is not None else ops_checks.AlertPublisher()

    async def sample_once(self) -> Sample | None:
        """Read one sample and run the checks on it. Returns the sample, or None when it could not be read. Never
        raises."""
        try:
            pool = await self._pool()
            sample = await read_sample(pool)
        except Exception as exc:  # noqa: BLE001 — DB down or not migrated: nothing to check this tick
            log.warning("ops_metrics_sample_failed stage=read error=%s", type(exc).__name__)
            return None
        checks = await self._publisher.alert(pool, ops_checks.findings(sample))
        log.info(
            "ops_metrics_sampled oldest_queued_age_s=%.0f overdue_running_jobs=%d running=%d due_queued_age_s=%.0f"
            " checks=%s",
            sample.oldest_queued_age_s,
            sample.overdue_running,
            sample.running,
            sample.due_queued_age_s,
            ",".join(f"{k}:{v}" for k, v in sorted(checks.items())) or "ok",
        )
        return sample

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Sample at once, then every ``every_s`` seconds until ``stop`` is set (or the task is cancelled)."""
        log.info("job checks sampler started (every %.0fs)", self._every_s)
        while not stop.is_set():
            await self.sample_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._every_s)
        log.info("job checks sampler stopped")
