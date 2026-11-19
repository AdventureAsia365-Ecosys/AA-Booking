"""AA-1111 slice S2 (root ADR 0006c, bk-061) — app/auth/admin_mfa.py through the real staff dependency.

A WORKFORCE booking-admin session whose access token's amr lacks "otp" is refused 403 mfa_required only while the
pricing policy parameter staff_admin_mfa_required is on; off it is served and logged once (would_refuse). Other roles,
the Preview session and the Dev-only E2E identities are never checked. Local RSA keys, scripted DB, no network."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from app.auth import admin_mfa
from app.pricing import policy_params
from tests.staff_tokens import BOOKING_STAFF_PREVIEW_CLIENT, LocalCis
from tests.unit.fakes import POLICY_FRAGMENT, ScriptedConn, policy_rows
from tests.unit.test_aa911_preview_cap import IDENTITY_SELECT, auth, client, cognito_row

TOTP_AMR = ["pwd", "otp", "mfa"]
EMAIL_CODE_AMR = ["email_otp"]


@pytest.fixture(autouse=True)
def fresh() -> Iterator[None]:
    admin_mfa.reset()
    yield
    admin_mfa.reset()


def _conn(role: str = "admin", *, switch: str = "false") -> ScriptedConn:
    return (
        ScriptedConn()
        .on(IDENTITY_SELECT, cognito_row(role))
        .on(POLICY_FRAGMENT, lambda *_: policy_rows(staff_admin_mfa_required=switch))
    )


def _me(cis: LocalCis, conn: ScriptedConn, token: str) -> Any:
    with client(cis, conn) as c:
        return c.get("/booking/staff/me", headers=auth(token))


def test_the_parameter_is_a_boolean_approved_by_the_administrator() -> None:
    p = policy_params.PARAMS["staff_admin_mfa_required"]
    assert (p.type, p.group, p.section, p.used_by) == ("boolean", "admin", "security", "app.auth.admin_mfa")
    assert "security" in policy_params.SECTIONS


@pytest.mark.parametrize(
    "value, expected",
    [(TOTP_AMR, ("pwd", "otp", "mfa")), ("otp", ("otp",)), ([1, "otp"], ("otp",)), ({"otp": True}, ())],
)
def test_token_amr_reads_the_claim_shapes(value: Any, expected: tuple[str, ...]) -> None:
    cis = LocalCis()
    assert admin_mfa.token_amr(cis.workforce.token(["booking-admin"], amr=value)) == expected


def test_token_amr_absent_or_malformed_is_empty() -> None:
    cis = LocalCis()
    assert admin_mfa.token_amr(cis.workforce.token(["booking-admin"])) == ()
    assert admin_mfa.token_amr("not-a-jwt") == ()
    assert admin_mfa.token_amr(None) == ()


def test_only_otp_counts_as_mfa() -> None:
    assert admin_mfa.mfa_used(("pwd", "otp", "mfa"))
    assert not admin_mfa.mfa_used(("pwd", "email_otp", "mfa"))
    assert not admin_mfa.mfa_used(())


def test_switch_on_refuses_an_admin_without_the_code_before_the_row_sync() -> None:
    cis, conn = LocalCis(), _conn(switch="true")
    r = _me(cis, conn, cis.workforce.token(["booking-admin"], amr=EMAIL_CODE_AMR))
    assert r.status_code == 403 and r.json()["detail"] == "mfa_required"
    assert conn.sql(IDENTITY_SELECT) == []  # refused before the staff row is read or written


def test_switch_on_refuses_an_admin_token_with_no_amr() -> None:
    cis, conn = LocalCis(), _conn(switch="true")
    r = _me(cis, conn, cis.workforce.token(["booking-admin"]))
    assert r.status_code == 403 and r.json()["detail"] == "mfa_required"


def test_switch_on_accepts_an_admin_who_used_the_code() -> None:
    cis, conn = LocalCis(), _conn(switch="true")
    r = _me(cis, conn, cis.workforce.token(["booking-admin"], amr=TOTP_AMR))
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert conn.sql(POLICY_FRAGMENT) == []  # the code was used: the switch is not even read


def test_switch_off_serves_and_logs_would_refuse_once(caplog: pytest.LogCaptureFixture) -> None:
    cis, conn = LocalCis(), _conn()
    token = cis.workforce.token(["booking-admin"], amr=EMAIL_CODE_AMR)
    with caplog.at_level(logging.INFO, logger="app.auth.admin_mfa"):
        assert _me(cis, conn, token).status_code == 200
        assert _me(cis, conn, token).status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "app.auth.admin_mfa"]
    assert len(lines) == 1 and "outcome=would_refuse" in lines[0] and "email_otp" in lines[0]
    assert len(conn.sql(POLICY_FRAGMENT)) == 1  # the switch read is cached


def test_a_missing_parameter_reads_off(caplog: pytest.LogCaptureFixture) -> None:
    cis = LocalCis()
    rows = [r for r in policy_rows() if r["param_key"] != "staff_admin_mfa_required"]
    conn = ScriptedConn().on(IDENTITY_SELECT, cognito_row("admin")).on(POLICY_FRAGMENT, lambda *_: rows)
    with caplog.at_level(logging.WARNING, logger="app.auth.admin_mfa"):
        r = _me(cis, conn, cis.workforce.token(["booking-admin"], amr=EMAIL_CODE_AMR))
    assert r.status_code == 200
    assert any("staff_admin_mfa_switch_unavailable" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize("group, role", [("booking-sales", "sales"), ("booking-contracting", "contracting")])
def test_switch_on_leaves_other_roles_alone(group: str, role: str) -> None:
    cis, conn = LocalCis(), _conn(role, switch="true")
    r = _me(cis, conn, cis.workforce.token([group], amr=EMAIL_CODE_AMR))
    assert r.status_code == 200 and r.json()["role"] == role
    assert conn.sql(POLICY_FRAGMENT) == []


def test_switch_on_leaves_the_preview_session_alone() -> None:
    cis, conn = LocalCis(), _conn(switch="true")
    token = cis.workforce.token(["booking-admin"], client_id=BOOKING_STAFF_PREVIEW_CLIENT, amr=EMAIL_CODE_AMR)
    r = _me(cis, conn, token)
    assert r.status_code == 200 and r.json()["preview_viewer"] is True
