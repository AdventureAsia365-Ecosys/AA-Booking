"""AA-1158 part 1 — approval rules for a three-person team, without a database (the DB rules are
tests/integration/test_bk064_schema.py, the HTTP flows tests/integration/test_aa1158_approval_rules_api.py).

- the business-owner read: uncached, fail closed, never for a capped session (Preview, Dev-only E2E writer, E2E user);
- :func:`app.auth.self_approval.check`: not a maker -> False; maker + owner + switch on -> True; anything else 403;
- suppliers start pending; activation by the creator is refused unless the business owner (flagged);
- contract publish follows the writer list; bills, payouts, agency payments, price adjustments, seasons;
- billing entity changes: request, decide, apply; the sellable gate; the audit filter and the My work counters.
All names are invented."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

import asyncpg
import pytest

from app import audit
from app.agency_billing import entity_changes, payments
from app.auth import custom_roles, self_approval
from app.auth.staff import E2E_TEST_STAFF_EMAIL, Staff
from app.b2b import price_adjustments
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.ops import bill_approvals, supplier_bills
from app.pricing import seasons_admin
from app.suppliers import service as suppliers
from tests.unit.fakes import ScriptedConn

OWNER_SUB, LEAD_SUB = "b1158000-0000-4000-8000-00000000000a", "b1158000-0000-4000-8000-00000000000b"
OWNER = Staff(f"cog:{OWNER_SUB}", "owner@example.test", "Owner", "admin", True, None, cognito_sub=OWNER_SUB)
LEAD = Staff(f"cog:{LEAD_SUB}", "lead@example.test", "Lead", "admin", True, None, cognito_sub=LEAD_SUB)
MAKER = Staff("it-1158-maker", None, "Maker", "contracting", True, None)
CHECKER = Staff("it-1158-checker", None, "Checker", "admin", True, None)
FIN = Staff("it-1158-fin", None, "Fin", "finance", True, None)
GRANT = "FROM shared.v_staff_booking_grant"
SUPPLIER = "5a000000-0000-4000-8000-000000001158"
CONTRACT = "5c000000-0000-4000-8000-000000001158"
ADJUSTMENT = "5d000000-0000-4000-8000-000000001158"


def marker(on: bool = True) -> dict[str, Any]:
    return {"booking_role_code": None, "is_owner": False, "is_business_owner": on}


def owner_conn(*, on: bool = True) -> ScriptedConn:
    return ScriptedConn().on(GRANT, lambda sub: marker(on) if sub == OWNER_SUB else marker(False))


def switch_off(conn: ScriptedConn, key: str) -> ScriptedConn:
    """The policy rows of the fake with ``key`` turned off."""
    from tests.unit.fakes import policy_rows  # noqa: PLC0415

    return conn.on("FROM booking.pricing_policy p", policy_rows(**{key: "false"}))


# ── The business-owner read ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_marker_is_read_uncached_and_fails_closed() -> None:
    conn = owner_conn()
    assert await custom_roles.is_business_owner(conn, OWNER_SUB) is True
    assert await custom_roles.is_business_owner(conn, OWNER_SUB) is True
    assert len(conn.sql(GRANT)) == 2  # no cache
    assert await custom_roles.is_business_owner(conn, LEAD_SUB) is False
    assert await custom_roles.is_business_owner(conn, None) is False
    assert await custom_roles.is_business_owner(ScriptedConn(), OWNER_SUB) is False  # no row
    broken = ScriptedConn().on(GRANT, asyncpg.InsufficientPrivilegeError("denied"))
    assert await custom_roles.is_business_owner(broken, OWNER_SUB) is False


class InTransaction(ScriptedConn):
    """A connection inside the caller's transaction that counts the savepoints opened on it."""

    savepoints = 0

    def is_in_transaction(self) -> bool:
        return True

    def transaction(self, **kwargs: Any) -> Any:
        self.savepoints += 1
        return super().transaction(**kwargs)


@pytest.mark.asyncio
async def test_the_marker_and_switch_reads_run_in_a_savepoint_inside_a_transaction() -> None:
    """A failed read of the CIS view or the policy must not abort the caller's transaction (the payout, the approve)."""
    broken = InTransaction().on(GRANT, asyncpg.InsufficientPrivilegeError("denied"))
    assert await custom_roles.is_business_owner(broken, OWNER_SUB) is False
    assert broken.savepoints == 1
    policy = InTransaction().on("FROM booking.pricing_policy p", asyncpg.InsufficientPrivilegeError("denied"))
    assert await self_approval.switch_on(policy, self_approval.APPROVE) is False
    assert policy.savepoints >= 1
    outside = owner_conn()  # no transaction: no savepoint (nothing to protect)
    assert await custom_roles.is_business_owner(outside, OWNER_SUB) is True


@pytest.mark.parametrize(
    "session",
    [
        replace(OWNER, preview=True),
        replace(OWNER, e2e_writer="pricing_checker"),
        replace(OWNER, e2e_grants=frozenset({"trip_plan.write"})),
        replace(OWNER, email=E2E_TEST_STAFF_EMAIL.upper()),
        replace(OWNER, active=False),
        replace(OWNER, cognito_sub=None),
    ],
    ids=["preview", "e2e_writer", "e2e_grants", "e2e_test_user", "inactive", "no_identity"],
)
@pytest.mark.asyncio
async def test_a_capped_session_never_self_approves(session: Staff) -> None:
    conn = owner_conn()
    assert await self_approval.is_business_owner(conn, session) is False
    assert conn.sql(GRANT) == []  # not even read
    with pytest.raises(Forbidden) as err:
        await self_approval.check(conn, session, [session.cis_user_id], message="no")
    assert err.value.code == "maker_checker"


@pytest.mark.asyncio
async def test_check_matrix() -> None:
    conn = owner_conn()
    assert await self_approval.check(conn, CHECKER, [MAKER.cis_user_id, None], message="x") is False
    assert conn.sql(GRANT) == []  # a second person never needs the view
    assert await self_approval.check(conn, OWNER, [OWNER.cis_user_id], message="x") is True
    with pytest.raises(Forbidden):
        await self_approval.check(conn, LEAD, [LEAD.cis_user_id], message="the tech lead has no marker")
    with pytest.raises(Forbidden):
        await self_approval.check(owner_conn(on=False), OWNER, [OWNER.cis_user_id], message="marker moved")
    off = switch_off(owner_conn(), "owner_self_approval_allowed")
    with pytest.raises(Forbidden):
        await self_approval.check(off, OWNER, [OWNER.cis_user_id], message="switch off")
    assert await self_approval.check(off, OWNER, [OWNER.cis_user_id], kind=self_approval.PAYOUT, message="x")
    pay_off = switch_off(owner_conn(), "owner_payout_after_approval_allowed")
    with pytest.raises(Forbidden):
        await self_approval.check(pay_off, OWNER, [OWNER.cis_user_id], kind=self_approval.PAYOUT, message="x")


@pytest.mark.asyncio
async def test_a_switch_without_an_approved_value_reads_as_off() -> None:
    conn = ScriptedConn().on("FROM booking.pricing_policy p", [])
    assert await self_approval.switch_on(conn, self_approval.APPROVE) is False


# ── Suppliers ────────────────────────────────────────────────────────────────


def supplier_row(**over: Any) -> dict[str, Any]:
    return {
        "id": SUPPLIER,
        "code": "SUP-001158",
        "company_name": "Fernhill Lodge",
        "status": "pending",
        "created_by": MAKER.cis_user_id,
        "categories": ["hotel"],
        **over,
    }


def supplier_conn(row: dict[str, Any], *, on: bool = True) -> ScriptedConn:
    return owner_conn(on=on).on("FROM booking.suppliers s LEFT JOIN booking.countries", row)


@pytest.mark.asyncio
async def test_a_new_supplier_starts_pending_for_everyone() -> None:
    conn = ScriptedConn().on("INSERT INTO booking.suppliers", {"id": SUPPLIER, "code": "SUP-001158"})
    await suppliers.create_supplier(conn, CHECKER, {"company_name": "Fernhill Lodge", "categories": ["hotel"]})
    (args,) = conn.sql("INSERT INTO booking.suppliers")
    assert "pending" in args
    (audit_args,) = conn.sql("INSERT INTO booking.audit_logs")
    assert "pending approval" in str(audit_args)


@pytest.mark.asyncio
async def test_the_creator_never_activates_except_the_business_owner() -> None:
    with pytest.raises(Forbidden) as err:
        await suppliers.set_status(supplier_conn(supplier_row(created_by=LEAD.cis_user_id)), LEAD, SUPPLIER, "active")
    assert err.value.code == "maker_checker"
    conn = supplier_conn(supplier_row(created_by=OWNER.cis_user_id))
    await suppliers.set_status(conn, OWNER, SUPPLIER, "active")
    (update,) = conn.sql("UPDATE booking.suppliers SET status = 'active'")
    assert update == (SUPPLIER, OWNER.cis_user_id, True)
    (audit_args,) = conn.sql("INSERT INTO booking.audit_logs")
    assert "owner_self_approved" in str(audit_args) and "business-owner self-approval" in str(audit_args)
    other = supplier_conn(supplier_row())
    await suppliers.set_status(other, CHECKER, SUPPLIER, "active")
    assert other.sql("UPDATE booking.suppliers SET status = 'active'") == [(SUPPLIER, CHECKER.cis_user_id, False)]


@pytest.mark.asyncio
async def test_whoever_edited_a_pending_supplier_never_activates_it() -> None:
    """R5 like R4: a writer of the not-live data (bk-064 ``writers``) is a maker, not only the creator."""
    edited = supplier_row(writers=[MAKER.cis_user_id, CHECKER.cis_user_id])
    assert suppliers.supplier_makers(edited) == [MAKER.cis_user_id, CHECKER.cis_user_id]
    assert suppliers.supplier_makers(supplier_row(writers=None, created_by=None)) == []
    conn = supplier_conn(edited)
    with pytest.raises(Forbidden) as err:
        await suppliers.set_status(conn, CHECKER, SUPPLIER, "active")
    assert err.value.code == "maker_checker"
    assert conn.sql("UPDATE booking.suppliers SET status = 'active'") == []
    assert (await suppliers.activation(owner_conn(), CHECKER, edited))["blockers"] == ["maker_checker"]
    third = supplier_conn(edited)
    await suppliers.set_status(third, LEAD, SUPPLIER, "active")
    assert third.sql("UPDATE booking.suppliers SET status = 'active'") == [(SUPPLIER, LEAD.cis_user_id, False)]
    # The business owner who edited it activates it flagged.
    mine = supplier_row(writers=[MAKER.cis_user_id, OWNER.cis_user_id])
    assert (await suppliers.activation(owner_conn(), OWNER, mine))["self_approval"] is True
    flagged = supplier_conn(mine)
    await suppliers.set_status(flagged, OWNER, SUPPLIER, "active")
    assert flagged.sql("UPDATE booking.suppliers SET status = 'active'") == [(SUPPLIER, OWNER.cis_user_id, True)]


@pytest.mark.asyncio
async def test_pending_is_never_a_target_and_deactivating_a_pending_one_rejects_it() -> None:
    with pytest.raises(Invalid):
        await suppliers.set_status(ScriptedConn(), CHECKER, SUPPLIER, "pending")
    with pytest.raises(Forbidden):
        await suppliers.set_status(ScriptedConn(), replace(MAKER, role="sales"), SUPPLIER, "active")
    rejected = supplier_conn(supplier_row())
    await suppliers.set_status(rejected, CHECKER, SUPPLIER, "inactive", "duplicate")
    assert "supplier.rejected" in str(rejected.sql("INSERT INTO booking.audit_logs"))
    withdrawn = supplier_conn(supplier_row())
    await suppliers.set_status(withdrawn, MAKER, SUPPLIER, "inactive")
    assert "supplier.withdrawn" in str(withdrawn.sql("INSERT INTO booking.audit_logs"))


@pytest.mark.asyncio
async def test_the_db_backstop_answers_maker_checker() -> None:
    conn = supplier_conn(supplier_row()).on(
        "UPDATE booking.suppliers SET status = 'active'", asyncpg.CheckViolationError("suppliers_maker_checker_chk")
    )
    with pytest.raises(Forbidden) as err:
        await suppliers.set_status(conn, CHECKER, SUPPLIER, "active")
    assert err.value.code == "maker_checker"


@pytest.mark.asyncio
async def test_the_activation_actions_follow_the_caller() -> None:
    conn = owner_conn()
    assert await suppliers.activation(conn, CHECKER, supplier_row()) == {
        "allowed": True,
        "self_approval": False,
        "blockers": [],
    }
    mine = await suppliers.activation(conn, LEAD, supplier_row(created_by=LEAD.cis_user_id))
    assert mine == {"allowed": False, "self_approval": False, "blockers": ["maker_checker"]}
    own = await suppliers.activation(conn, OWNER, supplier_row(created_by=OWNER.cis_user_id))
    assert own == {"allowed": True, "self_approval": True, "blockers": []}
    sales = await suppliers.activation(conn, replace(MAKER, role="sales"), supplier_row(status="active"))
    assert sales["blockers"] == ["already_active", "needs_supplier_activate"]


@pytest.mark.asyncio
async def test_the_approvable_list_and_queue_leave_out_my_own_unless_owner() -> None:
    conn = owner_conn().on("SELECT count(*)::int AS count, min(s.created_at)", {"count": 2, "oldest_at": None})
    assert (await suppliers.queue(conn, LEAD))["count"] == 2
    assert conn.sql("min(s.created_at)")[-1] == (LEAD.cis_user_id,)
    await suppliers.queue(conn, OWNER)
    assert conn.sql("min(s.created_at)")[-1] == ()
    await suppliers.queue(conn, replace(MAKER, role="sales"))
    assert "AND false" in [flat for flat, _ in conn.calls if "min(s.created_at)" in flat][-1]
    listed = owner_conn().on("SELECT count(*) FROM booking.suppliers", 0)
    await suppliers.list_suppliers(listed, approvable_by=LEAD)
    assert listed.sql("SELECT count(*) FROM booking.suppliers") == [(LEAD.cis_user_id,)]


# ── Contracts ────────────────────────────────────────────────────────────────


def contract(**over: Any) -> dict[str, Any]:
    return {
        "id": CONTRACT,
        "supplier_id": SUPPLIER,
        "title": "2027 rates",
        "status": "draft",
        "created_by": MAKER.cis_user_id,
        "updated_by": CHECKER.cis_user_id,
        "writers": [MAKER.cis_user_id, CHECKER.cis_user_id],
        **over,
    }


@pytest.mark.asyncio
async def test_no_writer_publishes_and_the_owner_publishes_her_own_flagged() -> None:
    conn = owner_conn()
    assert suppliers.writers(contract(writers=[])) == [CHECKER.cis_user_id, MAKER.cis_user_id]
    for writer in (MAKER, CHECKER):
        blocked = await suppliers.publication(conn, writer, contract())
        assert blocked["blockers"] == ["maker_checker"]
    assert (await suppliers.publication(conn, LEAD, contract()))["allowed"]
    own = await suppliers.publication(conn, OWNER, contract(writers=[OWNER.cis_user_id]))
    assert own == {"allowed": True, "self_approval": True, "blockers": []}
    published = owner_conn().on("FROM booking.supplier_contracts k", contract(writers=[OWNER.cis_user_id]))
    await suppliers._move(published, OWNER, contract(writers=[OWNER.cis_user_id]), supplier_row(status="active"),
                          "published")  # fmt: skip
    assert published.sql("owner_self_approved = true") and "owner_self_approved" in str(
        published.sql("INSERT INTO booking.audit_logs")
    )
    with pytest.raises(Forbidden):
        await suppliers._move(owner_conn(), CHECKER, contract(), supplier_row(status="active"), "published")


# ── Bills and payouts ────────────────────────────────────────────────────────


def bill(**over: Any) -> dict[str, Any]:
    return {
        "id": "b-1",
        "code": "BIL-001158",
        "status": "received",
        "created_by": OWNER.cis_user_id,
        "facts_edited_by": OWNER.cis_user_id,
        "approved_by": None,
        "line_count": 1,
        "lines_amount": "100.00",
        "amount": "100.00",
        **over,
    }


@pytest.mark.asyncio
async def test_bill_owner_paths_and_blockers() -> None:
    assert await bill_approvals.owner_paths(owner_conn(), OWNER) == bill_approvals.OwnerPaths(True, True)
    assert await bill_approvals.owner_paths(owner_conn(), LEAD) == bill_approvals.OwnerPaths()
    owner = bill_approvals.OwnerPaths(approve=True, pay=True)
    assert supplier_bills.approve_blockers(bill(), OWNER, owner) == []
    assert supplier_bills.approve_blockers(bill(), OWNER) == ["maker_checker"]


@pytest.mark.asyncio
async def test_the_payer_checks() -> None:
    conn = owner_conn()
    with pytest.raises(Forbidden, match="you entered"):
        await bill_approvals.payer_checks(conn, OWNER, bill(status="approved", approved_by=CHECKER.cis_user_id))
    with pytest.raises(Forbidden, match="last changed"):
        await bill_approvals.payer_checks(
            conn, OWNER, bill(created_by=MAKER.cis_user_id, approved_by=CHECKER.cis_user_id)
        )
    approved_by_lead = bill(
        created_by=MAKER.cis_user_id, facts_edited_by=MAKER.cis_user_id, approved_by=LEAD.cis_user_id
    )
    with pytest.raises(Forbidden, match="you approved"):
        await bill_approvals.payer_checks(conn, LEAD, approved_by_lead)
    assert await bill_approvals.payer_checks(conn, CHECKER, approved_by_lead) is False
    by_owner = {**approved_by_lead, "approved_by": OWNER.cis_user_id}
    assert await bill_approvals.payer_checks(conn, OWNER, by_owner) is True


# ── Agency payments, price adjustments, seasons ──────────────────────────────


PAYMENT = {"id": "p-1", "invoice_id": "i-1", "status": "recorded", "recorded_by": OWNER.cis_user_id}


@pytest.mark.asyncio
async def test_agency_payment_owner_approves_her_own_but_never_rejects_it() -> None:
    conn = owner_conn().on("FROM booking.agency_payment WHERE id", PAYMENT)
    with pytest.raises(Forbidden):
        await payments.reject(conn, "p-1", "not on the statement", OWNER)
    with pytest.raises(Forbidden):
        await payments.approve(conn, "p-1", OWNER.cis_user_id)  # a bare staff id never self-approves
    with pytest.raises(Forbidden):
        await payments.approve(owner_conn().on("FROM booking.agency_payment WHERE id", {**PAYMENT,
                               "recorded_by": LEAD.cis_user_id}), "p-1", LEAD)  # fmt: skip


@pytest.mark.asyncio
async def test_price_adjustment_owner_self_approval() -> None:
    row = {
        "id": ADJUSTMENT,
        "departure_id": "d-1",
        "created_by": LEAD.cis_user_id,
        "approved_at": None,
        "ended_at": None,
        "end_reason": None,
        "adjust_pct": None,
        "amount": None,
        "currency": None,
    }
    conn = owner_conn().on("FROM booking.departure_price_adjustments", row)
    with pytest.raises(Forbidden) as err:
        await price_adjustments.approve(conn, LEAD, ADJUSTMENT)
    assert err.value.code == "maker_checker"


@pytest.mark.asyncio
async def test_season_approve_own_only_for_the_owner() -> None:
    assert await seasons_admin._approve_own(owner_conn(), OWNER, OWNER.cis_user_id) is True
    assert await seasons_admin._approve_own(owner_conn(), CHECKER, MAKER.cis_user_id) is False
    with pytest.raises(Forbidden):
        await seasons_admin._approve_own(owner_conn(), LEAD, LEAD.cis_user_id)
    with pytest.raises(Forbidden):
        await seasons_admin._approve_own(owner_conn(), replace(MAKER, role="sales"), LEAD.cis_user_id)
    assert seasons_admin._flag({"a": 1}, True) == {"a": 1, "owner_self_approved": True}
    assert seasons_admin._flag({"a": 1}, False) == {"a": 1}


# ── Billing entity changes ───────────────────────────────────────────────────


ENTITY = {"id": "e-1", "code": "LB", "active": True, "is_default": False, "payment_instructions": "Old bank"}


def change(**over: Any) -> dict[str, Any]:
    return {
        "id": "c-1",
        "billing_entity_id": "e-1",
        "set_payment_instructions": True,
        "payment_instructions": "New bank",
        "make_default": True,
        "reason": "New account",
        "status": "pending",
        "requested_by": FIN.cis_user_id,
        "requested_at": None,
        "decided_by": None,
        "decided_at": None,
        "reject_reason": None,
        "owner_self_approved": False,
        **over,
    }


@pytest.mark.asyncio
async def test_request_checks_what_changes() -> None:
    base = ScriptedConn().on("FROM booking.billing_entity WHERE id", ENTITY)
    with pytest.raises(Invalid):
        await entity_changes.request(base, FIN.cis_user_id, "e-1", set_payment_instructions=False,
                                     payment_instructions=None, make_default=False)  # fmt: skip
    with pytest.raises(Invalid) as err:
        await entity_changes.request(base, FIN.cis_user_id, "e-1", set_payment_instructions=True,
                                     payment_instructions="Old bank", make_default=False)  # fmt: skip
    assert err.value.code == "nothing_to_change"
    with pytest.raises(Invalid):
        await entity_changes.request(base, FIN.cis_user_id, "e-1", set_payment_instructions=True,
                                     payment_instructions="x" * 2001, make_default=False)  # fmt: skip
    with pytest.raises(NotFound):
        await entity_changes.request(ScriptedConn(), FIN.cis_user_id, "e-1", set_payment_instructions=False,
                                     payment_instructions=None, make_default=True)  # fmt: skip
    inactive = ScriptedConn().on("FROM booking.billing_entity WHERE id", {**ENTITY, "active": False})
    with pytest.raises(Invalid):
        await entity_changes.request(inactive, FIN.cis_user_id, "e-1", set_payment_instructions=False,
                                     payment_instructions=None, make_default=True)  # fmt: skip
    busy = base.on("INSERT INTO booking.billing_entity_change", asyncpg.UniqueViolationError("pending"))
    with pytest.raises(Conflict) as err:
        await entity_changes.request(busy, FIN.cis_user_id, "e-1", set_payment_instructions=True,
                                     payment_instructions="New bank", make_default=False)  # fmt: skip
    assert err.value.code == "change_pending"
    made = (
        ScriptedConn()
        .on("FROM booking.billing_entity WHERE id", ENTITY)
        .on("INSERT INTO booking.billing_entity_change", change())
    )
    out = await entity_changes.request(
        made,
        FIN.cis_user_id,
        "e-1",
        set_payment_instructions=True,
        payment_instructions=" New bank ",
        make_default=True,
        reason="New account",
    )
    assert out["status"] == "pending"
    (insert,) = made.sql("INSERT INTO booking.billing_entity_change")
    assert insert == ("e-1", True, "New bank", True, "New account", FIN.cis_user_id)


def decide_conn(row: dict[str, Any], *, on: bool = True) -> ScriptedConn:
    return (
        owner_conn(on=on)
        .on("FROM booking.billing_entity_change c WHERE c.id", row)
        .on("FROM booking.billing_entity WHERE id", ENTITY)
        .on("UPDATE booking.billing_entity_change SET status", lambda *a: {**row, "status": "decided"})
    )


@pytest.mark.asyncio
async def test_decide_paths() -> None:
    with pytest.raises(Invalid):
        await entity_changes.decide(ScriptedConn(), CHECKER, "c-1", "maybe")
    with pytest.raises(Forbidden):
        await entity_changes.decide(ScriptedConn(), replace(MAKER, role="sales"), "c-1", "approve")
    with pytest.raises(NotFound):
        await entity_changes.decide(ScriptedConn(), CHECKER, "c-1", "approve")
    with pytest.raises(Conflict):
        await entity_changes.decide(decide_conn(change(status="approved")), CHECKER, "c-1", "approve")
    with pytest.raises(Forbidden, match="maker_checker|asked"):
        await entity_changes.decide(decide_conn(change()), FIN, "c-1", "approve")
    with pytest.raises(Forbidden):
        await entity_changes.decide(decide_conn(change()), FIN, "c-1", "reject", "no")
    with pytest.raises(Forbidden):
        await entity_changes.decide(decide_conn(change()), CHECKER, "c-1", "withdraw")
    with pytest.raises(Invalid):
        await entity_changes.decide(decide_conn(change()), CHECKER, "c-1", "reject")  # a reason is required
    approved = decide_conn(change())
    await entity_changes.decide(approved, CHECKER, "c-1", "approve")
    assert approved.sql("SET is_default = false") == [("e-1", CHECKER.cis_user_id)]
    (apply,) = approved.sql("UPDATE booking.billing_entity SET payment_instructions")
    assert apply == ("e-1", True, "New bank", True, CHECKER.cis_user_id)
    own = decide_conn(change(requested_by=OWNER.cis_user_id))
    await entity_changes.decide(own, OWNER, "c-1", "approve")
    assert own.sql("UPDATE booking.billing_entity_change SET status = 'approved'")[0][2] is True
    withdrawn = decide_conn(change())
    await entity_changes.decide(withdrawn, FIN, "c-1", "withdraw")
    assert not withdrawn.sql("UPDATE booking.billing_entity SET")
    backstop = decide_conn(change()).on(
        "UPDATE booking.billing_entity_change SET status", asyncpg.CheckViolationError("status_chk")
    )
    with pytest.raises(Forbidden):
        await entity_changes.decide(backstop, CHECKER, "c-1", "reject", "wrong account")
    gone = decide_conn(change()).on("FROM booking.billing_entity WHERE id", {**ENTITY, "active": False})
    with pytest.raises(Conflict):
        await entity_changes.decide(gone, CHECKER, "c-1", "approve")


@pytest.mark.asyncio
async def test_pending_for_and_queue() -> None:
    assert await entity_changes.pending_for(ScriptedConn(), []) == {}
    conn = ScriptedConn().on("FROM booking.billing_entity_change c LEFT JOIN", [change()])
    assert (await entity_changes.pending_for(conn, ["e-1"]))["e-1"]["id"] == "c-1"
    q = owner_conn().on("min(c.requested_at)", {"count": 1, "oldest_at": None})
    assert (await entity_changes.queue(q, FIN))["count"] == 1
    assert q.sql("min(c.requested_at)")[-1] == (FIN.cis_user_id,)
    await entity_changes.queue(q, OWNER)
    assert q.sql("min(c.requested_at)")[-1] == ()
    await entity_changes.queue(q, replace(MAKER, role="sales"))
    assert "AND false" in [flat for flat, _ in q.calls if "min(c.requested_at)" in flat][-1]


# ── Audit filter ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_filter_and_month_count() -> None:
    conn = ScriptedConn()
    await audit.list_logs(conn, owner_self_approved=True)
    (flat,) = [f for f, _ in conn.calls if "FROM booking.audit_logs l" in f]
    assert audit.OWNER_SELF in flat
    month = ScriptedConn().on("FROM booking.audit_logs l WHERE", {"count": 3, "oldest_at": None})
    out = await audit.owner_self_approvals_month(month)
    assert out["count"] == 3 and out["filter"]["owner_self_approved"] == "true"
    assert date.fromisoformat(out["filter"]["from"]).day == 1


# ── The page flag ────────────────────────────────────────────────────────────


def test_my_self_approval_route() -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app.auth.staff import current_staff, get_pool  # noqa: PLC0415
    from app.main import create_app  # noqa: PLC0415
    from app.settings import Settings  # noqa: PLC0415

    def client(staff: Staff, conn: ScriptedConn) -> TestClient:
        app = create_app(Settings(database_url=None, environment="test"))
        app.dependency_overrides[current_staff] = lambda: staff
        app.dependency_overrides[get_pool] = lambda: conn
        return TestClient(app)

    body = client(OWNER, owner_conn()).get("/booking/staff/me/self-approval").json()
    assert body == {"is_business_owner": True, "approve": True, "payout": True}
    body = client(LEAD, owner_conn()).get("/booking/staff/me/self-approval").json()
    assert body == {"is_business_owner": False, "approve": False, "payout": False}
