"""AA-1158 part 1 (bk-064) on a real PostgreSQL, as role ``booking`` where the app writes: registration and re-run
no-op, the policy seed, grants, and the database rules of the three-person approval model:

- suppliers: a new one has no activation; its creator, and whoever changed its pending data (``writers``), never
  activates it (CHECK, also when the statement names no activator: the guard takes it from updated_by); a forged
  business-owner flag is refused; the owner's flag holds and the row stays editable after the marker moves; never
  back to pending;
- contracts: the trigger keeps the writer list (the client value is ignored); no writer publishes, unless the
  business owner, flagged;
- bills and payouts: the owner self-approves her bill, flagged; nobody else does; the bill approver records the payout
  only flagged and only as the business owner; the creator never pays (bk-030 rule kept); the flag never changes;
- the generic guard on holiday events (final flag) and the season / stop-sale split in the CHECK text;
- billing_entity_change: starts pending, decided once, dated now, never by the requester unless flagged; its reasons
  can be redacted alone (DM-6.7).

The business owner is a ``staff_users`` row with a Cognito identity and a ``shared.staff_booking_grant_stub`` row with
``is_business_owner``; the tech lead the same with the marker off. All names are invented."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "064_approval_rules_three_person.sql"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_it1158"
OWNER_SUB, LEAD_SUB = "b1158000-0000-4000-8000-00000000000a", "b1158000-0000-4000-8000-00000000000b"
OWNER, LEAD = f"cog:{OWNER_SUB}", f"cog:{LEAD_SUB}"
MAKER, CHECKER, PAYER = "it-1158-maker", "it-1158-checker", "it-1158-payer"
ACTIVATE = (
    "UPDATE booking.suppliers SET status = 'active', activated_by = $2, activated_at = now(), owner_self_approved = $3,"
    " updated_by = $2 WHERE id = $1::uuid"
)


async def _marker(admin: asyncpg.Connection, sub: str, on: bool) -> None:
    await admin.execute(
        "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code, is_business_owner)"
        " VALUES ($1, NULL, $2) ON CONFLICT (cognito_sub) DO UPDATE SET is_business_owner = EXCLUDED.is_business_owner",
        sub,
        on,
    )


async def _purge(admin: asyncpg.Connection) -> None:
    await ops.purge_ops(admin)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.billing_entity WHERE code LIKE 'IT1158%'")
        await admin.execute("DELETE FROM booking.holiday_events WHERE code LIKE 'it1158_%'")
        await admin.execute("DELETE FROM booking.supplier_contracts WHERE title LIKE 'it-1158%'")
        await admin.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-1158%'")
    await admin.execute("DELETE FROM shared.staff_booking_grant_stub WHERE cognito_sub LIKE 'b1158000-%'")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (MAKER, "contracting"), (CHECKER, "admin"), (PAYER, "admin"))
    for key, sub in ((OWNER, OWNER_SUB), (LEAD, LEAD_SUB)):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name, cognito_iss, cognito_sub)"
            " VALUES ($1, 'admin', true, $1, $2, $3) ON CONFLICT (cis_user_id) DO UPDATE SET active = true",
            key,
            ISS,
            sub,
        )
    await _marker(conn, OWNER_SUB, True)
    await _marker(conn, LEAD_SUB, False)
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _supplier(conn: asyncpg.Connection, by: str | None, name: str, status: str = "pending") -> str:
    return await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, status, created_by, updated_by)"
        " VALUES ($1, ARRAY['hotel'], 'VN', $2, $3, $3) RETURNING id::text",
        f"it-1158 {name} {uuid.uuid4().hex[:6]}",
        status,
        by,
    )


# ── Registration, seed, grants ──────────────────────────────────────────────────────────────────────────────────


async def test_registered_once_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    policy = "SELECT id, param_key, value FROM booking.pricing_policy WHERE created_by = 'seed:bk-064'"
    before = sorted((str(r["id"]), r["param_key"], r["value"]) for r in await admin.fetch(policy))
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert sorted((str(r["id"]), r["param_key"], r["value"]) for r in await admin.fetch(policy)) == before
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-064'") == 1


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-064")
    assert "-- migrate:" not in text


async def test_policy_switches_seeded_on_and_approved(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(
        "SELECT p.param_key, p.value::text AS value, bool_or(e.event = 'approved') AS approved"
        " FROM booking.pricing_policy p JOIN booking.pricing_policy_events e ON e.policy_id = p.id"
        " WHERE p.created_by = 'seed:bk-064' GROUP BY p.param_key, p.value"
    )
    assert {r["param_key"]: (r["value"], r["approved"]) for r in rows} == {
        "owner_self_approval_allowed": ("true", True),
        "owner_payout_after_approval_allowed": ("true", True),
    }


async def test_booking_is_bound_and_grants_are_narrow(admin: asyncpg.Connection) -> None:
    assert not await admin.fetchval("SELECT pg_has_role('booking', 'bk_owner', 'USAGE')")
    priv = "SELECT has_column_privilege('booking', $1, $2, 'UPDATE')"
    for table, col, allowed in (
        ("booking.billing_entity_change", "status", True),
        ("booking.billing_entity_change", "decided_by", True),
        ("booking.billing_entity_change", "payment_instructions", False),
        ("booking.billing_entity_change", "requested_by", False),
        ("booking.supplier_bills", "owner_self_approved", True),
        ("booking.supplier_payouts", "same_approver_payer", False),
    ):
        assert await admin.fetchval(priv, table, col) is allowed, (table, col)
    assert not await admin.fetchval("SELECT has_table_privilege('booking', 'booking.billing_entity_change', 'DELETE')")


# ── Suppliers ───────────────────────────────────────────────────────────────────────────────────────────────────


async def test_a_new_supplier_has_no_activation(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="no activation yet"):
        await app_conn.execute(
            "INSERT INTO booking.suppliers (company_name, categories, status, created_by, activated_by, activated_at)"
            " VALUES ('it-1158 Early', ARRAY['hotel'], 'active', $1, $2, now())",
            MAKER,
            CHECKER,
        )
    sid = await _supplier(app_conn, MAKER, "Fernhill Lodge")
    assert await app_conn.fetchval("SELECT status FROM booking.suppliers WHERE id = $1::uuid", sid) == "pending"


async def test_the_creator_never_activates_and_a_forged_flag_is_refused(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn, LEAD, "Lead Lodge")
    # The running code's statement (no activator named): the guard takes updated_by, the CHECK refuses the creator.
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await app_conn.execute(
            "UPDATE booking.suppliers SET status = 'active', updated_by = $2 WHERE id = $1::uuid", sid, LEAD
        )
    assert exc.value.constraint_name == "suppliers_maker_checker_chk"
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await app_conn.execute(ACTIVATE, sid, LEAD, False)
    assert exc.value.constraint_name in ("suppliers_maker_checker_chk", "suppliers_owner_self_chk")
    with pytest.raises(asyncpg.CheckViolationError, match="not the business owner"):
        await app_conn.execute(ACTIVATE, sid, LEAD, True)
    with pytest.raises(asyncpg.CheckViolationError, match="activated_by = updated_by"):
        await app_conn.execute(
            "UPDATE booking.suppliers SET status = 'active', activated_by = $2, activated_at = now(), updated_by = $3"
            " WHERE id = $1::uuid",
            sid,
            CHECKER,
            PAYER,
        )
    await app_conn.execute(
        "UPDATE booking.suppliers SET status = 'active', updated_by = $2 WHERE id = $1::uuid", sid, CHECKER
    )
    row = await app_conn.fetchrow(
        "SELECT status, activated_by, activated_at IS NOT NULL AS dated, owner_self_approved FROM booking.suppliers"
        " WHERE id = $1::uuid",
        sid,
    )
    assert dict(row) == {"status": "active", "activated_by": CHECKER, "dated": True, "owner_self_approved": False}
    with pytest.raises(asyncpg.CheckViolationError, match="only with an activation"):
        await app_conn.execute("UPDATE booking.suppliers SET activated_by = $2 WHERE id = $1::uuid", sid, PAYER)
    await app_conn.execute(
        "UPDATE booking.suppliers SET status = 'inactive', updated_by = $2 WHERE id = $1::uuid", sid, CHECKER
    )
    with pytest.raises(asyncpg.CheckViolationError, match="never goes back to pending"):
        await app_conn.execute("UPDATE booking.suppliers SET status = 'pending' WHERE id = $1::uuid", sid)
    with pytest.raises(asyncpg.CheckViolationError, match="creator never changes"):
        await app_conn.execute("UPDATE booking.suppliers SET created_by = $2 WHERE id = $1::uuid", sid, CHECKER)


async def test_the_business_owner_self_activates_flagged_and_keeps_editing(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection
) -> None:
    sid = await _supplier(app_conn, OWNER, "Owner Lodge")
    await app_conn.execute(ACTIVATE, sid, OWNER, True)
    assert await app_conn.fetchval("SELECT owner_self_approved FROM booking.suppliers WHERE id = $1::uuid", sid)
    await _marker(admin, OWNER_SUB, False)  # the marker moves in CIS
    await app_conn.execute(
        "UPDATE booking.suppliers SET website = 'https://lodge.example', updated_by = $2 WHERE id = $1::uuid",
        sid,
        CHECKER,
    )
    # A re-activation by someone else clears the flag (mutable mode); the audit row keeps the history.
    await app_conn.execute(
        "UPDATE booking.suppliers SET status = 'inactive', updated_by = $2 WHERE id = $1::uuid", sid, CHECKER
    )
    await app_conn.execute(ACTIVATE, sid, CHECKER, False)
    assert not await app_conn.fetchval("SELECT owner_self_approved FROM booking.suppliers WHERE id = $1::uuid", sid)


async def test_whoever_edits_a_pending_supplier_never_activates_it(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection
) -> None:
    """R5 like R4: every staff key that changed the pending data is a maker (the trigger keeps the list)."""
    writers = "SELECT writers FROM booking.suppliers WHERE id = $1::uuid"
    sid = await app_conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, status, created_by, updated_by, writers)"
        " VALUES ('it-1158 Writers Lodge', ARRAY['hotel'], 'pending', $1, $1, ARRAY['forged']) RETURNING id::text",
        MAKER,
    )
    assert await app_conn.fetchval(writers, sid) == [MAKER]  # the client value is ignored
    await app_conn.execute(
        "UPDATE booking.suppliers SET contact_email = 'desk@lodge.example', updated_by = $2 WHERE id = $1::uuid",
        sid,
        CHECKER,
    )
    assert await app_conn.fetchval(writers, sid) == [MAKER, CHECKER]
    await app_conn.execute(  # no data change: not a writer
        "UPDATE booking.suppliers SET contact_email = 'desk@lodge.example', updated_by = $2 WHERE id = $1::uuid",
        sid,
        PAYER,
    )
    assert await app_conn.fetchval(writers, sid) == [MAKER, CHECKER]
    for statement in (
        "UPDATE booking.suppliers SET status = 'active', updated_by = $2 WHERE id = $1::uuid",
        ACTIVATE.replace("$3", "false"),
    ):
        with pytest.raises(asyncpg.CheckViolationError) as exc:
            await app_conn.execute(statement, sid, CHECKER)
        assert exc.value.constraint_name in ("suppliers_maker_checker_chk", "suppliers_owner_self_chk")
    # An edit in the activating statement makes the activator a writer: refused too.
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await app_conn.execute(
            "UPDATE booking.suppliers SET status = 'active', website = 'https://x.example', updated_by = $2"
            " WHERE id = $1::uuid",
            sid,
            PAYER,
        )
    assert exc.value.constraint_name == "suppliers_maker_checker_chk"
    await app_conn.execute(ACTIVATE, sid, PAYER, False)
    row = await app_conn.fetchrow(
        "SELECT status, activated_by, writers FROM booking.suppliers WHERE id = $1::uuid", sid
    )
    assert dict(row) == {"status": "active", "activated_by": PAYER, "writers": [MAKER, CHECKER]}
    await app_conn.execute(  # edits of a supplier that is not pending add no writer
        "UPDATE booking.suppliers SET website = 'https://y.example', updated_by = $2 WHERE id = $1::uuid", sid, LEAD
    )
    assert await app_conn.fetchval(writers, sid) == [MAKER, CHECKER]
    # The business owner who edited a pending supplier activates it only flagged.
    other = await _supplier(app_conn, MAKER, "Owner Edit Lodge")
    await app_conn.execute(
        "UPDATE booking.suppliers SET contact_phone = '+84 28 0000 0000', updated_by = $2 WHERE id = $1::uuid",
        other,
        OWNER,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(ACTIVATE, other, OWNER, False)
    await app_conn.execute(ACTIVATE, other, OWNER, True)
    assert await app_conn.fetchval("SELECT owner_self_approved FROM booking.suppliers WHERE id = $1::uuid", other)


async def test_owner_connections_keep_the_legacy_insert(admin: asyncpg.Connection) -> None:
    """Fixtures and DB Migrate run-script (members of bk_owner) are not bound by the activation guard."""
    sid = await _supplier(admin, None, "Legacy Coaches", status="active")
    assert await admin.fetchval("SELECT activated_by FROM booking.suppliers WHERE id = $1::uuid", sid) is None


# ── Contracts ───────────────────────────────────────────────────────────────────────────────────────────────────


async def _contract(conn: asyncpg.Connection, supplier_id: str, by: str) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, currency, created_by, updated_by,"
        " writers) VALUES ($1::uuid, 'it-1158 rates', DATE '2027-01-01', 'USD', $2, $2, ARRAY['forged']) RETURNING"
        " id::text",
        supplier_id,
        by,
    )


PUBLISH = (
    "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now(),"
    " owner_self_approved = $3 WHERE id = $1::uuid"
)


async def test_no_writer_publishes_and_the_list_is_kept_by_the_trigger(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection
) -> None:
    sup = await _supplier(admin, None, "Writers Lodge", status="active")
    cid = await _contract(app_conn, sup, MAKER)
    writers = "SELECT writers FROM booking.supplier_contracts WHERE id = $1::uuid"
    assert await app_conn.fetchval(writers, cid) == [MAKER]
    await app_conn.execute(
        "UPDATE booking.supplier_contracts SET notes = 'checked', updated_by = $2, writers = '{}' WHERE id = $1::uuid",
        cid,
        CHECKER,
    )
    assert await app_conn.fetchval(writers, cid) == [MAKER, CHECKER]
    for writer in (MAKER, CHECKER):
        with pytest.raises(asyncpg.CheckViolationError) as exc:
            await app_conn.execute(PUBLISH, cid, writer, False)
        assert exc.value.constraint_name in (
            "supplier_contracts_maker_checker_chk",
            "supplier_contracts_owner_self_chk",
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(PUBLISH, cid, LEAD, True)  # a flag without a self-approval
    await app_conn.execute(PUBLISH, cid, LEAD, False)
    assert await app_conn.fetchval(writers, cid) == [MAKER, CHECKER]


async def test_the_business_owner_publishes_her_own_draft_flagged(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection
) -> None:
    sup = await _supplier(admin, None, "Owner Rates", status="active")
    lead_draft = await _contract(app_conn, sup, LEAD)
    with pytest.raises(asyncpg.CheckViolationError, match="not the business owner"):
        await app_conn.execute(PUBLISH, lead_draft, LEAD, True)
    own = await _contract(app_conn, sup, OWNER)
    await app_conn.execute(PUBLISH, own, OWNER, True)
    assert await app_conn.fetchval(
        "SELECT owner_self_approved FROM booking.supplier_contracts WHERE id = $1::uuid", own
    )


# ── Bills and payouts ───────────────────────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def world(admin: asyncpg.Connection) -> dict[str, Any]:
    sup = await ops.supply(admin, "Kestrel")
    case = await ops.sold_case(admin, sup, tag="bk064")
    bookings = [await ops.confirmed_booking(admin, case, sup["supplier"], net="100.00") for _ in range(5)]
    return {"supplier": sup["supplier"], "bookings": bookings}


async def _bill(conn: asyncpg.Connection, world: dict[str, Any], n: int, by: str) -> str:
    made = await ops.bill(
        conn, world["supplier"], by=by, amount="100.00", number=f"IT1158-{n}", lines=((world["bookings"][n], "100"),)
    )
    return made["id"]


APPROVE = (
    "UPDATE booking.supplier_bills SET status = 'approved', approved_by = $2, approved_at = now(),"
    " owner_self_approved = $3 WHERE id = $1::uuid"
)
PAYOUT = (
    "INSERT INTO booking.supplier_payouts (bill_id, amount, currency, method, paid_on, idempotency_key,"
    " request_sha256, recorded_by, same_approver_payer)"
    " VALUES ($1::uuid, 100, 'USD', 'bank_transfer', current_date, $2, $3, $4, $5) RETURNING id::text"
)


async def test_bill_self_approval_only_for_the_business_owner(
    app_conn: asyncpg.Connection, world: dict[str, Any]
) -> None:
    lead_bill = await _bill(app_conn, world, 0, LEAD)
    with pytest.raises(asyncpg.CheckViolationError, match="maker-checker"):
        await app_conn.execute(APPROVE, lead_bill, LEAD, False)
    with pytest.raises(asyncpg.CheckViolationError, match="not the business owner"):
        await app_conn.execute(APPROVE, lead_bill, LEAD, True)
    own = await _bill(app_conn, world, 1, OWNER)
    await app_conn.execute(APPROVE, own, OWNER, True)
    with pytest.raises(asyncpg.CheckViolationError, match="stays recorded"):
        await app_conn.execute("UPDATE booking.supplier_bills SET owner_self_approved = false WHERE id = $1::uuid", own)
    # The bill's creator never pays it, the business owner included (bk-030 rule kept, critique 9).
    with pytest.raises(asyncpg.CheckViolationError, match="does not pay it"):
        await app_conn.fetchval(PAYOUT, own, ops.key("it1158-own"), ops.SHA, OWNER, True)


async def test_the_approver_pays_only_flagged_and_only_as_the_business_owner(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection, world: dict[str, Any]
) -> None:
    b = await _bill(app_conn, world, 2, MAKER)
    await app_conn.execute(APPROVE, b, CHECKER, False)
    with pytest.raises(asyncpg.CheckViolationError, match="approved it does not record its payout"):
        await app_conn.fetchval(PAYOUT, b, ops.key("it1158-a"), ops.SHA, CHECKER, False)
    with pytest.raises(asyncpg.CheckViolationError, match="not the business owner"):
        await app_conn.fetchval(PAYOUT, b, ops.key("it1158-b"), ops.SHA, CHECKER, True)
    with pytest.raises(asyncpg.CheckViolationError, match="recorded by the bill approver only"):
        await app_conn.fetchval(PAYOUT, b, ops.key("it1158-c"), ops.SHA, PAYER, True)
    await app_conn.fetchval(PAYOUT, b, ops.key("it1158-d"), ops.SHA, PAYER, False)  # a third person: fine

    owner_bill = await _bill(app_conn, world, 3, MAKER)
    await app_conn.execute(APPROVE, owner_bill, OWNER, False)
    paid = await app_conn.fetchval(PAYOUT, owner_bill, ops.key("it1158-e"), ops.SHA, OWNER, True)
    with pytest.raises(asyncpg.CheckViolationError, match="money facts never change"):
        await admin.execute("UPDATE booking.supplier_payouts SET same_approver_payer = false WHERE id = $1::uuid", paid)


# ── Generic guard, CHECK texts ──────────────────────────────────────────────────────────────────────────────────


async def _event(conn: asyncpg.Connection, by: str) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.holiday_events (country_iso2, code, name, year, date_from, date_to, reason, created_by)"
        " VALUES ('VN', $1, 'Lantern Week', 2027, DATE '2027-02-01', DATE '2027-02-05', 'yearly', $2)"
        " RETURNING id::text",
        f"it1158_{uuid.uuid4().hex[:8]}",
        by,
    )


DECIDE = (
    "UPDATE booking.holiday_events SET status = 'approved', decided_by = $2, decided_at = now(),"
    " owner_self_approved = $3 WHERE id = $1::uuid"
)


async def test_generic_guard_on_holiday_events(app_conn: asyncpg.Connection, admin: asyncpg.Connection) -> None:
    lead = await _event(app_conn, LEAD)
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await app_conn.execute(DECIDE, lead, LEAD, False)
    assert exc.value.constraint_name in ("holiday_events_status_chk", "holiday_events_owner_self_chk")
    with pytest.raises(asyncpg.CheckViolationError, match="not the business owner"):
        await app_conn.execute(DECIDE, lead, LEAD, True)
    await app_conn.execute(DECIDE, lead, CHECKER, False)
    own = await _event(app_conn, OWNER)
    await app_conn.execute(DECIDE, own, OWNER, True)
    with pytest.raises(asyncpg.CheckViolationError, match="stays recorded"):
        await app_conn.execute("UPDATE booking.holiday_events SET owner_self_approved = false WHERE id = $1::uuid", own)
    # An unreadable marker refuses a self-approval only (fail closed), never a normal approval.
    await _marker(admin, OWNER_SUB, False)
    again = await _event(app_conn, OWNER)
    with pytest.raises(asyncpg.CheckViolationError, match="not the business owner"):
        await app_conn.execute(DECIDE, again, OWNER, True)


async def test_check_texts_keep_stop_sales_and_rejections_strict(admin: asyncpg.Connection) -> None:
    defs = {
        r["conname"]: r["def"]
        for r in await admin.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint"
            " WHERE connamespace = 'booking'::regnamespace AND conname = ANY($1::text[])",
            [
                "season_ranges_status_chk",
                "season_ranges_end_chk",
                "price_list_versions_rejected_chk",
                "agency_payment_maker_checker_chk",
            ],
        )
    }
    assert "kind = 'season'" in defs["season_ranges_status_chk"]
    assert "owner_self_approved" not in defs["season_ranges_end_chk"]
    assert "owner_self_approved" not in defs["price_list_versions_rejected_chk"]
    assert "(rejected_by <> recorded_by)" in defs["agency_payment_maker_checker_chk"]


# ── Billing entity changes ──────────────────────────────────────────────────────────────────────────────────────


async def test_billing_entity_change_is_decided_once_by_another_person(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection
) -> None:
    entity = await admin.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity) VALUES ('IT1158A', 'Lanternfield Travel Ltd')"
        " RETURNING id::text"
    )
    insert = (
        "INSERT INTO booking.billing_entity_change (billing_entity_id, set_payment_instructions, payment_instructions,"
        " reason, requested_by, status, decided_by, decided_at) VALUES ($1::uuid, true, 'Account 0001', 'new bank',"
        " $2, $3, $4, $5) RETURNING id::text"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="starts pending"):
        await app_conn.fetchval(insert, entity, MAKER, "approved", CHECKER, None)
    change = await app_conn.fetchval(insert, entity, MAKER, "pending", None, None)
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.fetchval(insert, entity, MAKER, "pending", None, None)  # one pending change per entity
    decide = (
        "UPDATE booking.billing_entity_change SET status = 'approved', decided_by = $2, decided_at = $3,"
        " owner_self_approved = $4 WHERE id = $1::uuid"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="dated now"):
        await app_conn.execute(decide, change, CHECKER, None, False)
    now = await app_conn.fetchval("SELECT now()")
    with pytest.raises(asyncpg.CheckViolationError, match="dated now"):
        await app_conn.execute(decide, change, CHECKER, now, False)  # another transaction's now()
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await app_conn.execute(decide.replace("$3", "now()").replace("$4", "$3"), change, MAKER, False)
    assert exc.value.constraint_name in ("billing_entity_change_status_chk", "billing_entity_change_owner_self_chk")
    await app_conn.execute(decide.replace("$3", "now()").replace("$4", "$3"), change, CHECKER, False)
    with pytest.raises(asyncpg.CheckViolationError, match="final"):
        await admin.execute("UPDATE booking.billing_entity_change SET decided_at = now() WHERE id = $1::uuid", change)


async def test_billing_entity_change_reasons_can_be_redacted_alone(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection
) -> None:
    """Erasure (DM-6.7): reason / reject_reason may become '[redacted]', alone, on a decided row too (the owner
    connection runs it in a reviewed run-script; role booking has no UPDATE grant on reason)."""
    entity = await admin.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity) VALUES ('IT1158R', 'Lanternfield Travel Ltd')"
        " RETURNING id::text"
    )
    change = await app_conn.fetchval(
        "INSERT INTO booking.billing_entity_change (billing_entity_id, set_payment_instructions, payment_instructions,"
        " reason, requested_by) VALUES ($1::uuid, true, 'Account 0002', 'asked by Mai Linh', $2) RETURNING id::text",
        entity,
        MAKER,
    )
    await app_conn.execute(
        "UPDATE booking.billing_entity_change SET status = 'rejected', decided_by = $2, decided_at = now(),"
        " reject_reason = 'Mai Linh left' WHERE id = $1::uuid",
        change,
        CHECKER,
    )
    redact = "UPDATE booking.billing_entity_change SET {} WHERE id = $1::uuid"
    with pytest.raises(asyncpg.CheckViolationError, match="final"):
        await admin.execute(redact.format("reason = 'someone else'"), change)
    with pytest.raises(asyncpg.CheckViolationError, match="final"):
        await admin.execute(redact.format("reason = '[redacted]', decided_by = 'it-1158-other'"), change)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(redact.format("reason = '[redacted]'"), change)
    await admin.execute(redact.format("reason = '[redacted]', reject_reason = '[redacted]'"), change)
    row = await admin.fetchrow(
        "SELECT reason, reject_reason, status, decided_by FROM booking.billing_entity_change WHERE id = $1::uuid",
        change,
    )
    assert dict(row) == {
        "reason": "[redacted]",
        "reject_reason": "[redacted]",
        "status": "rejected",
        "decided_by": CHECKER,
    }
