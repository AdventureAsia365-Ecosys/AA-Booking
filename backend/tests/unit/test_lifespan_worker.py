"""The app lifespan starts the job worker only when enabled, and stops it on shutdown."""

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


@pytest.fixture(autouse=True)
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWorker.instances = []
    monkeypatch.setattr(main_module, "Worker", FakeWorker)


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
    }
    assert [r.kind for r in worker.recurring] == ["bk_inquiry_pull"]
    assert [type(h).__name__ for h in worker.hooks] == ["DailySchedule"]  # AA-825 daily retention runs
    # AA-751: retention runs keep no progress checkpoint, so a shutdown fails them instead of releasing them.
    assert worker.no_release == {"bk_retention_job", "bk_retention_inbox_redact"}


@pytest.mark.parametrize("kw", [{"worker_enabled": False}, {"worker_enabled": True, "database_url": None}])
def test_worker_not_started(kw: dict) -> None:
    with TestClient(_app(**kw)) as client:
        client.get("/booking/health")
    assert FakeWorker.instances == []
