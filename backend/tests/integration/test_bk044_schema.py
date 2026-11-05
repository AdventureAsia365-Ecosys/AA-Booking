"""bk-044 (AA-1037 R4 extras G4 / G6) on a real PostgreSQL: the reminder log of overdue agency invoices and the partner
terms clauses. An expand file (two new tables, a seed row into one of them). Every test runs in one transaction logged
in as role booking and is rolled back. Names are invented. No import of app code (the migration PR stands alone)."""

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

BK044 = Path(__file__).resolve().parents[3] / "migrations" / "044_agency_reminders_terms_clauses.sql"


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


async def _invoice(c: asyncpg.Connection) -> uuid.UUID:
    tid = uuid.uuid4()
    await c.execute(
        "INSERT INTO booking.b2b_partners (tenant_id, agency_name, status, billing_currency, m2m_client_id)"
        " VALUES ($1, 'Copperfield Trails', 'active', 'USD', 'bk044client')",
        tid,
    )
    period = date(2026, 9, 1)
    return await c.fetchval(
        "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency,"
        " subtotal, total, source, created_by, idempotency_key)"
        " VALUES ($1, 'saas', $2, $3, 'USD', $4, $4, 'job', 'job:test', $5) RETURNING id",
        tid,
        period,
        period + timedelta(days=30),
        Decimal("50.00"),
        f"saas:{tid}:{period}:USD",
    )


async def test_header_is_expand() -> None:
    lines = BK044.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "-- migrate: expand"
    assert "DO $$" not in BK044.read_text(encoding="utf-8")


async def test_registered_and_rerunnable(migrated_admin_dsn: str) -> None:
    c = await asyncpg.connect(migrated_admin_dsn)
    try:
        await c.execute("SET ROLE bk_owner")
        await c.execute(BK044.read_text(encoding="utf-8"))
        await c.execute("RESET ROLE")
        assert await c.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-044'") == 1
        assert await c.fetchval("SELECT count(*) FROM booking.partner_terms_clause WHERE code = 'price_error'") == 1
    finally:
        await c.close()


async def test_seed_clause_is_a_draft(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow("SELECT * FROM booking.partner_terms_clause WHERE code = 'price_error'")
    assert row["status"] == "draft" and row["approved_by"] is None
    assert row["free_cancel_days"] == 2 and row["version"] == 1
    assert "free of charge" in row["body"]


async def test_clause_approver_is_not_the_last_editor(conn: asyncpg.Connection) -> None:
    cid = await conn.fetchval(
        "INSERT INTO booking.partner_terms_clause (code, heading, body, updated_by)"
        " VALUES ('zz_test', 'T', 'B', 'staff-a') RETURNING id"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "UPDATE booking.partner_terms_clause SET status = 'approved', approved_by = 'staff-a',"
                " approved_at = now() WHERE id = $1",
                cid,
            )
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():  # approved needs an approver
            await conn.execute("UPDATE booking.partner_terms_clause SET status = 'approved' WHERE id = $1", cid)
    await conn.execute(
        "UPDATE booking.partner_terms_clause SET status = 'approved', approved_by = 'staff-b', approved_at = now()"
        " WHERE id = $1",
        cid,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE booking.partner_terms_clause SET free_cancel_days = 90 WHERE id = $1", cid)


async def test_reminder_once_per_step(conn: asyncpg.Connection) -> None:
    inv = await _invoice(conn)
    sql = (
        "INSERT INTO booking.agency_invoice_reminder (invoice_id, step_days, step_kind, days_overdue, outcome,"
        " skip_reason, sent_to, delivery_ref, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'job:test')"
    )
    await conn.execute(sql, inv, 7, "reminder", 8, "sent", None, "billing@copperfield.example", "msg-1")
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await conn.execute(sql, inv, 7, "reminder", 9, "sent", None, "billing@copperfield.example", "msg-2")
    await conn.execute(sql, inv, 30, "credit_stop", 30, "skipped", "no_billing_email", None, None)
    for bad in (
        (inv, 1, "reminder", 3, "sent", None, None, None),  # sent without an address
        (inv, 2, "reminder", 3, "skipped", None, None, None),  # skipped without a reason
        (inv, 3, "reminder", 2, "skipped", "no_billing_email", None, None),  # a step not yet due
        (inv, 4, "nudge", 5, "skipped", "no_billing_email", None, None),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute(sql, *bad)


async def test_role_booking_grants(conn: asyncpg.Connection) -> None:
    for table, priv, expected in (
        ("booking.agency_invoice_reminder", "INSERT", True),
        ("booking.agency_invoice_reminder", "SELECT", True),
        ("booking.partner_terms_clause", "UPDATE", True),
    ):
        assert await conn.fetchval("SELECT has_table_privilege('booking', $1, $2)", table, priv) is expected
    for table in ("booking.agency_invoice_reminder", "booking.partner_terms_clause"):
        assert not await conn.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", table)
