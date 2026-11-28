"""AA-1054 (S297; tech lead decision S295 sec-Q11; root ADR 0006a): the pilot fence in AA-Booking
(:mod:`app.auth.pilot_fence`).

While the CIS setting cis.platform.pilot_e2e_fence is on (read through catalog.v_pilot_fence_v1, CIS 496), every
WORKFORCE token whose address is of the E2E mailbox domain is refused 403 e2e_pilot_fence before its first database
read; when the view cannot be read it is refused 403 e2e_pilot_fence_unknown (fail closed). Off, nothing changes. Real
staff never read the view. Local RSA keys + fake JWKS, scripted pool, no network, no DB.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest
from fastapi import HTTPException

from app.auth import admin_mfa, e2e_owner, e2e_staff, pilot_fence
from app.auth.staff import E2E_TEST_STAFF_EMAIL
from tests.staff_tokens import LocalCis
from tests.unit.fakes import PILOT_FENCE_FRAGMENT, ScriptedConn
from tests.unit.test_aa911_preview_cap import IDENTITY_SELECT, auth, client, cognito_row

OWNER = e2e_owner.E2E_OWNER_EMAIL
OWNER_GROUPS = ["booking-admin", "cis-admin"]
STAFF = "lan.tran@adventure.asia"


@pytest.fixture(autouse=True)
def dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    admin_mfa.reset()


def _conn(fence: Any, email: str = OWNER, role: str = "admin") -> ScriptedConn:
    row = {**cognito_row(role), "email": email, "name": "zz", "cis_role": "admin"}
    return ScriptedConn().on(IDENTITY_SELECT, row).on(PILOT_FENCE_FRAGMENT, fence)


def _fence_reads(conn: ScriptedConn) -> int:
    return len(conn.fence_calls)


# ── the module ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("email", "e2e"),
    [
        (OWNER, True),
        (E2E_TEST_STAFF_EMAIL, True),
        (e2e_staff.E2E_BOOKING_STAFF_EMAIL, True),
        (e2e_staff.E2E_PRICING_CHECKER_EMAIL, True),
        ("  E2E-Booking@E2E-Mail.Lumiguides.IT.com ", True),
        ("someone-new@e2e-mail.lumiguides.it.com", True),
        ("K2e@e2e-mail.lumiguides.it.com", True),
        (STAFF, False),
        ("e2e@adventure.asia", False),
        ("e2e@x.e2e-mail.lumiguides.it.com", False),
        ("e2e@e2e-mail.lumiguides.it.com.evil", False),
        ("", False),
        (None, False),
    ],
)
def test_is_e2e_identity(email: Any, e2e: bool) -> None:
    assert pilot_fence.is_e2e_identity(email) is e2e


def test_every_named_booking_e2e_address_is_fenced() -> None:
    named = {v for k, v in vars(e2e_staff).items() if k.isupper() and k.endswith("_EMAIL") and isinstance(v, str)}
    named |= {E2E_TEST_STAFF_EMAIL, OWNER}
    assert len(named) >= 8
    assert all(pilot_fence.is_e2e_identity(a) for a in named)


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        (False, pilot_fence.OFF),
        (True, pilot_fence.ON),
        (None, pilot_fence.UNKNOWN),  # no row: CIS 495's definition is missing
        ("true", pilot_fence.UNKNOWN),
        (asyncpg.exceptions.UndefinedTableError("relation does not exist"), pilot_fence.UNKNOWN),
        (asyncpg.exceptions.InsufficientPrivilegeError("permission denied"), pilot_fence.UNKNOWN),
        (OSError("connection reset"), pilot_fence.UNKNOWN),
    ],
)
@pytest.mark.asyncio
async def test_read_state(answer: Any, state: str) -> None:
    assert await pilot_fence.read_state(ScriptedConn().on(PILOT_FENCE_FRAGMENT, answer)) == state


@pytest.mark.asyncio
async def test_the_answer_is_cached_per_process() -> None:
    conn = ScriptedConn().on(PILOT_FENCE_FRAGMENT, True)
    assert await pilot_fence.state(conn) == pilot_fence.ON
    assert await pilot_fence.state(conn) == pilot_fence.ON
    assert _fence_reads(conn) == 1
    pilot_fence.reset()
    assert await pilot_fence.state(ScriptedConn().on(PILOT_FENCE_FRAGMENT, False)) == pilot_fence.OFF


@pytest.mark.parametrize(
    ("answer", "detail"),
    [(True, "e2e_pilot_fence"), (None, "e2e_pilot_fence_unknown")],
)
@pytest.mark.asyncio
async def test_check_refuses_an_e2e_identity(answer: Any, detail: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await pilot_fence.check(ScriptedConn().on(PILOT_FENCE_FRAGMENT, answer), OWNER)
    assert (exc.value.status_code, exc.value.detail) == (403, detail)


@pytest.mark.asyncio
async def test_check_never_reads_for_real_staff() -> None:
    conn = ScriptedConn().on(PILOT_FENCE_FRAGMENT, True)
    await pilot_fence.check(conn, STAFF)
    assert conn.calls == [] and conn.fence_calls == []


# ── the real app, both ways ──────────────────────────────────────────────────────────────────────


def test_fence_off_the_e2e_owner_works_as_before() -> None:
    cis, conn = LocalCis(), _conn(False)
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.workforce.token(OWNER_GROUPS, email=OWNER)))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert _fence_reads(conn) == 1


@pytest.mark.parametrize(
    ("answer", "detail"),
    [(True, "e2e_pilot_fence"), (None, "e2e_pilot_fence_unknown"), (OSError("down"), "e2e_pilot_fence_unknown")],
)
def test_fence_on_or_unreadable_the_e2e_owner_is_refused_before_any_other_read(answer: Any, detail: str) -> None:
    cis, conn = LocalCis(), _conn(answer)
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.workforce.token(OWNER_GROUPS, email=OWNER)))
    assert (r.status_code, r.json()["detail"]) == (403, detail)
    assert conn.calls == [] and _fence_reads(conn) == 1


def test_fence_on_the_read_only_e2e_admin_read_is_refused() -> None:
    cis, conn = LocalCis(), _conn(True, email=E2E_TEST_STAFF_EMAIL)
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.workforce.token(["booking-admin"], email=E2E_TEST_STAFF_EMAIL)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_pilot_fence")


def test_fence_on_an_e2e_writer_is_refused() -> None:
    email = e2e_staff.E2E_BOOKING_STAFF_EMAIL
    cis, conn = LocalCis(), _conn(True, email=email, role="sales")
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.workforce.token(["booking-sales"], email=email)))
    assert (r.status_code, r.json()["detail"]) == (403, "e2e_pilot_fence")


def test_fence_on_real_staff_work_as_usual_and_never_read_it() -> None:
    cis, conn = LocalCis(), _conn(True, email=STAFF)
    with client(cis, conn) as c:
        r = c.get("/booking/staff/me", headers=auth(cis.workforce.token(OWNER_GROUPS, email=STAFF)))
    assert r.status_code == 200, r.text
    assert _fence_reads(conn) == 0
