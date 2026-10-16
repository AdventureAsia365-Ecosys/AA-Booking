"""Worker loop behaviour with the queue functions replaced by fakes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import app.jobs.worker as worker_module
from app.jobs.queue import Job
from app.jobs.worker import PermanentError, Recurring, Worker, default_worker_id

pytestmark = pytest.mark.asyncio

POOL = object()


class FakeQueue:
    def __init__(self, jobs: list[Job] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.events: list[tuple[Any, ...]] = []
        self.reaped = {"requeued": 0, "failed": 0, "cancelled": 0}
        self.beat_ok = True
        self.release_status: str | BaseException = "queued"

    async def reap(self, pool: Any) -> dict[str, int]:
        self.events.append(("reap",))
        return self.reaped

    async def enqueue(self, pool: Any, kind: str, payload: dict, **kw: Any) -> str | None:
        self.events.append(("enqueue", kind, kw.get("delay_seconds")))
        return "new"

    async def prune(self, pool: Any, kind: str, keep_days: int) -> int:
        self.events.append(("prune", kind, keep_days))
        return 0

    async def claim(self, pool: Any, worker_id: str, kinds: list[str], lease: int) -> Job | None:
        self.events.append(("claim", tuple(kinds)))
        return self.jobs.pop(0) if self.jobs else None

    async def complete(self, pool: Any, job_id: str, worker_id: str, result: Any) -> bool:
        self.events.append(("complete", job_id, result))
        return True

    async def fail(self, pool: Any, job: Job, worker_id: str, error: str, *, retryable: bool) -> str:
        self.events.append(("fail", job.id, retryable, error))
        return "queued" if retryable else "failed"

    async def release(self, pool: Any, job_id: str, worker_id: str) -> str:
        self.events.append(("release", job_id, worker_id))
        if isinstance(self.release_status, BaseException):
            raise self.release_status
        return self.release_status

    async def heartbeat(self, pool: Any, job_id: str, worker_id: str, lease: int) -> bool:
        self.events.append(("beat", job_id))
        if isinstance(self.beat_ok, BaseException):
            raise self.beat_ok
        return self.beat_ok


@pytest.fixture
def fq(monkeypatch: pytest.MonkeyPatch) -> FakeQueue:
    fake = FakeQueue()
    for name in ("reap", "enqueue", "prune", "claim", "complete", "fail", "release", "heartbeat"):
        monkeypatch.setattr(worker_module.queue, name, getattr(fake, name))
    return fake


async def provide() -> Any:
    return POOL


def job(kind: str = "bk_a") -> Job:
    return Job("j1", kind, {}, attempt=1, max_attempts=3)


async def ok_handler(pool: Any, j: Job) -> dict:
    return {"done": j.id}


async def test_recurring_kind_needs_a_handler() -> None:
    with pytest.raises(ValueError, match="bk_b"):
        Worker(provide, {"bk_a": ok_handler}, recurring=(Recurring("bk_b", 1),))


async def test_default_worker_id_is_unique() -> None:
    assert default_worker_id() != default_worker_id()


async def test_first_tick_runs_maintenance_then_claims(fq: FakeQueue) -> None:
    fq.reaped = {"requeued": 1, "failed": 0}
    w = Worker(provide, {"bk_a": ok_handler}, recurring=(Recurring("bk_a", 60, keep_days=3),), worker_id="w")
    assert await w.run_once() is False
    assert fq.events == [("reap",), ("enqueue", "bk_a", 60), ("prune", "bk_a", 3), ("claim", ("bk_a",))]
    assert await w.run_once() is False
    assert fq.events[-1] == ("claim", ("bk_a",))  # no maintenance on the second tick


async def test_success_completes_with_the_result(fq: FakeQueue) -> None:
    fq.jobs = [job()]
    w = Worker(provide, {"bk_a": ok_handler}, worker_id="w")
    assert await w.run_once() is True
    assert ("complete", "j1", {"done": "j1"}) in fq.events


async def test_permanent_error_fails_without_retry(fq: FakeQueue) -> None:
    async def bad(pool: Any, j: Job) -> None:
        raise PermanentError("bad input")

    fq.jobs = [job()]
    await Worker(provide, {"bk_a": bad}, worker_id="w").run_once()
    assert ("fail", "j1", False, "PermanentError: bad input") in fq.events


async def test_other_errors_are_retried_and_truncated(fq: FakeQueue) -> None:
    async def boom(pool: Any, j: Job) -> None:
        raise RuntimeError("x" * 5000)

    fq.jobs = [job()]
    await Worker(provide, {"bk_a": boom}, worker_id="w").run_once()
    fail = next(e for e in fq.events if e[0] == "fail")
    assert fail[2] is True and len(fail[3]) == worker_module.ERROR_MAX_CHARS


async def test_heartbeat_extends_the_lease_while_a_job_runs(fq: FakeQueue) -> None:
    async def slow(pool: Any, j: Job) -> dict:
        await asyncio.sleep(0.05)
        return {}

    fq.jobs = [job(), job()]
    await Worker(provide, {"bk_a": slow}, worker_id="w", lease_seconds=0.03).run_once()
    assert ("beat", "j1") in fq.events
    fq.beat_ok = False  # lease lost: the beat task stops quietly
    await Worker(provide, {"bk_a": slow}, worker_id="w", lease_seconds=0.03).run_once()


async def test_heartbeat_errors_are_tolerated(fq: FakeQueue) -> None:
    async def slow(pool: Any, j: Job) -> dict:
        await asyncio.sleep(0.05)
        return {}

    fq.beat_ok = OSError("db blip")
    fq.jobs = [job()]
    await Worker(provide, {"bk_a": slow}, worker_id="w", lease_seconds=0.03).run_once()
    assert any(e[0] == "complete" for e in fq.events)


async def test_run_forever_backs_off_on_errors_and_stops(fq: FakeQueue) -> None:
    calls = {"n": 0}
    stop = asyncio.Event()

    async def flaky_pool() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("db down")
        if calls["n"] >= 3:
            stop.set()
        return POOL

    fq.jobs = [job()]
    w = Worker(flaky_pool, {"bk_a": ok_handler}, worker_id="w", poll_seconds=0.01)
    await asyncio.wait_for(w.run_forever(stop), timeout=2)
    assert calls["n"] >= 3
    assert any(e[0] == "complete" for e in fq.events)


# ── Shutdown: release (AA-751, C6.2c) ─────────────────────────────────────────


async def _cancel_mid_job(w: Worker, started: asyncio.Event) -> None:
    """Run one tick, cancel it while the handler runs (what wait_for does on timeout), expect the cancel through."""
    task = asyncio.create_task(w.run_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _blocking_handler(started: asyncio.Event):
    async def handler(pool: Any, j: Job) -> dict:
        started.set()
        await asyncio.sleep(60)
        return {}

    return handler


async def test_shutdown_mid_job_releases_it_and_reraises(fq: FakeQueue) -> None:
    started = asyncio.Event()
    fq.jobs = [job()]
    w = Worker(provide, {"bk_a": _blocking_handler(started)}, worker_id="w")
    await _cancel_mid_job(w, started)
    assert ("release", "j1", "w") in fq.events
    assert not any(e[0] in ("complete", "fail") for e in fq.events)


async def test_shutdown_fails_a_kind_that_is_not_released(fq: FakeQueue) -> None:
    started = asyncio.Event()
    fq.jobs = [job()]
    w = Worker(provide, {"bk_a": _blocking_handler(started)}, worker_id="w", no_release=frozenset({"bk_a"}))
    await _cancel_mid_job(w, started)
    assert ("fail", "j1", False, worker_module.NOT_RELEASED_ERROR) in fq.events
    assert not any(e[0] == "release" for e in fq.events)


async def test_a_failing_release_still_lets_the_cancel_through(fq: FakeQueue) -> None:
    started = asyncio.Event()
    fq.jobs = [job()]
    fq.release_status = OSError("db gone")
    w = Worker(provide, {"bk_a": _blocking_handler(started)}, worker_id="w")
    await _cancel_mid_job(w, started)
    assert ("release", "j1", "w") in fq.events


async def test_wait_for_timeout_waits_for_the_release(fq: FakeQueue) -> None:
    """The lifespan pattern (main.py): stop, then wait_for(task, timeout). The release runs before wait_for returns,
    i.e. before the pool is closed."""
    started = asyncio.Event()
    fq.jobs = [job()]
    w = Worker(provide, {"bk_a": _blocking_handler(started)}, worker_id="w", poll_seconds=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run_forever(stop))
    await asyncio.wait_for(started.wait(), timeout=1)
    stop.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=0.05)
    assert ("release", "j1", "w") in fq.events
    assert task.cancelled()
