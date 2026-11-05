"""bk-041 / bk-042 (AA-1037 R4 slice 1, root ADR 0019 d9) on a real PostgreSQL: the agency billing tables, their
database rules and the CIS contract view booking.v_tenant_billing_status.

bk-041 is an expand file (new tables, seed rows into them), bk-042 a contract file (triggers, REVOKEs, a view and the
cis_app grant). Every test runs in one transaction logged in as role booking and is rolled back, so no row
outlives it (the guards refuse deleting issued invoices or payments, on purpose). Names are invented."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"
BK041 = MIGRATIONS / "041_agency_billing_tables.sql"
BK042 = MIGRATIONS / "042_agency_billing_guards_status_view.sql"
TODAY = date.today()
CLIENT = "aa1037client"


@pytest_asyncio.fixture
async def conn(migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(migrated_booking_dsn)
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _partner(c: asyncpg.Connection, name: str = "Juniper Ridge Travel") -> uuid.UUID:
    tid = uuid.uuid4()
    await c.execute(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1, $2, 'active', 'USD', $3)",
        tid,
        name,
        CLIENT,
    )
    return tid


async def _entity(c: asyncpg.Connection) -> uuid.UUID:
    return await c.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity, country_iso2) VALUES ($1, 'Lantern Bay Ltd', 'VN')"
        " RETURNING id",
        f"T{uuid.uuid4().hex[:8].upper()}",
    )


async def _draft(c: asyncpg.Connection, tid: uuid.UUID, *, kind: str = "saas", total: str = "100.00",
                 period: date = date(2026, 11, 1)) -> uuid.UUID:  # fmt: skip
    inv = await c.fetchval(
        "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency,"
        " subtotal, total, source, created_by, idempotency_key)"
        " VALUES ($1, $2, $3, $4, 'USD', $5, $5, 'job', 'job:test', $6) RETURNING id",
        tid,
        kind,
        period,
        period + timedelta(days=30),
        Decimal(total),
        f"{kind}:{tid}:{period}:USD",
    )
    role = "subscription" if kind == "saas" else "adjustment"
    await c.execute(
        "INSERT INTO booking.agency_invoice_line (invoice_id, invoice_kind, line_no, line_role, product, plan_key,"
        " description, unit_price, amount, created_by) VALUES ($1, $2, 1, $3, $4, $5, 'Plan', $6, $6, 'job:test')",
        inv,
        kind,
        role,
        "content" if kind == "saas" else None,
        "content.growth" if kind == "saas" else None,
        Decimal(total),
    )
    return inv


async def _issue(c: asyncpg.Connection, inv: uuid.UUID, entity: uuid.UUID, *, due: date, number: str) -> None:
    await c.execute(
        "UPDATE booking.agency_invoice SET status = 'issued', number = $2, issued_at = now(), issued_by = 'staff-a',"
        " issued_on = current_date, due_date = $3, billing_entity_id = $4, issuer = '{}'::jsonb,"
        " bill_to = '{}'::jsonb WHERE id = $1",
        inv,
        number,
        due,
        entity,
    )


async def _payment(c: asyncpg.Connection, inv: uuid.UUID, amount: str, *, by: str = "staff-a") -> uuid.UUID:
    return await c.fetchval(
        "INSERT INTO booking.agency_payment (invoice_id, amount, currency, method, received_on, document_ref,"
        " recorded_by, idempotency_key) VALUES ($1, $2, 'USD', 'bank_transfer', current_date, 'statement line 7',"
        " $3, $4) RETURNING id",
        inv,
        Decimal(amount),
        by,
        uuid.uuid4().hex,
    )


async def test_header_classes() -> None:
    assert BK041.read_text(encoding="utf-8").splitlines()[0] == "-- migrate: expand"
    assert not [ln for ln in BK042.read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith("-- migrate:")]


async def test_registered_and_rerunnable(migrated_admin_dsn: str) -> None:
    c = await asyncpg.connect(migrated_admin_dsn)
    try:
        await c.execute("SET ROLE bk_owner")
        await c.execute(BK041.read_text(encoding="utf-8"))
        await c.execute(BK042.read_text(encoding="utf-8"))
        await c.execute("RESET ROLE")
        n = await c.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version IN ('bk-041', 'bk-042')")
        assert n == 2
        assert await c.fetchval("SELECT count(*) FROM booking.billing_terms WHERE party_tenant_id IS NULL") == 1
        assert await c.fetchval("SELECT count(*) FROM booking.tax_rule WHERE code = 'EXCL_TAX'") == 1
    finally:
        await c.close()


async def test_seed_terms_are_policy_data(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow("SELECT * FROM booking.billing_terms WHERE party_tenant_id IS NULL")
    assert row["deposit_pct"] == Decimal("0.3000") and row["balance_days_before_departure"] == 30
    assert row["warn_after_days"] < row["suspend_after_days"] and row["saas_currency"] == "USD"


async def test_line_kind_follows_the_invoice(conn: asyncpg.Connection) -> None:
    tid = await _partner(conn)
    inv = await _draft(conn, tid)
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.agency_invoice_line (invoice_id, invoice_kind, line_no, line_role,"
                " source_hold_id, description, unit_price, amount, created_by)"
                " VALUES ($1, 'saas', 2, 'deposit', NULL, 'Deposit', 10, 10, 'x')",
                inv,
            )
    with pytest.raises(asyncpg.ForeignKeyViolationError):  # a settlement line on a saas invoice
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO booking.agency_invoice_line (invoice_id, invoice_kind, line_no, line_role,"
                " description, unit_price, amount, created_by) VALUES ($1, 'settlement', 2, 'adjustment', 'x', 1, 1,"
                " 'x')",
                inv,
            )


async def test_one_live_invoice_per_period(conn: asyncpg.Connection) -> None:
    tid = await _partner(conn)
    await _draft(conn, tid)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await _draft(conn, tid)


async def test_issued_invoice_keeps_its_facts_and_lines(conn: asyncpg.Connection) -> None:
    tid = await _partner(conn)
    entity = await _entity(conn)
    inv = await _draft(conn, tid)
    await _issue(conn, inv, entity, due=TODAY + timedelta(days=14), number="AAS-2026-000001")
    with pytest.raises(asyncpg.RestrictViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.agency_invoice SET total = 1, subtotal = 1 WHERE id = $1", inv)
    with pytest.raises(asyncpg.RestrictViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.agency_invoice_line SET description = 'x' WHERE invoice_id = $1", inv)
    with pytest.raises(asyncpg.RestrictViolationError):
        async with conn.transaction():
            await conn.execute("DELETE FROM booking.agency_invoice WHERE id = $1", inv)
    # the manual VAT e-invoice number: once
    await conn.execute(
        "UPDATE booking.agency_invoice SET e_invoice_no = 'C26TAA 0000123', e_invoice_recorded_by = 'staff-a',"
        " e_invoice_recorded_at = now() WHERE id = $1",
        inv,
    )
    with pytest.raises(asyncpg.RestrictViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.agency_invoice SET e_invoice_no = 'other' WHERE id = $1", inv)
    # staff note stays editable
    await conn.execute("UPDATE booking.agency_invoice SET staff_note = 'called the agency' WHERE id = $1", inv)


async def test_a_line_never_moves_off_its_invoice(conn: asyncpg.Connection) -> None:
    tid = await _partner(conn)
    entity = await _entity(conn)
    issued = await _draft(conn, tid)
    await _issue(conn, issued, entity, due=TODAY + timedelta(days=14), number="AAS-2026-000002")
    draft = await _draft(conn, tid, period=date(2026, 12, 1))
    # an issued invoice's line is not re-pointed at a draft of the same kind (the guard used to read NEW only)
    with pytest.raises(asyncpg.RestrictViolationError):
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.agency_invoice_line SET invoice_id = $1, line_no = 2 WHERE invoice_id = $2",
                draft,
                issued,
            )
    # a draft's line keeps its key too: not moved onto the issued invoice, not renumbered
    with pytest.raises(asyncpg.RestrictViolationError):
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.agency_invoice_line SET invoice_id = $1, line_no = 2 WHERE invoice_id = $2",
                issued,
                draft,
            )
    with pytest.raises(asyncpg.RestrictViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.agency_invoice_line SET line_no = 5 WHERE invoice_id = $1", draft)
    # the draft's other columns stay editable
    await conn.execute(
        "UPDATE booking.agency_invoice_line SET description = 'Plan, edited' WHERE invoice_id = $1", draft
    )
    assert await conn.fetchval("SELECT count(*) FROM booking.agency_invoice_line WHERE invoice_id = $1", issued) == 1


async def test_draft_is_deleted_with_its_lines(conn: asyncpg.Connection) -> None:
    tid = await _partner(conn)
    inv = await _draft(conn, tid)
    await conn.execute("DELETE FROM booking.agency_invoice WHERE id = $1", inv)
    assert await conn.fetchval("SELECT count(*) FROM booking.agency_invoice_line WHERE invoice_id = $1", inv) == 0


async def test_maker_checker_and_settle(conn: asyncpg.Connection) -> None:
    tid = await _partner(conn)
    entity = await _entity(conn)
    inv = await _draft(conn, tid, total="100.00")
    with pytest.raises(asyncpg.CheckViolationError):  # a draft takes no payment
        async with conn.transaction():
            await _payment(conn, inv, "10.00")
    await _issue(conn, inv, entity, due=TODAY + timedelta(days=14), number="AAS-2026-000002")
    first = await _payment(conn, inv, "60.00")
    with pytest.raises(asyncpg.CheckViolationError):  # the recorder never approves
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.agency_payment SET status = 'approved', approved_by = 'staff-a', approved_at = now()"
                " WHERE id = $1",
                first,
            )
    await conn.execute(
        "UPDATE booking.agency_payment SET status = 'approved', approved_by = 'staff-b', approved_at = now()"
        " WHERE id = $1",
        first,
    )
    assert await conn.fetchval("SELECT status FROM booking.agency_invoice WHERE id = $1", inv) == "issued"
    with pytest.raises(asyncpg.RestrictViolationError):  # the decision is final
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.agency_payment SET status = 'rejected', approved_by = NULL, approved_at = NULL,"
                " rejected_by = 'staff-b', rejected_at = now(), reject_reason = 'x' WHERE id = $1",
                first,
            )
    too_much = await _payment(conn, inv, "50.00")
    with pytest.raises(asyncpg.CheckViolationError):  # 60 + 50 > 100
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.agency_payment SET status = 'approved', approved_by = 'staff-b', approved_at = now()"
                " WHERE id = $1",
                too_much,
            )
    rest = await _payment(conn, inv, "40.00")
    await conn.execute(
        "UPDATE booking.agency_payment SET status = 'approved', approved_by = 'staff-c', approved_at = now()"
        " WHERE id = $1",
        rest,
    )
    assert await conn.fetchval("SELECT status FROM booking.agency_invoice WHERE id = $1", inv) == "paid"
    with pytest.raises(asyncpg.CheckViolationError):  # paid is final
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.agency_invoice SET status = 'void', voided_at = now(), voided_by = 'x',"
                " void_reason = 'x' WHERE id = $1",
                inv,
            )


async def test_paid_needs_approved_money(conn: asyncpg.Connection) -> None:
    tid = await _partner(conn)
    entity = await _entity(conn)
    inv = await _draft(conn, tid)
    await _issue(conn, inv, entity, due=TODAY, number="AAS-2026-000003")
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.agency_invoice SET status = 'paid' WHERE id = $1", inv)


async def test_void_marks_lines_and_frees_the_period(conn: asyncpg.Connection) -> None:
    tid = await _partner(conn)
    entity = await _entity(conn)
    inv = await _draft(conn, tid)
    await _issue(conn, inv, entity, due=TODAY, number="AAS-2026-000004")
    await conn.execute(
        "UPDATE booking.agency_invoice SET status = 'void', voided_at = now(), voided_by = 'staff-a',"
        " void_reason = 'wrong plan' WHERE id = $1",
        inv,
    )
    await conn.execute("UPDATE booking.agency_invoice_line SET voided = true WHERE invoice_id = $1", inv)
    await _draft(conn, tid)  # the period is free again


async def test_role_booking_cannot_delete_money_rows(conn: asyncpg.Connection) -> None:
    for table in ("agency_payment", "invoice_sequence", "tax_rule", "billing_entity"):
        assert not await conn.fetchval("SELECT has_table_privilege('booking', $1, 'DELETE')", f"booking.{table}")
    assert await conn.fetchval("SELECT has_table_privilege('booking', 'booking.agency_invoice', 'DELETE')")


async def test_sequence_is_gap_free_per_series_and_year(conn: asyncpg.Connection) -> None:
    take = (
        "INSERT INTO booking.invoice_sequence (series, year, last_no) VALUES ($1, 2099, 1)"
        " ON CONFLICT (series, year) DO UPDATE SET last_no = booking.invoice_sequence.last_no + 1 RETURNING last_no"
    )
    assert [await conn.fetchval(take, "AAS") for _ in range(3)] == [1, 2, 3]
    assert await conn.fetchval(take, "AAT") == 1
    async with conn.transaction():
        sp = conn.transaction()
        await sp.start()
        assert await conn.fetchval(take, "AAS") == 4
        await sp.rollback()  # a rolled-back issue gives its number back
    assert await conn.fetchval(take, "AAS") == 4


async def _status(conn: asyncpg.Connection, tid: uuid.UUID) -> asyncpg.Record:
    return await conn.fetchrow("SELECT * FROM booking.v_tenant_billing_status WHERE tenant_id = $1", tid)


async def test_status_view(conn: asyncpg.Connection) -> None:
    entity = await _entity(conn)
    clean, warn, late = await _partner(conn), await _partner(conn), await _partner(conn)
    a = await _draft(conn, clean)
    await _issue(conn, a, entity, due=TODAY + timedelta(days=3), number="AAS-2026-000010")
    b = await _draft(conn, warn, total="80.00")
    await _issue(conn, b, entity, due=TODAY - timedelta(days=5), number="AAS-2026-000011")
    c = await _draft(conn, late, kind="settlement", total="250.50")
    await _issue(conn, c, entity, due=TODAY - timedelta(days=31), number="AAT-2026-000001")
    s = await _status(conn, clean)
    assert (s["status"], s["oldest_overdue_days"], s["overdue_invoices"]) == ("ok", 0, 0)
    assert s["warn_after_days"] == 1 and s["suspend_after_days"] == 30
    s = await _status(conn, warn)
    assert (s["status"], s["oldest_overdue_days"], s["overdue_invoices"]) == ("warn", 5, 1)
    s = await _status(conn, late)
    assert (s["status"], s["oldest_overdue_days"]) == ("past_due", 31)
    assert s["overdue_amount"] == '{"USD": 250.50}'
    # an agency override moves the bar
    await conn.execute(
        "INSERT INTO booking.billing_terms (party_tenant_id, deposit_pct, balance_days_before_departure, saas_due_days,"
        " settlement_due_days, warn_after_days, suspend_after_days) VALUES ($1, 0.3, 30, 14, 14, 10, 60)",
        warn,
    )
    assert (await _status(conn, warn))["status"] == "ok"
    # a paid SaaS period sets paid_through
    pay = await _payment(conn, a, "100.00")
    await conn.execute(
        "UPDATE booking.agency_payment SET status = 'approved', approved_by = 'staff-b', approved_at = now()"
        " WHERE id = $1",
        pay,
    )
    assert (await _status(conn, clean))["paid_through"] == date(2026, 11, 30)


async def test_cis_app_reads_the_view_only(migrated_admin_dsn: str) -> None:
    conn = await asyncpg.connect(migrated_admin_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("SET LOCAL ROLE bk_owner")
        tid = await _partner(conn)
        await conn.execute("SET LOCAL ROLE cis_app")
        await _cis_app_reads(conn, tid)
    finally:
        await tx.rollback()
        await conn.close()


async def _cis_app_reads(conn: asyncpg.Connection, tid: uuid.UUID) -> None:
    row = await conn.fetchrow(
        "SELECT tenant_id, status, oldest_overdue_days, overdue_invoices, overdue_amount, paid_through,"
        " warn_after_days, suspend_after_days FROM booking.v_tenant_billing_status WHERE tenant_id = $1",
        tid,
    )
    assert row["status"] == "ok"
    for table in ("agency_invoice", "agency_payment", "billing_terms"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.fetch(f"SELECT 1 FROM booking.{table} LIMIT 1")  # noqa: S608
