"""AA-973 (bk-020) schema on a real PostgreSQL: registration, re-run no-op, the settings row, grants (nothing deleted
by the app), owner bk_owner, the B2B trip-case rule (since bk-045, AA-1046: a new case is b2b with its agency set, a
b2c case is history), source_system 'crm', one active hold set per proposal version, and no personal-data-looking
column."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "020_departures_holds.sql"
TABLES = ("departures", "b2b_holds", "b2b_hold_lines")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-020'") == 1
    if await admin.fetchval("SELECT 1 FROM shared.schema_versions WHERE version = 'bk-045'"):
        pytest.skip("bk-020 alters trip_cases.customer_id, which bk-045 dropped (AA-1046): superseded, never re-run")
    before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
    assert await admin.fetchval("SELECT value::text FROM booking.settings WHERE key = 'b2b_hold_hours'") == "48"


async def test_grants_and_owner(admin: asyncpg.Connection) -> None:
    def has(table: str, priv: str) -> Any:
        return admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", f"booking.{table}", priv)

    for table in TABLES:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            assert await has(table, priv), (table, priv)
        for priv in ("DELETE", "TRUNCATE"):
            assert not await has(table, priv), (table, priv)
    owners = await admin.fetch(
        "SELECT c.relname, c.relowner::regrole::text AS owner FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'booking' AND c.relname = ANY($1::text[])",
        list(TABLES),
    )
    assert {r["owner"] for r in owners} == {"bk_owner"} and len(owners) == len(TABLES)


class _Rollback(Exception):
    """Raised at the end of a test transaction so its rows are rolled back."""


async def test_trip_case_channel_rule(admin: asyncpg.Connection) -> None:
    """bk-020's rule as bk-045 left it (AA-1046): a new case is a B2B case of an agency; a b2c case is history (created
    before the B2C retirement), never with an agency."""
    tenant = str(uuid.uuid4())
    before_drop = "now() - interval '400 days'"
    try:
        async with admin.transaction():
            b2b = await admin.fetchval(
                "INSERT INTO booking.trip_cases (status, source_system, source_id, channel, party_tenant_id)"
                " VALUES ('confirmed', 'crm', 'prop-1', 'b2b', $1::uuid) RETURNING id::text",
                tenant,
            )
            assert b2b
            kept = await admin.fetchval(
                "INSERT INTO booking.trip_cases (status, source_system, created_at)"
                f" VALUES ('new_inquiry', 'manual', {before_drop}) RETURNING channel"  # noqa: S608 — test literal
            )
            assert kept == "b2c"  # the default channel, as history
            bad = (
                ("'b2c'", "NULL", "now()"),  # a new b2c case
                ("'b2b'", "NULL", "now()"),  # b2b without the agency
                ("'b2c'", "$1::uuid", before_drop),  # b2c with an agency
            )
            for channel_sql, party_sql, created_sql in bad:
                sql = (
                    "INSERT INTO booking.trip_cases (status, source_system, channel, party_tenant_id, created_at)"
                    f" VALUES ('confirmed', 'crm', {channel_sql}, {party_sql}, {created_sql})"  # noqa: S608 — test literals
                )
                with pytest.raises(asyncpg.CheckViolationError):
                    async with admin.transaction():
                        await admin.execute(sql, *([tenant] if "$1" in sql else []))
            with pytest.raises(asyncpg.CheckViolationError):
                async with admin.transaction():
                    await admin.execute(
                        "INSERT INTO booking.trip_cases (status, source_system, channel, party_tenant_id)"
                        " VALUES ('new_inquiry', 'agency', 'b2b', $1::uuid)",
                        tenant,
                    )
            raise _Rollback
    except _Rollback:
        pass


async def test_one_active_hold_set_per_proposal_version(admin: asyncpg.Connection) -> None:
    tenant = str(uuid.uuid4())
    try:
        async with admin.transaction():
            partner = await admin.fetchval(
                "INSERT INTO booking.b2b_partners (tenant_id, agency_name, m2m_client_id)"
                " VALUES ($1::uuid, 'Schema Agency', 'client020') RETURNING id::text",
                tenant,
            )
            quote = await admin.fetchval(
                "INSERT INTO booking.b2b_quotes (partner_id, m2m_client_id, proposal_ref, proposal_version, currency,"
                " fx_rate_per_usd, fx_as_of, fx_source, pricing_rule_valid_from, margin_applied, card_fee_pct,"
                " rounding, priced_on, total_b2b_price, price_hash, valid_until)"
                " VALUES ($1::uuid, 'client020', 'p-1', 1, 'USD',"
                " 1, current_date, 'base', current_date, 0.15, 0.03, 'line_ceil_0.01', current_date, 10, $2,"
                " now() + interval '1 day') RETURNING id::text",
                partner,
                "a" * 64,
            )
            expires = datetime.now(UTC) + timedelta(hours=1)

            async def hold(key: str, status: str = "active") -> str:
                extra = {"released": ", released_at", "expired": ", expired_at"}.get(status, "")
                value = ", now()" if extra else ""
                return await admin.fetchval(
                    "INSERT INTO booking.b2b_holds (partner_id, m2m_client_id, quote_id, proposal_ref,"
                    f" proposal_version, idempotency_key, status, expires_at{extra})"  # noqa: S608
                    " VALUES ($1::uuid, 'client020', $2::uuid, 'p-1', 1,"
                    f" $3, $4, $5{value}) RETURNING id::text",
                    partner,
                    quote,
                    key,
                    status,
                    expires,
                )

            await hold("key-released", "released")
            await hold("key-expired-1", "expired")
            await hold("key-active-1")
            with pytest.raises(asyncpg.UniqueViolationError):
                async with admin.transaction():
                    await hold("key-active-2")
            with pytest.raises(asyncpg.UniqueViolationError):  # Idempotency-Key unique per partner
                async with admin.transaction():
                    await hold("key-active-1", "released")
            with pytest.raises(asyncpg.CheckViolationError):  # a confirmed hold names its trip case
                async with admin.transaction():
                    await admin.execute(
                        "UPDATE booking.b2b_holds SET status = 'confirmed' WHERE idempotency_key = 'key-active-1'"
                    )
            raise _Rollback
    except _Rollback:
        pass


async def test_no_personal_data_columns_in_new_tables(admin: asyncpg.Connection) -> None:
    """Seats are counts; no name, e-mail, phone, passport or birth date on the B2B hold path (ADR 0008a d3)."""
    cols = await admin.fetch(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'booking'"
        " AND table_name = ANY($1::text[])",
        list(TABLES),
    )
    banned = ("name", "email", "phone", "passport", "birth", "dob", "traveller", "traveler", "supplier_net")
    assert [r for r in cols if any(b in r["column_name"] for b in banned)] == []
