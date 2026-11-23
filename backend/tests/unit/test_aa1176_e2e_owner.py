"""AA-1176 (S288, root ADR 0006a A10h): the Dev-only E2E owner in AA-Booking (:mod:`app.auth.e2e_owner`).

e2e-owner@e2e-mail.lumiguides.it.com with exactly cis-admin + booking-admin is a plain administrator session, NOT
read-only and not an E2E writer; refused outside ENVIRONMENT dev (reads too) and with any other group set before any DB
access; exempt from the administrator MFA switch; never self-approves; named on its first-sign-in staff row and audit.
Local RSA keys + fake JWKS, scripted pool, no network, no DB.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException

from app.auth import admin_mfa, e2e_owner, e2e_staff, self_approval
from app.auth.staff import Staff
from tests.staff_tokens import LocalCis
from tests.unit.fakes import POLICY_FRAGMENT, ScriptedConn, policy_rows
from tests.unit.test_aa911_preview_cap import IDENTITY_SELECT, auth, client, cognito_row

OWNER = "e2e-owner@e2e-mail.lumiguides.it.com"
GROUPS = ["booking-admin", "cis-admin"]
EMAIL_CODE_AMR = ["email_otp"]


@pytest.fixture(autouse=True)
def dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    admin_mfa.reset()


def _token(cis: LocalCis, groups: list[str] | None = None, email: str = OWNER, **extra: Any) -> str:
    return cis.workforce.token(GROUPS if groups is None else groups, email=email, **extra)


def _row() -> dict[str, Any]:
    return {**cognito_row("admin"), "email": OWNER, "name": e2e_owner.E2E_OWNER_NAME, "cis_role": "admin"}


def _conn(*, switch: str = "false") -> ScriptedConn:
    return (
        ScriptedConn()
        .on(IDENTITY_SELECT, _row())
        .on(POLICY_FRAGMENT, lambda *_: policy_rows(staff_admin_mfa_required=switch))
    )


# ── the module ──────────────────────────────────────────────────────────────────────────────────


def test_the_constants() -> None:
    assert e2e_owner.E2E_OWNER_EMAIL == OWNER
    assert e2e_owner.E2E_OWNER_GROUPS == frozenset({"cis-admin", "booking-admin"})
    assert "crm-platform-admin" not in e2e_owner.E2E_OWNER_GROUPS
    # Not an E2E writer: no allow-list, no synthetic-record rule, no route-scoped grant.
    assert OWNER not in e2e_staff.E2E_WRITERS
    assert e2e_staff.writer_for(OWNER) is None


@pytest.mark.parametrize(
    ("email", "exact", "like"),
    [
        (OWNER, True, True),
        (f"  {OWNER.upper()} ", True, True),
        (OWNER + "​", False, False),
        ("e2e-ᴏwner@e2e-mail.lumiguides.it.com", False, False),
        ("e2e-owner+x@e2e-mail.lumiguides.it.com", False, False),
        ("e2e-owner@x.e2e-mail.lumiguides.it.com", False, False),
        ("e2e-owner@e2e-mail.lumiguides.it.com.", False, False),
        ("thu@adventure.asia", False, False),
        (None, False, False),
    ],
)
def test_the_address_helpers(email: Any, exact: bool, like: bool) -> None:
    assert e2e_owner.is_e2e_owner(email) is exact
    assert e2e_owner.is_e2e_owner_like(email) is like


def test_check_owner_session_is_a_no_op_for_everyone_else() -> None:
    assert e2e_owner.check_session("thu@adventure.asia", ["booking-admin"]) is False
    assert e2e_owner.check_session(None, []) is False


@pytest.mark.parametrize("groups", [["booking-admin"], ["cis-admin"], [*GROUPS, "crm-platform-admin"], []])
def test_check_owner_session_refuses_other_group_sets(groups: list[str]) -> None:
    with pytest.raises(HTTPException) as exc:
        e2e_owner.check_session(OWNER, groups)
    assert (exc.value.status_code, exc.value.detail) == (403, "e2e_scope")


# ── the real app ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("env", [None, "prod", "test", "Dev", "dev ", "local"])
@pytest.mark.parametrize(("method", "path"), [("GET", "/booking/staff/me"), ("POST", "/booking/trip-cases")])
def test_outside_dev_every_request_is_refused_before_any_db_access(
    monkeypatch: pytest.MonkeyPatch, env: str | None, method: str, path: str
) -> None:
    if env is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", env)
    cis, conn = LocalCis(), ScriptedConn()
    with client(cis, conn) as c:
        r = c.request(method, path, json={}, headers=auth(_token(cis)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_not_allowed_here")
    assert conn.calls == []


@pytest.mark.parametrize(
    "groups",
    [
        ["booking-admin"],
        ["cis-admin", "booking-sales"],
        [*GROUPS, "crm-platform-admin"],
        [*GROUPS, "booking-finance"],
        [*GROUPS, "booking-custom"],
    ],
)
def test_any_other_group_set_is_refused_before_any_db_access(groups: list[str]) -> None:
    cis, conn = LocalCis(), ScriptedConn()
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis, groups)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_scope")
    assert conn.calls == []


def test_on_dev_it_is_an_administrator() -> None:
    cis, conn = LocalCis(), _conn()
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis)))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"


def test_a_write_is_not_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not read-only and not an E2E writer: the write reaches the route (its own rules answer from here on).
    cis, conn = LocalCis(), _conn()
    with client(cis, conn) as c:
        r = c.put("/booking/staff/me/preferences", json={"notify": {}}, headers=auth(_token(cis)))
    assert r.json().get("detail") not in ("e2e_read_only", "e2e_scope", "e2e_not_allowed_here", "e2e_synthetic_only")
    assert conn.sql(IDENTITY_SELECT)  # the staff row was read: the session passed the guards


def test_the_mfa_switch_on_does_not_refuse_it() -> None:
    cis, conn = LocalCis(), _conn(switch="true")
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis, amr=EMAIL_CODE_AMR)))
    assert r.status_code == 200, r.text


def test_the_mfa_switch_on_still_refuses_a_real_administrator() -> None:
    cis = LocalCis()
    conn = (
        ScriptedConn()
        .on(IDENTITY_SELECT, cognito_row("admin"))
        .on(POLICY_FRAGMENT, lambda *_: policy_rows(staff_admin_mfa_required="true"))
    )
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.workforce.token(GROUPS, amr=EMAIL_CODE_AMR)))
    assert (r.status_code, r.json()["detail"]) == (403, "mfa_required")


def test_the_first_sign_in_names_it_on_the_row_and_the_audit() -> None:
    cis = LocalCis()
    row = _row()
    conn = (
        ScriptedConn()
        .on("INSERT INTO booking.staff_users", row)
        .on(IDENTITY_SELECT, None)
        .on(POLICY_FRAGMENT, lambda *_: policy_rows())
    )
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(_token(cis)))
    assert r.status_code == 200, r.text
    (insert,) = conn.sql("INSERT INTO booking.staff_users")
    assert insert[1] == OWNER and insert[2] == e2e_owner.E2E_OWNER_NAME and insert[3] == "admin"
    audits = [args for flat, args in conn.calls if "audit" in flat.lower() and "staff.provisioned" in str(args)]
    assert audits and any("e2e_owner" in str(a) and "Dev-only E2E owner" in str(a) for a in audits)


# ── self-approval ───────────────────────────────────────────────────────────────────────────────


def _staff(email: str) -> Staff:
    base = Staff.from_row({**cognito_row("admin"), "cognito_sub": "sub-1"})
    return replace(base, email=email)


def test_the_e2e_owner_never_self_approves() -> None:
    assert self_approval.eligible_session(_staff("thu@adventure.asia")) is True
    assert self_approval.eligible_session(_staff(OWNER)) is False
    assert self_approval.eligible_session(_staff(OWNER.upper())) is False
