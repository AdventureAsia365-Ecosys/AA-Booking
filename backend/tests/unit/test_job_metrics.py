"""Backlog sampler (AA-751 B5; S263 checks instead of CloudWatch): the pure sample rule, the hand-off to the in-app
checks, and the never-raise loop. The SQL runs against Postgres in tests/integration/test_job_metrics.py."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.jobs import metrics
from app.jobs.kinds import DEFAULT_EXPECTED_SECONDS, EXPECTED_SECONDS, expected_seconds, overdue_after_seconds
from app.jobs.metrics import JobMetricsSampler, Sample, backlog_sample
from tests.unit.fakes import ScriptedConn


def test_expected_seconds_and_default() -> None:
    assert expected_seconds("bk_inquiry_pull") == 60
    assert expected_seconds("bk_retention_job") == 900
    assert expected_seconds("bk_unknown") == DEFAULT_EXPECTED_SECONDS == 300
    assert overdue_after_seconds("bk_notify_email") == 120
    assert overdue_after_seconds("bk_unknown") == 600
    assert all(kind.startswith("bk_") for kind in EXPECTED_SECONDS)


def test_nothing_due_nothing_running_is_zero() -> None:
    assert backlog_sample(None, []) == Sample(0.0, 0, 0, 0.0)


def test_due_rows_age_when_nothing_runs() -> None:
    assert backlog_sample(1200.4, []).oldest_queued_age_s == 1200.4


def test_a_healthy_running_job_zeroes_the_queue_age() -> None:
    s = backlog_sample(1200.0, [("bk_retention_job", 800.0)])  # 800 < 2 x 900
    assert (s.oldest_queued_age_s, s.overdue_running, s.running, s.due_queued_age_s) == (0.0, 0, 1, 1200.0)


def test_an_overdue_running_job_counts_and_does_not_hide_the_queue() -> None:
    s = backlog_sample(1000.0, [("bk_inquiry_process", 121.0)])  # > 2 x 60
    assert (s.oldest_queued_age_s, s.overdue_running) == (1000.0, 1)


def test_overdue_threshold_is_strict_and_unknown_kinds_use_the_default() -> None:
    assert backlog_sample(None, [("bk_notify_email", 120.0)]).overdue_running == 0
    assert backlog_sample(None, [("bk_x6_probe", 600.0), ("bk_x6_probe", 601.0)]).overdue_running == 1


def test_one_healthy_job_among_overdue_ones_still_zeroes_the_queue_age() -> None:
    # Rolling deploy: two workers, one stuck, one healthy. The stuck one shows as overdue.
    s = backlog_sample(950.0, [("bk_inquiry_pull", 500.0), ("bk_inquiry_pull", 5.0)])
    assert (s.oldest_queued_age_s, s.overdue_running) == (0.0, 1)


def test_unknown_attempt_age_is_healthy_and_negative_ages_clamp() -> None:
    s = backlog_sample(-3.0, [("bk_inquiry_pull", None)])
    assert (s.oldest_queued_age_s, s.overdue_running, s.due_queued_age_s) == (0.0, 0, 0.0)


def test_no_cloudwatch_left() -> None:
    """S263: the AA/Jobs/Booking metrics and their alarms are gone (app.jobs.ops_checks instead)."""
    for name in ("metric_data", "cloudwatch_client", "NAMESPACE", "CLOUDWATCH_REGION"):
        assert not hasattr(metrics, name)


class FakePublisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[list[Any]] = []
        self.error = error

    async def alert(self, pool: Any, found: list[Any], now: Any = None) -> dict[str, str]:
        self.calls.append(found)
        if self.error:
            raise self.error
        return {f.check: "sent" for f in found}


def _pool(conn: ScriptedConn):
    async def provide() -> ScriptedConn:
        return conn

    return provide


def _conn(oldest: Any = 1000.0, running: Any = ()) -> ScriptedConn:
    return ScriptedConn(
        [
            ("min(run_after)", oldest),
            ("WHERE status = 'running'", [{"kind": k, "attempt_age_s": a} for k, a in running]),
        ]
    )


@pytest.mark.asyncio
async def test_sample_once_hands_the_findings_to_the_publisher(caplog: pytest.LogCaptureFixture) -> None:
    pub = FakePublisher()
    sampler = JobMetricsSampler(_pool(_conn(1000.0, [("bk_inquiry_pull", 700.0)])), publisher=pub)  # type: ignore[arg-type]
    caplog.set_level(logging.INFO, logger="app.jobs.metrics")
    sample = await sampler.sample_once()
    assert sample == Sample(1000.0, 1, 1, 1000.0)
    assert [f.check for f in pub.calls[0]] == ["jobs_stuck", "jobs_overdue"]
    assert "ops_metrics_sampled oldest_queued_age_s=1000 overdue_running_jobs=1 running=1" in caplog.text
    assert "checks=jobs_overdue:sent,jobs_stuck:sent" in caplog.text


@pytest.mark.asyncio
async def test_a_healthy_sample_checks_nothing(caplog: pytest.LogCaptureFixture) -> None:
    pub = FakePublisher()
    caplog.set_level(logging.INFO, logger="app.jobs.metrics")
    await JobMetricsSampler(_pool(_conn(None, [])), publisher=pub).sample_once()  # type: ignore[arg-type]
    assert pub.calls == [[]] and "checks=ok" in caplog.text


@pytest.mark.asyncio
async def test_sample_once_never_raises_on_a_failed_read(caplog: pytest.LogCaptureFixture) -> None:
    pub = FakePublisher()
    broken_read = JobMetricsSampler(_pool(_conn(oldest=ConnectionError("db down"))), publisher=pub)  # type: ignore[arg-type]
    assert await broken_read.sample_once() is None
    assert pub.calls == []  # nothing to check when the read fails

    async def no_pool() -> Any:
        raise OSError("connect refused")

    assert await JobMetricsSampler(no_pool, publisher=pub).sample_once() is None  # type: ignore[arg-type]
    assert "stage=read error=ConnectionError" in caplog.text


def test_default_publisher_reads_the_topic_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERTS_TOPIC_ARN", "topic-placeholder")
    assert JobMetricsSampler(_pool(_conn()))._publisher.configured


@pytest.mark.asyncio
async def test_run_forever_samples_at_once_then_every_period_until_stop() -> None:
    pub = FakePublisher()
    sampler = JobMetricsSampler(_pool(_conn()), every_s=0.01, publisher=pub)  # type: ignore[arg-type]
    stop = asyncio.Event()
    task = asyncio.create_task(sampler.run_forever(stop))
    for _ in range(200):
        if len(pub.calls) >= 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert len(pub.calls) >= 3


@pytest.mark.asyncio
async def test_run_forever_keeps_going_after_failures_and_stops_on_cancel() -> None:
    reads: list[int] = []

    async def flaky() -> Any:
        reads.append(1)
        raise OSError("down")

    sampler = JobMetricsSampler(flaky, every_s=0.01, publisher=FakePublisher())  # type: ignore[arg-type]
    task = asyncio.create_task(sampler.run_forever(asyncio.Event()))
    for _ in range(200):
        if len(reads) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(reads) >= 3
