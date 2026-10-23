"""The app lifespan starts the job worker (and the backlog metrics sampler) only when enabled, and stops them on
shutdown."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.settings import Settings


class FakeWorker:
    instances: list[FakeWorker] = []

    def __init__(
        self,
        pool: Any,
        handlers: dict,
        *,
        recurring: tuple = (),
        hooks: tuple = (),
        no_release: frozenset = frozenset(),
    ) -> None:
        self.handlers, self.recurring, self.hooks, self.no_release = handlers, recurring, hooks, no_release
        self.started = self.stopped = False
        FakeWorker.instances.append(self)

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.started = True
        await stop.wait()
        self.stopped = True


class FakeSampler:
    instances: list[FakeSampler] = []

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self.started = self.cancelled = False
        FakeSampler.instances.append(self)

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()  # only a cancel ends it: the lifespan must cancel it, not wait for it
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.fixture(autouse=True)
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWorker.instances = []
    FakeSampler.instances = []
    monkeypatch.setattr(main_module, "Worker", FakeWorker)
    monkeypatch.setattr(main_module, "JobMetricsSampler", FakeSampler)


def _app(**kw: Any):
    settings = Settings(database_url=kw.pop("database_url", "postgresql://u@h/db"), **kw)
    return main_module.create_app(settings, Database(settings))


def test_worker_runs_for_the_app_lifetime_when_enabled() -> None:
    with TestClient(_app(worker_enabled=True)) as client:
        assert client.get("/booking/health").status_code == 200
    (worker,) = FakeWorker.instances
    assert worker.started and worker.stopped
    assert set(worker.handlers) == {
        "bk_inquiry_pull",
        "bk_inquiry_process",
        "bk_notify_email",
        "bk_retention_job",
        "bk_retention_inbox_redact",
        "bk_b2b_hold_expire",
        "bk_tour_lifecycle_pull",
    }
    assert [r.kind for r in worker.recurring] == ["bk_inquiry_pull"]
    # AA-825 daily retention runs; AA-973 B2B hold expiry when a hold is due; AA-986 L5 the CIS lifecycle outbox pull
    # when the outbox has a new row.
    assert [type(h).__name__ for h in worker.hooks] == ["DailySchedule", "ExpirySchedule", "PullSchedule"]
    # AA-751: retention runs keep no progress checkpoint, so a shutdown fails them instead of releasing them.
    assert worker.no_release == {"bk_retention_job", "bk_retention_inbox_redact"}
    # AA-751: the backlog metrics sampler runs for the app lifetime too and is cancelled on shutdown.
    (sampler,) = FakeSampler.instances
    assert sampler.started and sampler.cancelled


def test_every_handled_kind_has_an_expected_run_time() -> None:
    """app/jobs/kinds.py EXPECTED_SECONDS lists exactly the kinds the worker handles (overdue-running metric)."""
    from app.jobs.kinds import EXPECTED_SECONDS

    with TestClient(_app(worker_enabled=True)):
        pass
    (worker,) = FakeWorker.instances
    assert set(EXPECTED_SECONDS) == set(worker.handlers)


@pytest.mark.parametrize("kw", [{"worker_enabled": False}, {"worker_enabled": True, "database_url": None}])
def test_worker_not_started(kw: dict) -> None:
    with TestClient(_app(**kw)) as client:
        client.get("/booking/health")
    assert FakeWorker.instances == []
    assert FakeSampler.instances == []
