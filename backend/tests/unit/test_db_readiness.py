"""Database.readiness() against a fake pool: every failure is a 503 body, never a leaked DSN."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

import app.db as db_module
from app.db import REQUIRED_SCHEMA_VERSION, REQUIRED_SCHEMA_VERSIONS, Database
from app.settings import Settings

DSN = "postgresql://booking:super-secret@db.internal:5432/aa_cis"
SETTINGS = Settings(database_url=DSN)


class FakeConn:
    def __init__(self, answers: dict[str, Any]) -> None:
        self._answers = answers

    async def fetchval(self, query: str) -> Any:
        answer = self._answers[query]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakePool:
    def __init__(self, answers: dict[str, Any]) -> None:
        self._answers = answers
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self._answers)

    async def close(self) -> None:
        self.closed = True


VERSIONS_SQL = "SELECT coalesce(array_agg(version ORDER BY version), '{}') FROM booking.v_schema_versions"


def _answers(versions: Any = REQUIRED_SCHEMA_VERSIONS, select_one: Any = 1) -> dict[str, Any]:
    if isinstance(versions, tuple):
        versions = list(versions)
    return {"SELECT 1": select_one, VERSIONS_SQL: versions}


@pytest.fixture
def answers() -> dict[str, Any]:
    """What the fake connection returns per query; tests overwrite entries."""
    return _answers()


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_create_pool(**kwargs: Any) -> FakePool:
        calls.append(kwargs)
        return FakePool(answers)

    monkeypatch.setattr(db_module.asyncpg, "create_pool", fake_create_pool)
    return calls


@pytest.mark.asyncio
async def test_ready_when_db_answers_and_required_version_registered(created: list[dict[str, Any]]) -> None:
    result = await Database(SETTINGS).readiness()
    assert result.ok
    assert result.body == {"status": "ok", "db": "ok", "schema_version": REQUIRED_SCHEMA_VERSION}


@pytest.mark.asyncio
async def test_not_ready_when_only_an_older_version_is_registered(
    created: list[dict[str, Any]], answers: dict[str, Any]
) -> None:
    answers.update(_answers(versions=["bk-001"]))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body["schema_version"] == "bk-001"
    assert result.body["required_schema_version"] == REQUIRED_SCHEMA_VERSION
    assert result.body["missing_schema_versions"] == [v for v in REQUIRED_SCHEMA_VERSIONS if v != "bk-001"]


@pytest.mark.asyncio
async def test_not_ready_when_an_older_required_version_is_missing(
    created: list[dict[str, Any]], answers: dict[str, Any]
) -> None:
    # The newest file is in but one before it is not (e.g. bk-005 applied, bk-003 never ran): not ready.
    gap = [v for v in REQUIRED_SCHEMA_VERSIONS if v != "bk-003"]
    answers.update(_answers(versions=gap))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body["schema_version"] == REQUIRED_SCHEMA_VERSION
    assert result.body["missing_schema_versions"] == ["bk-003"]


@pytest.mark.asyncio
async def test_pool_is_lazy_single_and_requires_ssl(created: list[dict[str, Any]]) -> None:
    db = Database(SETTINGS)
    assert created == []  # nothing at construction time
    await db.readiness()
    await db.readiness()
    assert len(created) == 1
    assert created[0]["ssl"] == "require"
    assert created[0]["min_size"] == 0
    await db.close()


@pytest.mark.asyncio
async def test_newer_schema_is_ready(created: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    answers.update(_answers(versions=[*REQUIRED_SCHEMA_VERSIONS, "bk-999"]))
    result = await Database(SETTINGS).readiness()
    assert result.ok and result.body["schema_version"] == "bk-999"


@pytest.mark.asyncio
async def test_not_ready_when_no_booking_version(created: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    answers.update(_answers(versions=[]))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body == {
        "status": "not_ready",
        "db": "ok",
        "schema_version": None,
        "required_schema_version": REQUIRED_SCHEMA_VERSION,
        "missing_schema_versions": list(REQUIRED_SCHEMA_VERSIONS),
    }


@pytest.mark.asyncio
async def test_not_ready_when_view_missing(created: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    class UndefinedTableError(Exception):
        pass

    answers.update(_answers(versions=UndefinedTableError("relation does not exist")))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body["db"] == "ok"
    assert result.body["error"] == "UndefinedTableError"


@pytest.mark.asyncio
async def test_select_one_failure_is_db_error(created: list[dict[str, Any]], answers: dict[str, Any]) -> None:
    answers.update(_answers(select_one=TimeoutError()))
    result = await Database(SETTINGS).readiness()
    assert not result.ok
    assert result.body["db"] == "error" and result.body["error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_db_error_does_not_leak_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_create_pool(**kwargs: Any) -> FakePool:
        raise OSError(f"could not connect to {DSN}")

    monkeypatch.setattr(db_module.asyncpg, "create_pool", failing_create_pool)
    db = Database(SETTINGS)
    result = await db.readiness()
    assert not result.ok
    assert result.body == {"status": "not_ready", "db": "error", "error": "OSError", "schema_version": None}
    assert "super-secret" not in repr(result.body)


@pytest.mark.asyncio
async def test_pool_creation_is_retried_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    async def flaky_create_pool(**kwargs: Any) -> FakePool:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("db down")
        return FakePool(_answers())

    monkeypatch.setattr(db_module.asyncpg, "create_pool", flaky_create_pool)
    db = Database(SETTINGS)
    assert not (await db.readiness()).ok
    assert (await db.readiness()).ok
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_close_without_pool_is_a_noop() -> None:
    await Database(SETTINGS).close()
