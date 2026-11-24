"""bk-071 (AA-1158 part 2 follow-up, tech lead S289) on a real PostgreSQL, as role ``booking`` where the app writes: a
supplier bank account is approved by the business owner, or by the tech lead (the platform owner marker ``is_owner``)
as the fallback; any other administrator, an inactive owner and a Dev-only E2E identity carrying ``is_owner`` are
refused by the database; the person who entered it stays under the bk-067 rules (only the business owner, flagged);
an approval that does not change keeps passing after the marker moves (superseding).

The markers are ``shared.staff_booking_grant_stub`` rows (the CIS contract view in the stub schema). All names are
invented; the "ciphertext" is filler bytes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "071_supplier_bank_approver.sql"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_it1158d"
MAKER, ADMIN = "it-1158d-maker", "it-1158d-admin"
# staff key -> (cognito sub, is_owner, is_business_owner, e-mail)
PEOPLE = {
    "it-1158d-owner": ("d1158071-0000-4000-8000-0000000000a1", False, True, "owner@example.test"),
    "it-1158d-lead": ("d1158071-0000-4000-8000-0000000000a2", True, False, "lead@example.test"),
    "it-1158d-e2e": ("d1158071-0000-4000-8000-0000000000a3", True, False, "e2e-owner@e2e-mail.lumiguides.it.com"),
    "it-1158d-gone": ("d1158071-0000-4000-8000-0000000000a4", True, False, "gone@example.test"),
}
OWNER, LEAD, E2E_OWNER, GONE = PEOPLE
FILLER = b"\x01" * 48
WRAPPED = b"\x02" * 184
ENTER = (
    "INSERT INTO booking.supplier_bank_account (supplier_id, bank_name, bank_country, currency, account_last4,"
    " ciphertext, wrapped_key, kms_key_id, reason, entered_by)"
    " VALUES ($1::uuid, 'Fernbrook Bank', 'TH', 'USD', '8765', $2, $3, 'arn:test:key/it-1158d', 'new account', $4)"
    " RETURNING id::text"
)
APPROVE = (
    "UPDATE booking.supplier_bank_account SET status = 'approved', approved_by = $2, approved_at = now(),"
    " callback_note = 'Called the accounts desk on the number from the signed contract', owner_self_approved = $3"
    " WHERE id = $1::uuid"
)


async def _purge(admin: asyncpg.Connection) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.supplier_bank_account WHERE supplier_id IN"
                            " (SELECT id FROM booking.suppliers WHERE company_name LIKE 'it-1158d%')")  # fmt: skip
        await admin.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-1158d%'")
    await admin.execute(
        "DELETE FROM shared.staff_booking_grant_stub WHERE cognito_sub = ANY($1::text[])",
        [p[0] for p in PEOPLE.values()],
    )


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (MAKER, "contracting"), (ADMIN, "admin"))
    for key, (sub, is_owner, business_owner, email) in PEOPLE.items():
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name, email, cognito_iss, cognito_sub)"
            " VALUES ($1, 'admin', $5, $1, $4, $2, $3) ON CONFLICT (cis_user_id) DO UPDATE SET active = $5,"
            " email = $4, cognito_iss = $2, cognito_sub = $3",
            key,
            ISS,
            sub,
            email,
            key != GONE,
        )
        await conn.execute(
            "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code, is_owner, is_business_owner)"
            " VALUES ($1, NULL, $2, $3)",
            sub,
            is_owner,
            business_owner,
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


async def _account(conn: asyncpg.Connection, name: str, by: str = MAKER) -> str:
    sid = await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, status, created_by, updated_by)"
        " VALUES ($1, ARRAY['hotel'], 'TH', 'pending', $2, $2) RETURNING id::text",
        f"it-1158d {name} {uuid.uuid4().hex[:6]}",
        by,
    )
    return await conn.fetchval(ENTER, sid, FILLER, WRAPPED, by)


async def test_registered_once_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) AS n FROM booking.v_schema_versions WHERE version = 'bk-071'") == 1
    triggers = await admin.fetchval(
        "SELECT count(*) AS n FROM pg_trigger WHERE tgrelid = 'booking.supplier_bank_account'::regclass"
        " AND tgname = 'supplier_bank_account_approver_guard'"
    )
    assert triggers == 1


async def test_contract_file_without_a_directive() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert text.startswith("-- Migration bk-071")
    assert "-- migrate:" not in text


@pytest.mark.parametrize("approver", [ADMIN, E2E_OWNER, GONE, "it-1158d-nobody"])
async def test_anyone_but_the_owner_or_the_tech_lead_is_refused(app_conn: asyncpg.Connection, approver: str) -> None:
    acc = await _account(app_conn, "Refused")
    with pytest.raises(asyncpg.CheckViolationError, match="neither the business owner nor the tech lead"):
        await app_conn.execute(APPROVE, acc, approver, False)
    assert await app_conn.fetchval("SELECT status FROM booking.supplier_bank_account WHERE id = $1::uuid", acc) == (
        "pending"
    )


@pytest.mark.parametrize("approver", [OWNER, LEAD])
async def test_the_business_owner_or_the_tech_lead_approves(app_conn: asyncpg.Connection, approver: str) -> None:
    acc = await _account(app_conn, "Approved")
    await app_conn.execute(APPROVE, acc, approver, False)
    row = await app_conn.fetchrow(
        "SELECT status, approved_by FROM booking.supplier_bank_account WHERE id = $1::uuid", acc
    )
    assert (row["status"], row["approved_by"]) == ("approved", approver)


async def test_the_person_who_entered_stays_under_the_bk067_rules(app_conn: asyncpg.Connection) -> None:
    # The tech lead never approves his own entry (the maker-checker CHECK); the business owner does, flagged.
    acc = await _account(app_conn, "Own lead", by=LEAD)
    with pytest.raises(asyncpg.CheckViolationError, match="maker_checker_chk"):
        await app_conn.execute(APPROVE, acc, LEAD, False)
    with pytest.raises(asyncpg.CheckViolationError, match="is not the business owner"):
        await app_conn.execute(APPROVE, acc, LEAD, True)
    own = await _account(app_conn, "Own owner", by=OWNER)
    await app_conn.execute(APPROVE, own, OWNER, True)


async def test_an_unchanged_approval_survives_a_marker_change(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection
) -> None:
    acc = await _account(app_conn, "Kept")
    await app_conn.execute(APPROVE, acc, LEAD, False)
    await admin.execute("UPDATE shared.staff_booking_grant_stub SET is_owner = false WHERE cognito_sub = $1",
                        PEOPLE[LEAD][0])  # fmt: skip
    await app_conn.execute(
        "UPDATE booking.supplier_bank_account SET status = 'superseded', closed_by = $2, closed_at = now()"
        " WHERE id = $1::uuid",
        acc,
        OWNER,
    )
    assert await app_conn.fetchval(
        "SELECT approved_by FROM booking.supplier_bank_account WHERE id = $1::uuid", acc
    ) == (LEAD)
