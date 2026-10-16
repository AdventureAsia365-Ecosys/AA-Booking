"""booking.job SQL wrappers: arguments and return mapping (the SQL itself runs in tests/integration)."""

from __future__ import annotations

import json

import pytest

from app.jobs import queue
from app.jobs.queue import Job, backoff_seconds
from tests.unit.fakes import ScriptedConn

pytestmark = pytest.mark.asyncio

JOB = Job("j1", "bk_probe", {}, attempt=1, max_attempts=3)


async def test_backoff_doubles_and_caps() -> None:
    assert [backoff_seconds(n) for n in (0, 1, 2, 3)] == [30, 30, 60, 120]
    assert backoff_seconds(50) == queue.BACKOFF_MAX_SECONDS


async def test_enqueue_passes_payload_and_key() -> None:
    conn = ScriptedConn([("INSERT INTO booking.job", "id-1")])
    assert await queue.enqueue(conn, "bk_probe", {"a": 1}, idempotency_key="k", delay_seconds=5) == "id-1"
    (args,) = conn.sql("INSERT INTO booking.job")
    assert args == ("bk_probe", json.dumps({"a": 1}), "k", 5, "system", 5.0)


async def test_claim_maps_the_row_and_parses_text_payload() -> None:
    row = {"id": "j1", "kind": "bk_probe", "payload": '{"x": 2}', "attempt": 1, "max_attempts": 5}
    conn = ScriptedConn([("UPDATE booking.job SET status = 'running'", row)])
    job = await queue.claim(conn, "w", ["bk_probe"], 60)
    assert job == Job("j1", "bk_probe", {"x": 2}, 1, 5)
    assert await queue.claim(ScriptedConn(), "w", ["bk_probe"], 60) is None


async def test_claim_skips_cancel_requests_and_stamps_the_attempt() -> None:
    conn = ScriptedConn()
    await queue.claim(conn, "w", ["bk_probe"], 60)
    ((sql, _),) = conn.calls
    assert "AND NOT cancel_requested" in sql
    assert "progress = progress || jsonb_build_object('attempt_started_at', now())" in sql
    assert "started_at = coalesce(started_at, now())" in sql  # the first attempt's start is kept (F18)


async def test_heartbeat_and_complete_report_ownership() -> None:
    assert await queue.heartbeat(ScriptedConn([("locked_until = now()", 1)]), "j1", "w", 60) is True
    assert await queue.heartbeat(ScriptedConn(), "j1", "w", 60) is False
    conn = ScriptedConn([("status = 'succeeded'", 1)])
    assert await queue.complete(conn, "j1", "w", {"ok": True}) is True
    assert conn.sql("status = 'succeeded'")[0][2] == '{"ok": true}'
    assert await queue.complete(ScriptedConn(), "j1", "w", None) is False


async def test_fail_requeues_while_attempts_remain() -> None:
    conn = ScriptedConn([("ELSE 'queued' END", "queued")])
    assert await queue.fail(conn, JOB, "w", "e", retryable=True) == "queued"
    (sql,) = [flat for flat, _ in conn.calls]
    assert "WHEN cancel_requested THEN 'cancelled'" in sql and "RETURNING status" in sql
    # A cancel request ends the job instead of re-queueing it (the claim would skip it forever).
    assert await queue.fail(ScriptedConn([("ELSE 'queued' END", "cancelled")]), JOB, "w", "e", retryable=True) == (
        "cancelled"
    )
    assert await queue.fail(ScriptedConn(), JOB, "w", "e", retryable=True) == ""


async def test_fail_is_final_when_not_retryable_or_out_of_attempts() -> None:
    last = Job("j1", "bk_probe", {}, attempt=3, max_attempts=3)
    for job, retryable in ((JOB, False), (last, True)):
        assert (
            await queue.fail(ScriptedConn([("status = 'failed'", 1)]), job, "w", "e", retryable=retryable) == "failed"
        )
    assert await queue.fail(ScriptedConn(), JOB, "w", "e", retryable=False) == ""


async def test_release_returns_the_new_status_and_passes_the_cap() -> None:
    conn = ScriptedConn([("WITH cur AS", "queued")])
    assert await queue.release(conn, "j1", "w") == "queued"
    ((sql, args),) = conn.calls
    assert args == ("j1", "w", queue.MAX_RELEASES)
    assert "WHERE j.id = $1::uuid AND j.status = 'running' AND j.locked_by = $2" in sql
    assert "attempt = greatest(j.attempt - 1, 0)" in sql and "run_after = now()" in sql
    assert "locked_by = NULL, locked_until = NULL" in sql
    assert "jsonb_build_object('released', true, 'releases', cur.releases)" in sql
    assert "WHEN cur.cancel_requested THEN 'cancelled' WHEN cur.releases >= $3 THEN 'failed' ELSE 'queued'" in sql
    assert await queue.release(ScriptedConn(), "j1", "w") == ""  # no longer ours


async def test_reap_and_prune_return_counts() -> None:
    conn = ScriptedConn([("re-queued", 2), ("no attempts left", {"failed": 1, "cancelled": 3})])
    assert await queue.reap(conn) == {"requeued": 2, "failed": 1, "cancelled": 3}
    requeue_sql, end_sql = (flat for flat, _ in conn.calls)
    assert "AND NOT cancel_requested" in requeue_sql
    assert "status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END" in end_sql
    assert await queue.reap(ScriptedConn()) == {"requeued": 0, "failed": 0, "cancelled": 0}
    assert await queue.prune(ScriptedConn([("DELETE FROM booking.job", 4)]), "bk_inquiry_pull", 7) == 4
