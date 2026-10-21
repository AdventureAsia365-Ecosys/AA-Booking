"""AA-876 — /booking/docs and /booking/openapi.json are gated outside test/local: staff.admin only, else 403.

AA-911: staff sign in with Cognito WORKFORCE tokens only; a retired CIS bridge token is one more 403 here."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalRsaIssuer, LocalStaff
from tests.unit.fakes import ScriptedConn
from tests.unit.test_staff_auth import IDENTITY_SELECT, FakeDatabase, cognito_row

DEV = Settings(database_url="postgresql://booking:x@db.test/aa", environment="dev")
TEST = Settings(database_url="postgresql://booking:x@db.test/aa", environment="test")
PATHS = ["/booking/openapi.json", "/booking/docs"]


def _client(settings: Settings, conn: ScriptedConn, idp: LocalStaff) -> TestClient:
    db = FakeDatabase(conn)
    return TestClient(create_app(settings, db, idp.verifier()))


def _linked_conn(role: str = "admin") -> ScriptedConn:
    """The caller's linked WORKFORCE row, already in sync with a token in the ``booking-<role>`` group."""
    return ScriptedConn().on(IDENTITY_SELECT, cognito_row(role=role, last_seen_at=datetime.now(UTC)))


@pytest.mark.parametrize("path", PATHS)
def test_dev_without_a_token_is_403(path: str) -> None:
    with _client(DEV, ScriptedConn(), LocalStaff()) as c:
        r = c.get(path)
    assert r.status_code == 403
    assert r.json() == {"detail": "docs_forbidden"}


@pytest.mark.parametrize("path", PATHS)
def test_dev_with_a_bad_token_is_403_not_401(path: str) -> None:
    idp = LocalStaff()
    with _client(DEV, ScriptedConn(), idp) as c:
        r = c.get(path, headers={"Authorization": f"Bearer {idp.token(role='admin', client_id='cis')}"})
    assert r.status_code == 403
    assert "www-authenticate" not in {k.lower() for k in r.headers}


@pytest.mark.parametrize("path", PATHS)
def test_dev_with_a_retired_bridge_token_is_403(path: str) -> None:
    cis = LocalRsaIssuer()
    idp, conn = LocalStaff(foreign=cis), ScriptedConn()
    with _client(DEV, conn, idp) as c:
        r = c.get(path, headers={"Authorization": f"Bearer {cis.token(cis_role='admin')}"})
    assert r.status_code == 403
    assert r.json() == {"detail": "docs_forbidden"}
    # no staff row read or written; the CIS JWKS is never asked for (the lifespan may prefetch the WORKFORCE one)
    assert conn.calls == [] and cis.fetches == 0
    assert all("api-cis" not in url for url in idp.fetched)


@pytest.mark.parametrize("path", PATHS)
def test_dev_auth_unavailable_is_403(path: str) -> None:
    idp = LocalStaff()
    idp.fail_next = True
    with _client(DEV, ScriptedConn(), idp) as c:
        r = c.get(path, headers={"Authorization": f"Bearer {idp.token(role='admin')}"})
    assert r.status_code == 403


@pytest.mark.parametrize("path", PATHS)
def test_dev_non_admin_staff_is_403(path: str) -> None:
    idp = LocalStaff()
    with _client(DEV, _linked_conn(role="sales"), idp) as c:
        r = c.get(path, headers={"Authorization": f"Bearer {idp.token(role='sales')}"})
    assert r.status_code == 403
    assert r.json() == {"detail": "docs_forbidden"}


def test_dev_admin_gets_the_schema_and_the_page() -> None:
    idp = LocalStaff()
    with _client(DEV, _linked_conn(), idp) as c:
        headers = {"Authorization": f"Bearer {idp.token(role='admin')}"}
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
    with _client(TEST, ScriptedConn(), LocalStaff()) as c:
        assert c.get(path).status_code == 200


def test_fastapi_default_doc_routes_are_gone() -> None:
    with _client(DEV, ScriptedConn(), LocalStaff()) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/booking/redoc").status_code == 404
