"""HTTP contract of /booking/health and /booking/health/ready (root ADR 0004 decisions 4 and 9)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import Database, Readiness
from app.main import create_app
from app.settings import Settings

SETTINGS = Settings(database_url=None, git_sha="0123abc", app_version="0.2.0")


class ExplodingDatabase(Database):
    """Fails the test if liveness touches the database."""

    async def readiness(self) -> Readiness:
        raise AssertionError("liveness must not call the database")

    async def pool(self):  # type: ignore[override]
        raise AssertionError("liveness must not call the database")


class FixedDatabase(Database):
    def __init__(self, result: Readiness) -> None:
        super().__init__(SETTINGS)
        self._result = result

    async def readiness(self) -> Readiness:
        return self._result


def test_liveness_shape_and_no_db_call() -> None:
    with TestClient(create_app(SETTINGS, ExplodingDatabase(SETTINGS))) as client:
        r = client.get("/booking/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "aa-booking-api", "version": "0.2.0", "git_sha": "0123abc"}


def test_ready_200_when_ready() -> None:
    body = {"status": "ok", "db": "ok", "schema_version": "bk-001"}
    with TestClient(create_app(SETTINGS, FixedDatabase(Readiness(True, body)))) as client:
        r = client.get("/booking/health/ready")
    assert r.status_code == 200
    assert r.json() == body


def test_ready_503_when_not_ready() -> None:
    body = {"status": "not_ready", "db": "error", "error": "OSError", "schema_version": None}
    with TestClient(create_app(SETTINGS, FixedDatabase(Readiness(False, body)))) as client:
        r = client.get("/booking/health/ready")
    assert r.status_code == 503
    assert r.json() == body


def test_ready_503_when_database_url_missing() -> None:
    with TestClient(create_app(SETTINGS)) as client:
        r = client.get("/booking/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "db": "not_configured", "schema_version": None}


def test_only_booking_prefix_is_served() -> None:
    with TestClient(create_app(SETTINGS)) as client:
        assert client.get("/health").status_code == 404
        # SETTINGS is ENVIRONMENT=dev: the schema is served under /booking but gated (AA-876, test_docs_gate.py).
        assert client.get("/booking/openapi.json").status_code == 403
