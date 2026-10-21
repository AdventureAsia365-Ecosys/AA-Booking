"""AA-911 decision D-1 (root ADR 0006a A11): a token from the Preview-only ``booking-staff-preview`` client is a
read-only session whatever ``booking-*`` group the user holds. Writes are 403 ``preview_read_only`` before any DB
access; reads are limited to :data:`app.auth.staff.PREVIEW_PERMISSIONS`. Local RSA keys + fake JWKS, no network."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import identity_config
from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS, Staff
from app.db import Database
from app.main import create_app
from app.settings import Settings, SettingsError, load_settings
from tests.staff_tokens import (
    BOOKING_STAFF_CLIENT,
    BOOKING_STAFF_PREVIEW_CLIENT,
    BREAKGLASS_CLIENT,
    WORKFORCE_SUB,
    LocalStaff,
)
from tests.unit.fakes import ScriptedConn

DSN = "postgresql://booking:x@db.test/aa"
IDENTITY_SELECT = "cognito_iss = $1 AND cognito_sub = $2"
COG_KEY = f"cog:{WORKFORCE_SUB}"


def cognito_row(role: str = "admin") -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "cis_user_id": COG_KEY,
        "email": "thu@adventure.asia",
        "name": "thu",
        "role": role,
        "active": True,
        "cis_role": "admin",
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,  # recent: no last_seen write on a read
        "cognito_sub": WORKFORCE_SUB,
    }


class FakeDatabase(Database):
    def __init__(self, settings: Settings, conn: ScriptedConn) -> None:
        super().__init__(settings)
        self.conn = conn

    @property
    def configured(self) -> bool:
        return True

    async def pool(self):  # type: ignore[override]
        return self.conn


def client(staff: LocalStaff, conn: ScriptedConn, settings: Settings | None = None) -> TestClient:
    """The app with the SAME settings the verifier is built from (as create_app does in production)."""
    settings = settings or replace(staff.settings(), database_url=DSN)
    verifier = identity_config.build_staff_verifier(settings, staff.fetch)
    return TestClient(create_app(settings, FakeDatabase(settings, conn), verifier))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def preview_admin_token(staff: LocalStaff) -> str:
    return staff.workforce.token(["booking-admin", "cis-admin"], client_id=BOOKING_STAFF_PREVIEW_CLIENT)


# ── /booking/staff/me: real role shown, capped permissions ───────────────────


def test_preview_client_with_an_admin_group_is_a_read_only_session() -> None:
    staff = LocalStaff()
    conn = ScriptedConn().on(IDENTITY_SELECT, cognito_row("admin"))
    with client(staff, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(preview_admin_token(staff)))
    assert r.status_code == 200
    body = r.json()
    assert body["preview_viewer"] is True
    assert body["role"] == "admin"  # the stored role is the user's; the SESSION is capped
    granted = set(body["permissions"])
    assert granted == {p for p in PREVIEW_PERMISSIONS if "admin" in PERMISSIONS[p]}
    assert not granted & {"staff.admin", "audit.read", "retention.admin", "trip_case.write", "pricing.write"}


def test_same_user_on_the_production_client_keeps_the_full_role() -> None:
    staff = LocalStaff()
    conn = ScriptedConn().on(IDENTITY_SELECT, cognito_row("admin"))
    with client(staff, conn) as c:
        token = staff.workforce.token(["booking-admin"], client_id=BOOKING_STAFF_CLIENT)
        r = c.get("/booking/staff/me", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["preview_viewer"] is False
    assert "staff.admin" in r.json()["permissions"] and "trip_case.write" in r.json()["permissions"]


# ── Writes: 403 preview_read_only, nothing read or written ───────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/booking/staff/me/preferences", {"notify": {}}),
        ("PATCH", f"/booking/staff/{COG_KEY}", {"active": False}),
        ("POST", "/booking/trip-cases", {"title": "x"}),
        ("POST", "/booking/customers", {"name": "x"}),
        ("POST", "/booking/notifications/read", {"all": True}),
        ("POST", "/booking/pricing/rules", {}),
        ("POST", "/booking/retention/runs", {}),
        ("DELETE", "/booking/customers/c-1", None),
        ("DELETE", "/booking/rates/00000000-0000-4000-8000-000000000001", None),
    ],
)
def test_preview_client_writes_are_403_before_any_db_access(method: str, path: str, body: Any) -> None:
    staff, conn = LocalStaff(), ScriptedConn()
    with client(staff, conn) as c:
        r = c.request(method, path, json=body, headers=auth(preview_admin_token(staff)))
    assert r.status_code == 403
    assert r.json()["detail"] == "preview_read_only"
    assert conn.calls == []


def test_preview_client_admin_only_reads_are_403() -> None:
    staff = LocalStaff()
    conn = ScriptedConn().on(IDENTITY_SELECT, cognito_row("admin"))
    with client(staff, conn) as c:
        r = c.get("/booking/audit-logs", headers=auth(preview_admin_token(staff)))
    assert r.status_code == 403
    assert r.json()["detail"] == "forbidden"


def test_production_client_write_is_not_refused_by_the_preview_rule() -> None:
    """Control: the same admin on booking-staff reaches the route (it then fails for its own reasons, not 403)."""
    staff = LocalStaff()
    conn = ScriptedConn().on(IDENTITY_SELECT, cognito_row("admin"))
    with client(staff, conn) as c:
        token = staff.workforce.token(["booking-admin"], client_id=BOOKING_STAFF_CLIENT)
        r = c.put("/booking/staff/me/preferences", json={"notify": {}}, headers=auth(token))
    assert r.status_code != 403 or r.json().get("detail") != "preview_read_only"


# ── Configuration: no preview id = preview tokens refused, never uncapped ────


def test_preview_token_is_401_when_no_preview_client_is_configured() -> None:
    staff, conn = LocalStaff(), ScriptedConn()
    settings = replace(staff.settings(), database_url=DSN, cognito_booking_staff_preview_client_id="")
    with client(staff, conn, settings) as c:
        r = c.get("/booking/staff/me", headers=auth(preview_admin_token(staff)))
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_token"
    assert conn.calls == []


def test_staff_can_is_capped_for_a_preview_session() -> None:
    admin = Staff(cis_user_id="u", email=None, name=None, role="admin", active=True, cis_role=None)
    capped = replace(admin, preview=True)
    assert admin.can("trip_case.write") and admin.can("staff.admin")
    assert not capped.can("trip_case.write") and not capped.can("staff.admin") and not capped.can("audit.read")
    assert capped.can("trip_case.read") and capped.can("catalog.read")
    assert set(capped.permissions) <= PREVIEW_PERMISSIONS
    assert all(p.endswith(".read") or p == "staff.directory" for p in PREVIEW_PERMISSIONS)


def test_default_settings_accept_no_preview_client() -> None:
    settings = load_settings({})
    assert identity_config.DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID == "" or settings.cognito_booking_staff_preview_client_id
    (workforce, *_) = identity_config.booking_staff_issuers(settings)
    assert identity_config.preview_client_ids(settings) <= workforce.client_ids
    assert not identity_config.is_preview_client(settings, settings.cognito_booking_staff_client_id)
    assert not identity_config.is_preview_client(settings, None)


def test_preview_client_env_override_is_accepted_and_capped() -> None:
    settings = load_settings({"COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID": " previewclient123 "})
    (workforce, *_) = identity_config.booking_staff_issuers(settings)
    assert "previewclient123" in workforce.client_ids
    assert identity_config.is_preview_client(settings, "previewclient123")


@pytest.mark.parametrize(
    "env",
    [
        {"COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID": "not a client id"},
        {"COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID": identity_config.DEV_BOOKING_STAFF_CLIENT_ID},
        {"COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID": "abc123", "COGNITO_BREAKGLASS_CLIENT_ID": "abc123"},
    ],
    ids=["not-alnum", "same-as-booking-staff", "same-as-breakglass"],
)
def test_bad_preview_client_ids_are_refused(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        load_settings(env)


def test_breakglass_and_production_clients_are_never_preview() -> None:
    settings = LocalStaff().settings()
    assert identity_config.is_preview_client(settings, BOOKING_STAFF_PREVIEW_CLIENT)
    assert not identity_config.is_preview_client(settings, BOOKING_STAFF_CLIENT)
    assert not identity_config.is_preview_client(settings, BREAKGLASS_CLIENT)
