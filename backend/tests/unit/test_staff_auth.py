"""Staff authentication end to end over HTTP (local RSA key + fake JWKS), provisioning and admin ops."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import admin
from app.auth.staff import PERMISSIONS, Staff, provision, require
from app.auth.tokens import StaffClaims
from app.db import Database
from app.errors import Conflict, Invalid, NotFound
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalCis
from tests.unit.fakes import ScriptedConn

SETTINGS = Settings(database_url="postgresql://booking:x@db.test/aa", environment="test")
NOW = datetime(2026, 10, 14, tzinfo=UTC)


def staff_row(cis_user_id: str = "u-1", role: str = "admin", active: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "cis_user_id": cis_user_id,
        "email": "staff@example.test",
        "name": "Staff One",
        "role": role,
        "active": active,
        "cis_role": "admin",
        "created_at": NOW,
        "updated_at": NOW,
        "last_seen_at": NOW,
        **extra,
    }


class FakeDatabase(Database):
    def __init__(self, conn: ScriptedConn, configured: bool = True) -> None:
        super().__init__(SETTINGS)
        self.conn = conn
        self._configured = configured

    @property
    def configured(self) -> bool:
        return self._configured

    async def pool(self):  # type: ignore[override]
        return self.conn


def client(conn: ScriptedConn, cis: LocalCis, configured: bool = True) -> TestClient:
    return TestClient(create_app(SETTINGS, FakeDatabase(conn, configured), cis.verifier()))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── /booking/staff/me ────────────────────────────────────────────────────────


def test_me_without_token_is_401() -> None:
    with client(ScriptedConn(), LocalCis()) as c:
        r = c.get("/booking/staff/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_me_with_a_bad_token_is_401() -> None:
    cis = LocalCis()
    with client(ScriptedConn(), cis) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.token(aud="cis")))
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_token"


def test_me_when_cis_jwks_is_down_is_503() -> None:
    cis = LocalCis()
    cis.fail_next = True
    with client(ScriptedConn(), cis) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.token()))
    assert r.status_code == 503
    assert r.json()["detail"] == "auth_unavailable"


def test_unknown_cis_role_is_403_and_nothing_is_written() -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with client(conn, cis) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.token(cis_role="superuser")))
    assert r.status_code == 403
    assert r.json()["detail"] == "unknown_cis_role"
    assert conn.calls == []


def test_db_not_configured_is_503() -> None:
    cis = LocalCis()
    with client(ScriptedConn(), cis, configured=False) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.token()))
    assert r.status_code == 503


def test_first_admin_login_provisions_an_active_admin() -> None:
    cis = LocalCis()
    conn = ScriptedConn().on("INSERT INTO booking.staff_users", lambda *a: {**staff_row(a[0]), "inserted": True})
    with client(conn, cis) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.token(sub="u-1", cis_role="admin")))
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin" and body["active"] is True
    assert body["permissions"] == sorted(PERMISSIONS)
    insert = conn.sql("INSERT INTO booking.staff_users")[0]
    assert insert[:6] == ("u-1", "staff@example.test", "Staff One", "admin", True, "admin")
    audit = conn.sql("INSERT INTO booking.audit_logs")
    assert len(audit) == 1 and audit[0][0] == "system" and audit[0][3] == "staff.provisioned"


@pytest.mark.parametrize(("cis_role", "role"), [("reviewer", "sales"), ("content", "content_creator")])
def test_other_cis_roles_are_provisioned_inactive_and_get_403(cis_role: str, role: str) -> None:
    cis = LocalCis()
    conn = ScriptedConn().on(
        "INSERT INTO booking.staff_users",
        lambda *a: {**staff_row(a[0], role=a[3], active=a[4], cis_role=a[5]), "inserted": True},
    )
    with client(conn, cis) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.token(cis_role=cis_role)))
    assert r.status_code == 403
    assert r.json()["detail"] == "staff_inactive"
    insert = conn.sql("INSERT INTO booking.staff_users")[0]
    assert insert[3:5] == (role, False)


def test_returning_user_without_changes_is_read_back_and_not_audited() -> None:
    cis = LocalCis()
    conn = (
        ScriptedConn()
        .on("INSERT INTO booking.staff_users", None)
        .on("SELECT cis_user_id", staff_row("u-1", role="sales"))
    )
    with client(conn, cis) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.token(sub="u-1", cis_role="reviewer")))
    assert r.status_code == 200
    assert r.json()["role"] == "sales"
    assert r.json()["permissions"] == [
        "customer.read",
        "customer.write",
        "staff.directory",
        "trip_case.assign",
        "trip_case.read",
        "trip_case.write",
        "version.write",
    ]
    assert conn.sql("INSERT INTO booking.audit_logs") == []


def test_permission_denied_is_403() -> None:
    cis = LocalCis()
    conn = ScriptedConn().on(
        "INSERT INTO booking.staff_users", {**staff_row("u-2", role="content_creator"), "inserted": False}
    )
    with client(conn, cis) as c:
        r = c.get("/booking/trip-cases", headers=auth(cis.token(sub="u-2", cis_role="content")))
    assert r.status_code == 403
    assert r.json()["detail"] == "forbidden"


def test_require_rejects_unknown_permission_names() -> None:
    with pytest.raises(ValueError, match="unknown permission"):
        require("trip_case.delete")


def test_inactive_staff_has_no_permissions() -> None:
    assert Staff.from_row(staff_row(active=False)).permissions == []


@pytest.mark.asyncio
async def test_provision_passes_claims_through() -> None:
    conn = ScriptedConn().on("INSERT INTO booking.staff_users", {**staff_row("u-9"), "inserted": False})
    staff = await provision(conn, StaffClaims("u-9", None, None, "admin"), "admin")
    assert staff.cis_user_id == "u-9"
    assert conn.sql("INSERT INTO booking.staff_users")[0][:3] == ("u-9", None, None)


# ── Admin endpoints ──────────────────────────────────────────────────────────


def admin_conn() -> ScriptedConn:
    return ScriptedConn().on("INSERT INTO booking.staff_users", {**staff_row("u-1"), "inserted": False})


def test_admin_lists_staff() -> None:
    cis = LocalCis()
    conn = admin_conn().on("ORDER BY active DESC", [staff_row("u-1"), staff_row("u-2", role="sales", active=False)])
    with client(conn, cis) as c:
        r = c.get("/booking/staff?active=false", headers=auth(cis.token(sub="u-1")))
    assert r.status_code == 200
    assert [s["cis_user_id"] for s in r.json()["items"]] == ["u-1", "u-2"]
    assert conn.sql("ORDER BY active DESC")[0] == (False,)


def test_admin_activates_a_user() -> None:
    cis = LocalCis()
    conn = (
        admin_conn()
        .on("FOR UPDATE", staff_row("u-2", role="sales", active=False))
        .on("UPDATE booking.staff_users", staff_row("u-2", role="contracting", active=True))
    )
    with client(conn, cis) as c:
        r = c.patch(
            "/booking/staff/u-2", json={"active": True, "role": "contracting"}, headers=auth(cis.token(sub="u-1"))
        )
    assert r.status_code == 200
    assert r.json()["active"] is True and r.json()["role"] == "contracting"
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[3] == "staff.updated" and audit[2] == "u-1"


def test_admin_patch_unknown_user_is_404() -> None:
    cis = LocalCis()
    with client(admin_conn(), cis) as c:
        r = c.patch("/booking/staff/nobody", json={"active": True}, headers=auth(cis.token(sub="u-1")))
    assert r.status_code == 404
    assert r.json()["detail"] == "staff_not_found"


def test_non_admin_cannot_use_admin_endpoints() -> None:
    cis = LocalCis()
    conn = ScriptedConn().on("INSERT INTO booking.staff_users", {**staff_row("u-3", role="sales"), "inserted": False})
    with client(conn, cis) as c:
        r = c.get("/booking/staff", headers=auth(cis.token(sub="u-3", cis_role="reviewer")))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_update_staff_rules() -> None:
    me = Staff.from_row(staff_row("u-1"))
    with pytest.raises(Invalid):
        await admin.update_staff(ScriptedConn(), me, "u-2", active=None, role="owner")
    conn = ScriptedConn().on("FOR UPDATE", staff_row("u-1"))
    with pytest.raises(Conflict):
        await admin.update_staff(conn, me, "u-1", active=False, role=None)
    with pytest.raises(Conflict):
        await admin.update_staff(conn, me, "u-1", active=None, role="sales")
    unchanged = await admin.update_staff(conn, me, "u-1", active=True, role="admin")
    assert unchanged["cis_user_id"] == "u-1"
    assert conn.sql("UPDATE booking.staff_users") == []
    with pytest.raises(NotFound):
        await admin.update_staff(ScriptedConn(), me, "u-404", active=True, role=None)
