"""AA-993 G4 proof — the Dev-only test kind ``bk_noop_sleep`` (app.jobs.noop_sleep), over a scripted pool."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.jobs import noop_sleep
from app.jobs.kinds import DEPLOY_BLOCKING_KINDS, DEPLOY_GATE_EXEMPT, EXPECTED_SECONDS, TEST_KINDS
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from tests.unit.fakes import ScriptedConn

JOB_ID = "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture(autouse=True)
def fast_steps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """No real sleeping: record each step instead."""
    steps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        steps.append(seconds)

    monkeypatch.setattr(noop_sleep.asyncio, "sleep", fake_sleep)
    return steps


def _job(payload: dict[str, Any]) -> Job:
    return Job(JOB_ID, noop_sleep.KIND, payload, attempt=1, max_attempts=1)


def _pool(slept: float = 0.0, cancel_after: int | None = None, lost_after: int | None = None) -> ScriptedConn:
    writes = {"n": 0}

    def progress(*_args: Any) -> bool | None:
        writes["n"] += 1
        if lost_after is not None and writes["n"] > lost_after:
            return None
        return cancel_after is not None and writes["n"] >= cancel_after

    conn = ScriptedConn().on("SELECT coalesce((progress->>'slept_seconds')", slept)
    return conn.on("RETURNING cancel_requested", progress)


def test_the_kind_is_test_only_exempt_and_not_blocking() -> None:
    assert noop_sleep.KIND in TEST_KINDS and noop_sleep.KIND in DEPLOY_GATE_EXEMPT
    assert noop_sleep.KIND not in DEPLOY_BLOCKING_KINDS
    assert EXPECTED_SECONDS[noop_sleep.KIND] == noop_sleep.MAX_SECONDS + 60
    assert noop_sleep.build_handlers(False) == {}
    assert noop_sleep.build_handlers(True) == {noop_sleep.KIND: noop_sleep.handle}


@pytest.mark.asyncio
async def test_sleeps_in_steps_and_writes_progress(fast_steps: list[float]) -> None:
    conn = _pool()
    result = await noop_sleep.handle(conn, _job({"seconds": 12}))
    assert result == {"slept_seconds": 12.0, "resumed_at_seconds": 0.0}
    assert fast_steps == [5.0, 5.0, 2.0]
    assert [args for args in conn.sql("RETURNING cancel_requested")] == [
        (JOB_ID, 5.0, 12.0),
        (JOB_ID, 10.0, 12.0),
        (JOB_ID, 12.0, 12.0),
    ]
    (sql, _), *_ = [c for c in conn.calls if "RETURNING cancel_requested" in c[0]]
    assert "status = 'running'" in sql


@pytest.mark.asyncio
async def test_a_released_job_resumes_from_its_progress(fast_steps: list[float]) -> None:
    result = await noop_sleep.handle(_pool(slept=290.0), _job({"seconds": 300}))
    assert result == {"slept_seconds": 300.0, "resumed_at_seconds": 290.0}
    assert fast_steps == [5.0, 5.0]


@pytest.mark.asyncio
async def test_a_cancel_request_ends_the_row_cancelled() -> None:
    conn = _pool(cancel_after=2)
    result = await noop_sleep.handle(conn, _job({"seconds": 300}))
    assert result == {"slept_seconds": 10.0, "resumed_at_seconds": 0.0, "cancelled": True}
    ((sql, args),) = [c for c in conn.calls if "SET status = 'cancelled'" in c[0]]
    assert args == (JOB_ID,)
    assert "status = 'running' AND cancel_requested" in sql


@pytest.mark.asyncio
async def test_a_lost_row_stops_the_handler() -> None:
    with pytest.raises(noop_sleep.JobLost):
        await noop_sleep.handle(_pool(lost_after=1), _job({"seconds": 300}))


@pytest.mark.asyncio
async def test_shutdown_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan's cancel must reach the worker (which releases the row): the handler never swallows it."""

    async def cancelled_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(noop_sleep.asyncio, "sleep", cancelled_sleep)
    conn = _pool()
    with pytest.raises(asyncio.CancelledError):
        await noop_sleep.handle(conn, _job({"seconds": 300}))
    assert not conn.sql("RETURNING cancel_requested")


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [None, 0, 3601, "300", True, -5])
async def test_a_bad_payload_is_permanent(seconds: Any) -> None:
    with pytest.raises(PermanentError):
        await noop_sleep.handle(_pool(), _job({} if seconds is None else {"seconds": seconds}))
