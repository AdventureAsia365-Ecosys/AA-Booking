"""Business-owner self-approval (AA-1158 part 1; tech lead S285 Q4 / Q5; bk-064).

With three people, strict maker != checker blocks the business owner's own work. Every approval that Q5 covers asks
:func:`check` whether its caller is one of the work's makers and, if so, whether the caller may approve it anyway:

- not a maker: an ordinary second-person approval (returns ``False``: the row's ``owner_self_approved`` flag);
- a maker who is the business owner, while the policy switch is on: a self-approval (returns ``True``: the flag);
- any other maker: 403 ``maker_checker``.

The business owner is the identity whose CIS record carries ``is_business_owner`` (CIS 462, read uncached through
:func:`app.auth.custom_roles.is_business_owner`, fail closed). It is decided here, server-side, on every write, never
taken from a request field; the database checks the same marker again for any row whose flag is set
(``booking.owner_self_approval_guard``). A Preview session, a Dev-only E2E writer and the E2E test user never
self-approve, nor does the Dev-only E2E owner (A10h, AA-1176). Self-approval only relaxes maker != checker: each
approval still needs its own capability.

The switches are pricing policy parameters (Pricing > Policy, section security, approved by the administrator group):
``owner_self_approval_allowed`` for approvals and ``owner_payout_after_approval_allowed`` for a payout of a bill the
business owner approved. A switch with no approved value in force reads as off.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import asyncpg

from app.auth import custom_roles, e2e_owner
from app.auth.staff import E2E_TEST_STAFF_EMAIL, Staff
from app.errors import Forbidden
from app.pricing import policy_values

log = logging.getLogger(__name__)

APPROVE = "approve"
PAYOUT = "payout"
SWITCHES = {APPROVE: "owner_self_approval_allowed", PAYOUT: "owner_payout_after_approval_allowed"}
MAKER_CHECKER = "maker_checker"


def eligible_session(staff: Staff) -> bool:
    """A session that may ever self-approve: active, signed in with a WORKFORCE identity, and none of the capped
    sessions (Preview, Dev-only E2E writer, the E2E test user)."""
    if not staff.active or not staff.cognito_sub or staff.preview or staff.e2e_writer or staff.e2e_grants:
        return False
    if e2e_owner.is_e2e_owner_like(staff.email):  # AA-1176 A10h: the E2E owner is never the business owner
        return False
    return not (isinstance(staff.email, str) and staff.email.strip().lower() == E2E_TEST_STAFF_EMAIL)


async def is_business_owner(conn: asyncpg.Connection, staff: Staff) -> bool:
    """The caller holds the CIS business-owner marker (uncached; False on any doubt)."""
    if not eligible_session(staff):
        return False
    return await custom_roles.is_business_owner(conn, staff.cognito_sub)


async def switch_on(conn: asyncpg.Connection, kind: str) -> bool:
    """The policy switch of ``kind`` in force today; no approved value, or a failed read, reads as off."""
    try:
        async with custom_roles.isolated(conn):  # a read error never aborts the caller's transaction
            policy = await policy_values.load_today(conn)
        return policy.flag(SWITCHES[kind])
    except (policy_values.PolicyUnavailable, asyncpg.PostgresError, OSError):
        log.warning("owner_self_approval_switch_unavailable kind=%s (read as off)", kind)
        return False


async def may_self_approve(conn: asyncpg.Connection, staff: Staff, kind: str = APPROVE) -> bool:
    """The caller may approve their own work of ``kind`` now (business owner and the switch on)."""
    return await is_business_owner(conn, staff) and await switch_on(conn, kind)


async def check(
    conn: asyncpg.Connection,
    staff: Staff,
    makers: Iterable[str | None],
    *,
    message: str,
    kind: str = APPROVE,
    allow_self: bool = True,
) -> bool:
    """The ``owner_self_approved`` value of this approval: False when the caller made none of the work, True for a
    business-owner self-approval; else 403 ``maker_checker`` with ``message``. ``allow_self=False``: a decision that
    always takes two people (a rejection, a withdrawal), the strict maker-checker."""
    if staff.cis_user_id not in {m for m in makers if m}:
        return False
    if allow_self and await may_self_approve(conn, staff, kind):
        log.info("owner_self_approval kind=%s staff=%s", kind, staff.cis_user_id)
        return True
    raise Forbidden(MAKER_CHECKER, message)


def flag(flagged: bool) -> dict[str, bool]:
    """The audit ``after`` fields of a self-approval (``owner_self_approved``; the audit filter reads it)."""
    return {"owner_self_approved": True} if flagged else {}
