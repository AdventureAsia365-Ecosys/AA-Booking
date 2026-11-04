"""bk-040 (AA-1029, the Content studio Marketplace "from" price) on a real PostgreSQL: a contract file (no expand
header), registered and re-runnable, cis_app reads booking.v_from_prices (price.from.v1) and still nothing behind it,
the read returns the public suggested price only, and the guard refuses a runner other than bk_owner.

Logged in as the non-superuser aa_cis_admin (member of bk_owner and cis_app, tests/integration/bootstrap_roles.py), as
on RDS. Price rows are written by role booking and removed by the fixture."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK040 = Path(__file__).resolve().parents[3] / "migrations" / "040_from_prices_cis_reader.sql"
SOURCE = "aa102940-0000-4000-8000-000000000001"
TODAY = datetime.now(UTC).date()
PRICE_TABLES = (
    "booking.trip_plans",
    "booking.trip_plan_prices",
    "booking.trip_plan_price_history",
    "booking.pricing_rules",
)


async def _purge(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.execute(
            "DELETE FROM booking.trip_plan_prices WHERE trip_plan_id IN"
            " (SELECT id FROM booking.trip_plans WHERE source_id = $1)",
            SOURCE,
        )
    finally:
        await conn.execute("RESET ROLE")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
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


async def test_file_is_a_contract_file() -> None:
    """No `-- migrate:` directive: the cis_app grant is a cross-owner grant (root ADR 0017 'What stays manual')."""
    assert not [ln for ln in BK040.read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith("-- migrate:")]


async def test_registered_and_rerunnable(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(BK040.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-040'") == 1


async def test_cis_app_reads_the_view_and_nothing_behind_it(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT has_schema_privilege('cis_app', 'booking', 'USAGE')")
    assert await admin.fetchval("SELECT has_table_privilege('cis_app', 'booking.v_from_prices', 'SELECT')")
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not await admin.fetchval(
            "SELECT has_table_privilege('cis_app', 'booking.v_from_prices', $1)", privilege
        ), privilege
    for table in PRICE_TABLES:
        assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", table), table


async def test_cis_app_reads_the_public_price_only(app_conn: asyncpg.Connection, admin: asyncpg.Connection) -> None:
    plan = await app_conn.fetchval(
        "INSERT INTO booking.trip_plans (source_id, sellable) VALUES ($1, false)"
        " ON CONFLICT (source_system, source_id) DO UPDATE SET sellable = false RETURNING id::text",
        SOURCE,
    )
    await app_conn.execute(
        "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, valid_from, valid_to,"
        " pax_min, pax_max, currency, amount, rrp_amount, net_amount, net_currency, priced_on, computed)"
        " VALUES ($1::uuid, 1, 'b2b', 'Low', $2, $3, 2, 2, 'USD', 160, 200, 120, 'USD', $2, true)",
        plan,
        TODAY,
        TODAY + timedelta(days=60),
    )
    await admin.execute("SET ROLE cis_app")
    try:
        rows = await admin.fetch(
            "SELECT source_id, amount, currency, basis, pax FROM booking.v_from_prices"
            " WHERE source_system = 'cis' AND source_id = ANY($1::text[])",
            [SOURCE],
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.fetch("SELECT 1 FROM booking.trip_plan_prices LIMIT 1")
    finally:
        await admin.execute("RESET ROLE")
    assert [dict(r) for r in rows] == [
        {"source_id": SOURCE, "amount": Decimal("200.00"), "currency": "USD", "basis": "per_person", "pax": 2}
    ]  # the RRP, never the agency price (160) or the net (120)


async def test_guard_refuses_a_runner_other_than_bk_owner(admin: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="expected to run as bk_owner"):
        await admin.execute(BK040.read_text(encoding="utf-8"))
    await admin.execute("ROLLBACK")
