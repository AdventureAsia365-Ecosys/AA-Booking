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
        self.reaped = {"requeued": 0, "failed": 0}
        self.beat_ok = True

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

    async def heartbeat(self, pool: Any, job_id: str, worker_id: str, lease: int) -> bool:
        self.events.append(("beat", job_id))
        if isinstance(self.beat_ok, BaseException):
            raise self.beat_ok
        return self.beat_ok


@pytest.fixture
def fq(monkeypatch: pytest.MonkeyPatch) -> FakeQueue:
    fake = FakeQueue()
    for name in ("reap", "enqueue", "prune", "claim", "complete", "fail", "heartbeat"):
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
