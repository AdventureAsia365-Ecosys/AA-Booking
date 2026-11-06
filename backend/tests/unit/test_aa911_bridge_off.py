"""AA-911 (S271, root ADR 0019 d23): the CIS -> Booking bridge is retired. The verifier the app builds in production
(``create_app`` without an injected verifier -> ``identity_config.build_staff_verifier(settings)``) accepts the
WORKFORCE issuer only: a CIS-minted bridge token is 401 before any DB access, a WORKFORCE token still signs in.
Local RSA keys + fake JWKS, no network."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.auth import identity_config
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import ISSUER, WORKFORCE_SUB, LocalCis
from tests.unit.fakes import ScriptedConn

DSN = "postgresql://booking:x@db.test/aa"
IDENTITY_SELECT = "cognito_iss = $1 AND cognito_sub = $2"


class FakeDatabase(Database):
    def __init__(self, settings: Settings, conn: ScriptedConn) -> None:
        super().__init__(settings)
        self.conn = conn

    @property
    def configured(self) -> bool:
        return True

    async def pool(self):  # type: ignore[override]
        return self.conn


def cognito_row() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "cis_user_id": f"cog:{WORKFORCE_SUB}",
        "email": "staff@example.test",
        "name": "staff",
        "role": "sales",
        "active": True,
        "cis_role": None,
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "cognito_sub": WORKFORCE_SUB,
    }


def production_client(cis: LocalCis, conn: ScriptedConn) -> TestClient:
    """The verifier exactly as production builds it (no include_bridge), over the local issuers' JWKS."""
    settings = replace(cis.settings(), database_url=DSN)
    verifier = identity_config.build_staff_verifier(settings, cis.fetch)
    return TestClient(create_app(settings, FakeDatabase(settings, conn), verifier))


def test_production_verifier_holds_the_workforce_issuer_only() -> None:
    verifier = identity_config.build_staff_verifier(LocalCis().settings())
    assert identity_config.issuer_kind(verifier, ISSUER) is None
    assert [i.name for i in verifier.issuers] == [identity_config.WORKFORCE]


def test_a_bridge_token_is_refused_before_any_db_access() -> None:
    cis = LocalCis()
    conn = ScriptedConn()
    with production_client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers={"Authorization": f"Bearer {cis.token(cis_role='admin')}"})
    assert r.status_code == 401
    assert conn.calls == []


def test_a_workforce_token_still_signs_in() -> None:
    cis = LocalCis()
    conn = ScriptedConn().on(IDENTITY_SELECT, cognito_row())
    with production_client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers={"Authorization": f"Bearer {cis.workforce.token(['booking-sales'])}"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "sales"
