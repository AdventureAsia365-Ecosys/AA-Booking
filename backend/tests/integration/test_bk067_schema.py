"""AA-1158 part 2 (bk-067) on a real PostgreSQL, as role ``booking`` where the app writes: registration and re-run
no-op, the seeds, grants, and the database rules:

- supplier profile fields keep their shapes, and an edit of a pending supplier's profile makes the editor a writer;
- the part 1 backstop on the billing entity: its bank text and default flag change only in the transaction that
  approved the change;
- supplier bank accounts: start pending, what was entered never changes, a second person approves with a call-back
  note (the business owner may approve her own, flagged; a forged flag is refused), one pending and one approved per
  supplier, superseded keeps its approval, the free texts can be redacted alone;
- the payout lock: no payout while a bank account is pending, nor before the newest approval is
  ``supplier_bank_payout_lock_hours`` old (aged here as the owner), none when no value is in force;
- the choices of the supplier profile (``booking.supplier_profile_option``): seeded, codes fixed, never deleted, labels,
  order and active editable by the app role.

The business owner is a ``staff_users`` row with a Cognito identity and a ``shared.staff_booking_grant_stub`` row with
``is_business_owner``. All names are invented; the "ciphertext" is filler bytes (the database never decrypts)."""

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

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "067_supplier_bank_accounts.sql"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_it1158b"
OWNER_SUB = "b1158000-0000-4000-8000-0000000000b1"
OWNER = f"cog:{OWNER_SUB}"
MAKER, CHECKER, PAYER = "it-1158b-maker", "it-1158b-checker", "it-1158b-payer"
FILLER = b"\x01" * 48
WRAPPED = b"\x02" * 184
ENTER = (
    "INSERT INTO booking.supplier_bank_account (supplier_id, bank_name, bank_country, currency, account_last4,"
    " ciphertext, wrapped_key, kms_key_id, reason, entered_by)"
    " VALUES ($1::uuid, 'Harbourline Bank', 'VN', 'USD', '4321', $2, $3, 'arn:test:key/it-1158b', 'new account', $4)"
    " RETURNING id::text"
)
APPROVE = (
    "UPDATE booking.supplier_bank_account SET status = 'approved', approved_by = $2, approved_at = now(),"
    " callback_note = 'Called the accounts desk on the number from the signed contract', owner_self_approved = $3"
    " WHERE id = $1::uuid"
)
LOCK_KEY = "supplier_bank_payout_lock_hours"
CONTACT_KEY = "supplier_bank_contact_change_days"
SEEDED_OPTIONS = {
    "contact_role": ["reservations", "accounts", "sales", "operations", "emergency", "management"],
    "terms_basis": ["prepayment", "deposit_balance", "on_arrival", "after_service", "monthly_statement"],
    "terms_method": ["bank_transfer", "card", "cash", "other"],
}


async def _purge(admin: asyncpg.Connection) -> None:
    await ops.purge_ops(admin)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.billing_entity_change WHERE billing_entity_id IN"
                            " (SELECT id FROM booking.billing_entity WHERE code LIKE 'IT1158B%')")  # fmt: skip
        await admin.execute("DELETE FROM booking.billing_entity WHERE code LIKE 'IT1158B%'")
        await admin.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-1158b%'")
        await admin.execute("DELETE FROM booking.supplier_profile_option WHERE created_by LIKE 'it-1158b%'")
    await admin.execute("DELETE FROM shared.staff_booking_grant_stub WHERE cognito_sub = $1", OWNER_SUB)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (MAKER, "contracting"), (CHECKER, "admin"), (PAYER, "admin"))
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name, cognito_iss, cognito_sub)"
        " VALUES ($1, 'admin', true, $1, $2, $3) ON CONFLICT (cis_user_id) DO UPDATE SET active = true",
        OWNER,
        ISS,
        OWNER_SUB,
    )
    await conn.execute(
        "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code, is_business_owner)"
        " VALUES ($1, NULL, true) ON CONFLICT (cognito_sub) DO UPDATE SET is_business_owner = true",
        OWNER_SUB,
    )
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


async def _pending_supplier(conn: asyncpg.Connection, name: str, by: str = MAKER) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, status, created_by, updated_by)"
        " VALUES ($1, ARRAY['hotel'], 'VN', 'pending', $2, $2) RETURNING id::text",
        f"it-1158b {name} {uuid.uuid4().hex[:6]}",
        by,
    )


# ── Registration, seeds, grants ─────────────────────────────────────────────────────────────────────────────────


async def test_registered_once_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    before = await admin.fetchval("SELECT count(*) AS n FROM booking.pricing_policy WHERE created_by = 'seed:bk-067'")
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    seeded = "SELECT count(*) AS n FROM booking.pricing_policy WHERE created_by = 'seed:bk-067'"
    assert await admin.fetchval(seeded) == before
    assert await admin.fetchval("SELECT count(*) AS n FROM booking.v_schema_versions WHERE version = 'bk-067'") == 1


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-067")
    assert "-- migrate:" not in text


async def test_seeds(admin: asyncpg.Connection) -> None:
    for key, value, unit in ((LOCK_KEY, "24", "hours"), (CONTACT_KEY, "30", "days")):
        row = await admin.fetchrow(
            "SELECT p.value::text AS value, p.unit, p.version_no FROM booking.pricing_policy p"
            " JOIN booking.pricing_policy_events a ON a.policy_id = p.id AND a.event = 'approved'"
            " WHERE p.param_key = $1 AND p.created_by = 'seed:bk-067'",
            key,
        )
        assert dict(row) == {"value": value, "unit": unit, "version_no": 1}
    rows = await admin.fetch(
        "SELECT kind, code FROM booking.supplier_profile_option WHERE created_by = 'seed:bk-067' AND active"
        " ORDER BY kind, position"
    )
    by_kind: dict[str, list[str]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r["code"])
    assert by_kind == SEEDED_OPTIONS
    caps = {r["capability"] for r in await admin.fetch(
        "SELECT capability FROM booking.staff_role_capability WHERE role_code = 'custom_product_ops_accounting'"
        " AND capability LIKE 'supplier_bank.%'")}  # fmt: skip
    assert caps == {"supplier_bank.write"}


@pytest.mark.parametrize(
    ("column", "privilege", "granted"),
    [
        ("ciphertext", "UPDATE", False),
        ("wrapped_key", "UPDATE", False),
        ("account_last4", "UPDATE", False),
        ("entered_by", "UPDATE", False),
        ("approved_by", "UPDATE", True),
        ("callback_note", "UPDATE", True),
        ("closed_by", "UPDATE", True),
        ("ciphertext", "SELECT", True),
    ],
)
async def test_grants(admin: asyncpg.Connection, column: str, privilege: str, granted: bool) -> None:
    has = await admin.fetchval(
        "SELECT has_column_privilege('booking', 'booking.supplier_bank_account', $1, $2) AS ok", column, privilege
    )
    assert has is granted
    assert not await admin.fetchval(
        "SELECT has_table_privilege('booking', 'booking.supplier_bank_account', 'DELETE') AS ok"
    )


@pytest.mark.parametrize(
    ("column", "privilege", "granted"),
    [
        ("code", "UPDATE", False),
        ("kind", "UPDATE", False),
        ("created_by", "UPDATE", False),
        ("label_en", "UPDATE", True),
        ("label_vi", "UPDATE", True),
        ("position", "UPDATE", True),
        ("active", "UPDATE", True),
        ("code", "INSERT", True),
    ],
)
async def test_option_grants(admin: asyncpg.Connection, column: str, privilege: str, granted: bool) -> None:
    has = await admin.fetchval(
        "SELECT has_column_privilege('booking', 'booking.supplier_profile_option', $1, $2) AS ok", column, privilege
    )
    assert has is granted
    assert not await admin.fetchval(
        "SELECT has_table_privilege('booking', 'booking.supplier_profile_option', 'DELETE') AS ok"
    )


async def test_option_rows_keep_their_shape(app_conn: asyncpg.Connection) -> None:
    add = (
        "INSERT INTO booking.supplier_profile_option (kind, code, label_en, label_vi, position, created_by, updated_by)"
        " VALUES ($1, $2, $3, 'Nhãn', 70, $4, $4)"
    )
    await app_conn.execute(add, "contact_role", "night_desk", "Night desk", CHECKER)
    await app_conn.execute(
        "UPDATE booking.supplier_profile_option SET active = false, label_en = 'Night desk (old)',"
        " updated_at = now(), updated_by = $1 WHERE kind = 'contact_role' AND code = 'night_desk'",
        CHECKER,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.execute(add, "contact_role", "night_desk", "Again", CHECKER)
    for kind, code, label in (("vendor_tier", "gold", "Gold"), ("contact_role", "Night", "Night"),
                              ("contact_role", "x", "X"), ("contact_role", "lantern", "   ")):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):
            await app_conn.execute(add, kind, code, label, CHECKER)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.supplier_profile_option WHERE code = 'night_desk'")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(
            "UPDATE booking.supplier_profile_option SET code = 'night_shift' WHERE code = 'night_desk'"
        )


# ── Supplier profile ────────────────────────────────────────────────────────────────────


async def test_profile_shapes_and_a_pending_edit_makes_a_writer(app_conn: asyncpg.Connection) -> None:
    sid = await _pending_supplier(app_conn, "Profile")
    await app_conn.execute(
        "UPDATE booking.suppliers SET legal_entity = 'Harbourline Lodges Co., Ltd', tax_code = '0312345678',"
        ' role_contacts = \'[{"role": "accounts", "name": "Mai Lantern"}]\'::jsonb,'
        ' payment_terms = \'{"basis": "after_service", "due_days": 15}\'::jsonb, updated_by = $2'
        " WHERE id = $1::uuid",
        sid,
        CHECKER,
    )
    writers = await app_conn.fetchval("SELECT writers FROM booking.suppliers WHERE id = $1::uuid", sid)
    assert CHECKER in writers  # the profile is data: its editor never activates the supplier
    for bad in ("role_contacts = '{}'::jsonb", "payment_terms = '[]'::jsonb", "tax_code = '   '",
                "role_contacts = (SELECT jsonb_agg(i) FROM generate_series(1, 13) i)"):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError, match="suppliers_profile_chk"):
            await app_conn.execute(f"UPDATE booking.suppliers SET {bad} WHERE id = $1::uuid", sid)  # noqa: S608


# ── Billing entity backstop ─────────────────────────────────────────────────────────────────────────────────────


async def test_billing_entity_bank_text_changes_only_through_an_approved_change(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="no payment details"):
        await app_conn.execute(
            "INSERT INTO booking.billing_entity (code, legal_entity, payment_instructions) VALUES ('IT1158BX',"
            " 'Lanternfield Travel Ltd', 'Bank: invented')"
        )
    entity = await app_conn.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity) VALUES ('IT1158BA', 'Lanternfield Travel Ltd')"
        " RETURNING id::text"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="only by an approved change"):
        await app_conn.execute(
            "UPDATE booking.billing_entity SET payment_instructions = 'Bank: invented' WHERE id = $1::uuid", entity
        )
    change = await app_conn.fetchval(
        "INSERT INTO booking.billing_entity_change (billing_entity_id, set_payment_instructions, payment_instructions,"
        " reason, requested_by) VALUES ($1::uuid, true, 'Bank: invented 2', 'new bank', $2) RETURNING id::text",
        entity,
        MAKER,
    )
    async with app_conn.transaction():
        await app_conn.execute(
            "UPDATE booking.billing_entity_change SET status = 'approved', decided_by = $2, decided_at = now()"
            " WHERE id = $1::uuid",
            change,
            CHECKER,
        )
        with pytest.raises(asyncpg.CheckViolationError, match="only by an approved change"):
            async with app_conn.transaction():  # another text than the approved one
                await app_conn.execute(
                    "UPDATE booking.billing_entity SET payment_instructions = 'Bank: other' WHERE id = $1::uuid",
                    entity,
                )
        await app_conn.execute(
            "UPDATE booking.billing_entity SET payment_instructions = 'Bank: invented 2' WHERE id = $1::uuid", entity
        )
    # The approved change is old now: the same text again in a new transaction is a replay and refused.
    with pytest.raises(asyncpg.CheckViolationError, match="only by an approved change"):
        await app_conn.execute(
            "UPDATE booking.billing_entity SET payment_instructions = NULL WHERE id = $1::uuid", entity
        )


async def test_the_default_flag_moves_only_with_an_approved_change(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    entity = await app_conn.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity) VALUES ('IT1158BD', 'Lanternfield Travel Ltd')"
        " RETURNING id::text"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="only by an approved change"):
        await app_conn.execute("UPDATE booking.billing_entity SET is_default = true WHERE id = $1::uuid", entity)
    current = await admin.fetchval("SELECT id::text FROM booking.billing_entity WHERE is_default")
    if current is not None:
        with pytest.raises(asyncpg.CheckViolationError, match="moves only by approving another entity"):
            await app_conn.execute("UPDATE booking.billing_entity SET is_default = false WHERE id = $1::uuid", current)


# ── Supplier bank accounts ──────────────────────────────────────────────────────────────────────────────────────


async def test_an_account_starts_pending_and_what_was_entered_never_changes(app_conn: asyncpg.Connection) -> None:
    sid = await _pending_supplier(app_conn, "Bank A")
    with pytest.raises(asyncpg.CheckViolationError, match="starts pending"):
        await app_conn.execute(
            "INSERT INTO booking.supplier_bank_account (supplier_id, status, bank_name, currency, account_last4,"
            " ciphertext, wrapped_key, kms_key_id, reason, entered_by, approved_by, approved_at, callback_note)"
            " VALUES ($1::uuid, 'approved', 'X Bank', 'USD', '1234', $2, $3, 'k', 'r', $4, $5, now(), 'c')",
            sid,
            FILLER,
            WRAPPED,
            MAKER,
            CHECKER,
        )
    acc = await app_conn.fetchval(ENTER, sid, FILLER, WRAPPED, MAKER)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(
            "UPDATE booking.supplier_bank_account SET ciphertext = $2 WHERE id = $1::uuid", acc, b"\x03" * 48
        )
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.fetchval(ENTER, sid, FILLER, WRAPPED, CHECKER)  # one pending per supplier


async def test_a_second_person_approves_with_a_call_back(app_conn: asyncpg.Connection) -> None:
    sid = await _pending_supplier(app_conn, "Bank B")
    acc = await app_conn.fetchval(ENTER, sid, FILLER, WRAPPED, MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="maker_checker_chk"):
        await app_conn.execute(APPROVE, acc, MAKER, False)
    with pytest.raises(asyncpg.CheckViolationError, match="is not the business owner"):
        await app_conn.execute(APPROVE, acc, MAKER, True)  # a forged flag
    with pytest.raises(asyncpg.CheckViolationError, match="status_chk"):
        await app_conn.execute(
            "UPDATE booking.supplier_bank_account SET status = 'approved', approved_by = $2, approved_at = now()"
            " WHERE id = $1::uuid",
            acc,
            CHECKER,
        )  # no call-back note
    await app_conn.execute(APPROVE, acc, CHECKER, False)
    with pytest.raises(asyncpg.CheckViolationError, match="only pending"):
        await app_conn.execute(
            "UPDATE booking.supplier_bank_account SET status = 'rejected', closed_by = $2, closed_at = now(),"
            " close_reason = 'late' WHERE id = $1::uuid",
            acc,
            PAYER,
        )
    # A newer account replaces it: superseded keeps its approval.
    newer = await app_conn.fetchval(ENTER, sid, FILLER, WRAPPED, MAKER)
    async with app_conn.transaction():
        await app_conn.execute(
            "UPDATE booking.supplier_bank_account SET status = 'superseded', closed_by = $2, closed_at = now()"
            " WHERE id = $1::uuid",
            acc,
            PAYER,
        )
        await app_conn.execute(APPROVE, newer, PAYER, False)
    rows = await app_conn.fetch(
        "SELECT status, approved_by FROM booking.supplier_bank_account WHERE supplier_id = $1::uuid ORDER BY status",
        sid,
    )
    assert [dict(r) for r in rows] == [
        {"status": "approved", "approved_by": PAYER},
        {"status": "superseded", "approved_by": CHECKER},
    ]


async def test_the_business_owner_approves_her_own_entry_flagged(app_conn: asyncpg.Connection) -> None:
    sid = await _pending_supplier(app_conn, "Bank C")
    acc = await app_conn.fetchval(ENTER, sid, FILLER, WRAPPED, OWNER)
    with pytest.raises(asyncpg.CheckViolationError, match="maker_checker_chk|owner_self_chk"):
        await app_conn.execute(APPROVE, acc, OWNER, False)
    await app_conn.execute(APPROVE, acc, OWNER, True)
    assert await app_conn.fetchval(
        "SELECT owner_self_approved FROM booking.supplier_bank_account WHERE id = $1::uuid", acc
    )


async def test_reject_and_withdraw(app_conn: asyncpg.Connection) -> None:
    sid = await _pending_supplier(app_conn, "Bank D")
    acc = await app_conn.fetchval(ENTER, sid, FILLER, WRAPPED, MAKER)
    reject = (
        "UPDATE booking.supplier_bank_account SET status = $3, closed_by = $2, closed_at = now(),"
        " close_reason = 'number does not match the call-back' WHERE id = $1::uuid"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="status_chk"):
        await app_conn.execute(reject, acc, MAKER, "rejected")  # the enterer withdraws, never rejects
    with pytest.raises(asyncpg.CheckViolationError, match="status_chk"):
        await app_conn.execute(reject, acc, CHECKER, "withdrawn")  # only the enterer withdraws
    await app_conn.execute(reject, acc, CHECKER, "rejected")
    with pytest.raises(asyncpg.CheckViolationError, match="only pending"):
        await app_conn.execute(APPROVE, acc, PAYER, False)


async def test_free_texts_can_be_redacted_alone(app_conn: asyncpg.Connection, admin: asyncpg.Connection) -> None:
    sid = await _pending_supplier(app_conn, "Bank E")
    acc = await app_conn.fetchval(ENTER, sid, FILLER, WRAPPED, MAKER)
    await app_conn.execute(APPROVE, acc, CHECKER, False)
    async with admin.transaction():  # an erasure run-script as the owner; the app has no UPDATE on reason
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "UPDATE booking.supplier_bank_account SET reason = '[redacted]', callback_note = '[redacted]'"
            " WHERE id = $1::uuid",
            acc,
        )
    with pytest.raises(asyncpg.CheckViolationError, match="only pending"):
        await app_conn.execute(
            "UPDATE booking.supplier_bank_account SET callback_note = 'rewritten' WHERE id = $1::uuid", acc
        )
    await app_conn.execute(
        "UPDATE booking.supplier_bank_account SET callback_note = '[redacted]' WHERE id = $1::uuid", acc
    )


# ── The payout lock ─────────────────────────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def world(admin: asyncpg.Connection) -> dict[str, Any]:
    sup = await ops.supply(admin, "it-1158b Kestrel")
    case = await ops.sold_case(admin, sup, tag="bk067")
    bookings = [await ops.confirmed_booking(admin, case, sup["supplier"], net="100.00") for _ in range(3)]
    return {"supplier": sup["supplier"], "bookings": bookings}


async def _approved_bill(conn: asyncpg.Connection, world: dict[str, Any], n: int) -> str:
    line = ((world["bookings"][n], "100"),)
    made = await ops.bill(conn, world["supplier"], by=MAKER, amount="100.00", number=f"IT1158B-{n}", lines=line)
    await ops.approve_bill(conn, made["id"], by=CHECKER)
    return made["id"]


async def _age(admin: asyncpg.Connection, acc: str, hours: int) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute(
            "UPDATE booking.supplier_bank_account SET entered_at = entered_at - make_interval(hours => $2),"
            " approved_at = approved_at - make_interval(hours => $2) WHERE id = $1::uuid",
            acc,
            hours,
        )


async def test_no_payout_while_pending_or_within_the_lock(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection, world: dict[str, Any]
) -> None:
    first = await _approved_bill(app_conn, world, 0)
    await ops.payout(app_conn, first, "50.00", by=PAYER, tag="it1158b-free")  # no bank account on file: unchanged
    acc = await app_conn.fetchval(ENTER, world["supplier"], FILLER, WRAPPED, MAKER)
    with pytest.raises(asyncpg.CheckViolationError, match="bank_account_pending"):
        await ops.payout(app_conn, first, "10.00", by=PAYER, tag="it1158b-pending")
    await app_conn.execute(APPROVE, acc, CHECKER, False)
    with pytest.raises(asyncpg.CheckViolationError, match="bank_payout_locked"):
        await ops.payout(app_conn, first, "10.00", by=PAYER, tag="it1158b-locked")
    await _age(admin, acc, 23)
    with pytest.raises(asyncpg.CheckViolationError, match="bank_payout_locked"):
        await ops.payout(app_conn, first, "10.00", by=PAYER, tag="it1158b-23h")
    await _age(admin, acc, 2)
    await ops.payout(app_conn, first, "10.00", by=PAYER, tag="it1158b-25h")


async def _as_owner(admin: asyncpg.Connection, *sql: tuple[Any, ...]) -> list[Any]:
    """Statements as the schema owner with the policy guards off (committed: the app connection must see them)."""
    out = []
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("ALTER TABLE booking.pricing_policy DISABLE TRIGGER USER")
        await admin.execute("ALTER TABLE booking.pricing_policy_events DISABLE TRIGGER USER")
        for query, *args in sql:
            out.append(await admin.fetch(query, *args))
        await admin.execute("ALTER TABLE booking.pricing_policy ENABLE TRIGGER USER")
        await admin.execute("ALTER TABLE booking.pricing_policy_events ENABLE TRIGGER USER")
    return out


async def test_no_lock_value_in_force_refuses(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection, world: dict[str, Any]
) -> None:
    bill = await _approved_bill(app_conn, world, 1)
    acc = await app_conn.fetchval(ENTER, world["supplier"], FILLER, WRAPPED, MAKER)
    await app_conn.execute(APPROVE, acc, CHECKER, False)
    await _age(admin, acc, 200)
    (taken,) = await _as_owner(
        admin,
        ("DELETE FROM booking.pricing_policy_events WHERE event = 'approved' AND policy_id IN"
         " (SELECT id FROM booking.pricing_policy WHERE param_key = $1) RETURNING *", LOCK_KEY),
    )  # fmt: skip
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="bank_payout_lock_unavailable"):
            await ops.payout(app_conn, bill, "10.00", by=PAYER, tag="it1158b-nolock")
    finally:
        cols = list(taken[0].keys())
        marks = ", ".join(f"${i + 1}" for i in range(len(cols)))
        await _as_owner(
            admin,
            *[(f"INSERT INTO booking.pricing_policy_events ({', '.join(cols)}) VALUES ({marks})", *r.values())  # noqa: S608
              for r in taken],
        )  # fmt: skip


async def test_the_lock_reads_the_value_in_force(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection, world: dict[str, Any]
) -> None:
    bill = await _approved_bill(app_conn, world, 2)
    acc = await app_conn.fetchval(ENTER, world["supplier"], FILLER, WRAPPED, MAKER)
    await app_conn.execute(APPROVE, acc, CHECKER, False)
    await _age(admin, acc, 30)
    ((made,),) = await _as_owner(
        admin,
        ("INSERT INTO booking.pricing_policy (param_key, version_no, replaces_id, value, unit, effective_from,"
         " reason, created_by) VALUES ($1, 2, '00000000-0000-4000-8000-0000b6700000', '48'::jsonb, 'hours',"
         " (now() AT TIME ZONE 'UTC')::date, 'test', 'it-1158b') RETURNING id", LOCK_KEY),
    )  # fmt: skip
    try:
        await _as_owner(
            admin,
            ("INSERT INTO booking.pricing_policy_events (policy_id, maker, event, actor)"
             " VALUES ($1, 'it-1158b', 'approved', 'it-1158b-approver')", made["id"]),
        )  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError, match="bank_payout_locked"):
            await ops.payout(app_conn, bill, "10.00", by=PAYER, tag="it1158b-48h")
    finally:
        await _as_owner(
            admin,
            ("DELETE FROM booking.pricing_policy_events WHERE policy_id = $1", made["id"]),
            ("DELETE FROM booking.pricing_policy WHERE id = $1", made["id"]),
        )
    await ops.payout(app_conn, bill, "10.00", by=PAYER, tag="it1158b-24h")  # back to 24 h: 30 h passed
