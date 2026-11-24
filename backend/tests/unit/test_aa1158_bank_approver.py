"""AA-1158 part 2 follow-up (tech lead S289): who approves a supplier bank account change, without a database.

Only the business owner (CIS ``is_business_owner``) approves, the tech lead (the platform owner marker ``is_owner``) as
the fallback (:func:`app.suppliers.bank_accounts.approver_kind`); any other administrator, a capped session (no
identity, Preview, Dev-only E2E writer, the E2E owner) gets no Approve action, an empty My work queue and 403
``bank_approver_not_allowed``, and may still reject; the audit row records ``approved_as``. The database rule is
tests/integration/test_bk071_schema.py, the HTTP flow tests/integration/test_aa1158_supplier_bank_api.py. All names are
invented."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.auth import custom_roles
from app.auth.staff import Staff
from app.errors import Forbidden
from app.suppliers import bank_accounts
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa1158_supplier_bank import (
    ADMIN_ENTERER,
    CHECKER,
    NOW,
    ROW,
    SALES,
    _row,
    decide_conn,
    writes,
)

REAL_APPROVER_KIND = bank_accounts.approver_kind

# ── Who approves (tech lead S289): the business owner, the tech lead as the fallback ─────────────────────────────


SIGNED_IN = Staff("cog:it-1158p2-sub", None, "Admin", "admin", True, None, cognito_sub="it-1158p2-sub")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("markers", "kind"),
    [
        ((False, True), "business_owner"),
        ((True, True), "business_owner"),
        ((True, False), "tech_lead"),
        ((False, False), None),
    ],
)
async def test_approver_kind_reads_the_markers(
    monkeypatch: pytest.MonkeyPatch, markers: tuple[bool, bool], kind: str | None
) -> None:
    async def fake_markers(_conn: Any, _staff: Any) -> tuple[bool, bool]:
        return markers

    monkeypatch.setattr(custom_roles, "markers", fake_markers)
    assert await REAL_APPROVER_KIND(ScriptedConn(), SIGNED_IN) == kind


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "staff",
    [
        Staff("cog:it-1158p2-fin", None, "Fin", "finance", True, None, cognito_sub="it-1158p2-fin"),
        Staff("it-1158p2-legacy", None, "Legacy", "admin", True, None),  # no WORKFORCE identity
        Staff("cog:it-1158p2-pv", None, "Pv", "admin", True, None, cognito_sub="it-1158p2-pv", preview=True),
        Staff("cog:it-1158p2-w", None, "W", "admin", True, None, cognito_sub="it-1158p2-w", e2e_writer="pricing_maker"),
        Staff(
            "cog:it-1158p2-e2e",
            "e2e-owner@e2e-mail.lumiguides.it.com",
            "E2E owner",
            "admin",
            True,
            None,
            cognito_sub="it-1158p2-e2e",
        ),
    ],
    ids=["no_capability", "no_identity", "preview", "e2e_writer", "e2e_owner"],
)
async def test_approver_kind_never_reads_for_a_capped_session(monkeypatch: pytest.MonkeyPatch, staff: Staff) -> None:
    async def owner_markers(_conn: Any, _staff: Any) -> tuple[bool, bool]:
        raise AssertionError("no marker read for a session that never approves")

    monkeypatch.setattr(custom_roles, "markers", owner_markers)
    assert await REAL_APPROVER_KIND(ScriptedConn(), staff) is None


def test_no_approve_action_for_another_administrator() -> None:
    other = bank_accounts.row_out(_row(), CHECKER)
    assert other["actions"]["approve"] is False and other["actions"]["reject"] is True
    assert other["approve_blocked"] == bank_accounts.APPROVER_NOT_ALLOWED
    # The person who entered it is not told about the approver (their own row has no approval to give).
    own = bank_accounts.row_out(_row(entered_by=ADMIN_ENTERER.cis_user_id), ADMIN_ENTERER)
    assert own["approve_blocked"] is None
    assert bank_accounts.row_out(_row(), SALES)["approve_blocked"] is None


@pytest.mark.asyncio
async def test_another_administrator_is_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def nobody(_conn: Any, _staff: Staff) -> str | None:
        return None

    monkeypatch.setattr(bank_accounts, "approver_kind", nobody)
    conn = decide_conn()
    with pytest.raises(Forbidden) as err:
        await bank_accounts.decide(conn, CHECKER, ROW, "approve", "Called the desk on the contract number")
    assert err.value.code == bank_accounts.APPROVER_NOT_ALLOWED and writes(conn) == []
    # A rejection stays open to every holder of supplier_bank.approve (it keeps the money where it was).
    await bank_accounts.decide(decide_conn(), CHECKER, ROW, "reject", "number does not match")


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "suffix"), [("business_owner", ""), ("tech_lead", " by the tech lead")])
async def test_the_approval_records_who_approved_as(monkeypatch: pytest.MonkeyPatch, kind: str, suffix: str) -> None:
    async def as_kind(_conn: Any, _staff: Staff) -> str | None:
        return kind

    monkeypatch.setattr(bank_accounts, "approver_kind", as_kind)
    lock_row = {"pending": False, "last_approved": NOW, "now": NOW}
    conn = decide_conn().on("FROM booking.supplier_bank_account a WHERE a.supplier_id", lock_row)
    await bank_accounts.decide(conn, CHECKER, ROW, "approve", "Called the accounts desk on the contract number")
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert json.loads(audit[7])["approved_as"] == kind
    assert (" by the tech lead" in str(audit)) is bool(suffix)


@pytest.mark.asyncio
async def test_my_work_and_the_list_filter_are_empty_for_another_administrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def nobody(_conn: Any, _staff: Staff) -> str | None:
        return None

    monkeypatch.setattr(bank_accounts, "approver_kind", nobody)
    where: list[str] = []
    await bank_accounts.approvable_filter(ScriptedConn(), CHECKER, where, lambda *_: None)
    assert where == ["false"]
    conn = ScriptedConn().on("count(*)::int AS count", {"count": 0, "oldest_at": None})
    await bank_accounts.queue(conn, CHECKER)
    assert "AND false" in conn.calls[0][0]
