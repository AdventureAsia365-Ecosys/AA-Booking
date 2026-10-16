"""Backlog metrics sampler (AA-751 B5): the pure sample rule, the metric payload, and the never-raise loop.
The SQL runs against Postgres in tests/integration/test_job_metrics.py."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.jobs import metrics
from app.jobs.kinds import DEFAULT_EXPECTED_SECONDS, EXPECTED_SECONDS, expected_seconds, overdue_after_seconds
from app.jobs.metrics import JobMetricsSampler, Sample, backlog_sample, metric_data
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


def test_metric_data_has_no_dimensions_and_the_alarm_units() -> None:
    assert metric_data(Sample(901.0, 2, 3, 901.0)) == [
        {"MetricName": "OldestQueuedAgeSeconds", "Value": 901.0, "Unit": "Seconds"},
        {"MetricName": "OverdueRunningJobs", "Value": 2.0, "Unit": "Count"},
    ]


def test_cloudwatch_client_uses_short_timeouts_and_one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    seen: dict[str, Any] = {}

    def fake_client(service: str, **kw: Any) -> str:
        seen.update(kw, service=service)
        return "client"

    monkeypatch.setattr(boto3, "client", fake_client)
    assert metrics.cloudwatch_client() == "client"
    config = seen["config"]
    assert (seen["service"], seen["region_name"]) == ("cloudwatch", "us-west-1")
    assert (config.connect_timeout, config.read_timeout, config.retries) == (3, 5, {"max_attempts": 1})


class FakeCloudWatch:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {}


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
async def test_sample_once_puts_one_call_in_the_booking_namespace(caplog: pytest.LogCaptureFixture) -> None:
    cw = FakeCloudWatch()
    made: list[int] = []

    def factory() -> FakeCloudWatch:
        made.append(1)
        return cw

    sampler = JobMetricsSampler(_pool(_conn(1000.0, [("bk_inquiry_pull", 700.0)])), client_factory=factory)
    caplog.set_level(logging.INFO, logger="app.jobs.metrics")
    sample = await sampler.sample_once()
    assert sample == Sample(1000.0, 1, 1, 1000.0)
    await sampler.sample_once()
    assert len(made) == 1  # the client is created once and reused
    assert len(cw.calls) == 2
    assert cw.calls[0] == {"Namespace": "AA/Jobs/Booking", "MetricData": metric_data(sample)}
    assert "ops_metrics_sampled namespace=AA/Jobs/Booking oldest_queued_age_s=1000" in caplog.text
    assert "overdue_running_jobs=1 running=1" in caplog.text


@pytest.mark.asyncio
async def test_sample_once_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    cw = FakeCloudWatch()
    broken_read = JobMetricsSampler(_pool(_conn(oldest=ConnectionError("db down"))), client_factory=lambda: cw)
    assert await broken_read.sample_once() is None
    assert cw.calls == []  # no datapoint at all when the read fails (that is what sampling-stopped sees)

    async def no_pool() -> Any:
        raise OSError("connect refused")

    assert await JobMetricsSampler(no_pool, client_factory=lambda: cw).sample_once() is None

    denied = JobMetricsSampler(_pool(_conn()), client_factory=lambda: FakeCloudWatch(PermissionError("AccessDenied")))
    assert await denied.sample_once() is None

    def factory_fails() -> FakeCloudWatch:
        raise RuntimeError("no credentials")

    assert await JobMetricsSampler(_pool(_conn()), client_factory=factory_fails).sample_once() is None
    assert "stage=read error=ConnectionError" in caplog.text
    assert "stage=put error=PermissionError: AccessDenied" in caplog.text


@pytest.mark.asyncio
async def test_run_forever_samples_at_once_then_every_period_until_stop() -> None:
    cw = FakeCloudWatch()
    sampler = JobMetricsSampler(_pool(_conn()), every_s=0.01, client_factory=lambda: cw)
    stop = asyncio.Event()
    task = asyncio.create_task(sampler.run_forever(stop))
    for _ in range(200):
        if len(cw.calls) >= 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert len(cw.calls) >= 3


@pytest.mark.asyncio
async def test_run_forever_keeps_going_after_failures_and_stops_on_cancel() -> None:
    reads: list[int] = []

    async def flaky() -> Any:
        reads.append(1)
        raise OSError("down")

    sampler = JobMetricsSampler(flaky, every_s=0.01, client_factory=FakeCloudWatch)
    task = asyncio.create_task(sampler.run_forever(asyncio.Event()))
    for _ in range(200):
        if len(reads) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(reads) >= 3
