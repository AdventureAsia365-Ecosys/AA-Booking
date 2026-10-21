"""AA-876 — /booking/docs and /booking/openapi.json are gated outside test/local: staff.admin only, else 403."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_staff_auth import FakeDatabase, staff_row

DEV = Settings(database_url="postgresql://booking:x@db.test/aa", environment="dev")
TEST = Settings(database_url="postgresql://booking:x@db.test/aa", environment="test")
PATHS = ["/booking/openapi.json", "/booking/docs"]


def _client(settings: Settings, conn: ScriptedConn, cis: LocalCis) -> TestClient:
    db = FakeDatabase(conn)
    return TestClient(create_app(settings, db, cis.verifier()))


def _admin_conn(role: str = "admin") -> ScriptedConn:
    def row(*a: Any) -> dict[str, Any]:
        return {**staff_row(a[0], role=role), "inserted": False}

    return ScriptedConn().on("INSERT INTO booking.staff_users", row)


@pytest.mark.parametrize("path", PATHS)
def test_dev_without_a_token_is_403(path: str) -> None:
    with _client(DEV, ScriptedConn(), LocalCis()) as c:
        r = c.get(path)
    assert r.status_code == 403
    assert r.json() == {"detail": "docs_forbidden"}


@pytest.mark.parametrize("path", PATHS)
def test_dev_with_a_bad_token_is_403_not_401(path: str) -> None:
    cis = LocalCis()
    with _client(DEV, ScriptedConn(), cis) as c:
        r = c.get(path, headers={"Authorization": f"Bearer {cis.token(aud='cis')}"})
    assert r.status_code == 403
    assert "www-authenticate" not in {k.lower() for k in r.headers}


@pytest.mark.parametrize("path", PATHS)
def test_dev_auth_unavailable_is_403(path: str) -> None:
    cis = LocalCis()
    cis.fail_next = True
    with _client(DEV, ScriptedConn(), cis) as c:
        r = c.get(path, headers={"Authorization": f"Bearer {cis.token()}"})
    assert r.status_code == 403


@pytest.mark.parametrize("path", PATHS)
def test_dev_non_admin_staff_is_403(path: str) -> None:
    cis = LocalCis()
    with _client(DEV, _admin_conn(role="sales"), cis) as c:
        r = c.get(path, headers={"Authorization": f"Bearer {cis.token(sub='u-1', cis_role='reviewer')}"})
    assert r.status_code == 403
    assert r.json() == {"detail": "docs_forbidden"}


def test_dev_admin_gets_the_schema_and_the_page() -> None:
    cis = LocalCis()
    with _client(DEV, _admin_conn(), cis) as c:
        headers = {"Authorization": f"Bearer {cis.token(sub='u-1', cis_role='admin')}"}
        schema = c.get("/booking/openapi.json", headers=headers)
        page = c.get("/booking/docs", headers=headers)
    assert schema.status_code == 200
    body = schema.json()
    assert body["info"]["title"] == "AA-Booking API"
    assert "/booking/health" in body["paths"]
    # the docs routes themselves stay out of the schema
    assert "/booking/openapi.json" not in body["paths"] and "/booking/docs" not in body["paths"]
    assert page.status_code == 200 and "swagger-ui" in page.text


@pytest.mark.parametrize("path", PATHS)
def test_test_environment_is_open(path: str) -> None:
    with _client(TEST, ScriptedConn(), LocalCis()) as c:
        assert c.get(path).status_code == 200


def test_fastapi_default_doc_routes_are_gone() -> None:
    with _client(DEV, ScriptedConn(), LocalCis()) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/booking/redoc").status_code == 404
