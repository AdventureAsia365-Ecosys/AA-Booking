"""AA-996 (bk-032) on a real PostgreSQL: registration and re-run no-op, owner, grants (role ``booking`` writes
``trip_plans.supplier_id`` and replaces ``trip_plan_prices`` as a set: SELECT, INSERT, DELETE, never UPDATE or
TRUNCATE; ``cis_app`` reads nothing), the CHECKs and FKs, and the DM-6.1 tags of the commercial columns. The HTTP flows
are in ``test_bk032_trip_plan_prices_api.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops
from tests.integration.supply import purge_supply

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "032_trip_plan_supplier_prices.sql"
STAFF = "it-996-prices"
SOURCE = "aa996032-0000-4000-8000-000000000001"


async def _purge(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.execute(
            "DELETE FROM booking.trip_plan_prices WHERE trip_plan_id IN"
            " (SELECT id FROM booking.trip_plans WHERE source_id = $1)",
            SOURCE,
        )
        await conn.execute("UPDATE booking.trip_plans SET supplier_id = NULL WHERE source_id = $1", SOURCE)
    finally:
        await conn.execute("RESET ROLE")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (STAFF, "admin"))
    try:
        yield conn
    finally:
        await _purge(conn)
        await purge_supply(conn)
        await conn.execute("SET ROLE bk_owner")
        try:
            await conn.execute("DELETE FROM booking.suppliers WHERE company_name = 'it-682 Prices'")
        finally:
            await conn.execute("RESET ROLE")
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _plan(conn: asyncpg.Connection) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.trip_plans (source_id, sellable, created_by, updated_by) VALUES ($1, true, $2, $2)"
        " ON CONFLICT (source_system, source_id) DO UPDATE SET sellable = true RETURNING id::text",
        SOURCE,
        STAFF,
    )


ROW_SQL = (
    "INSERT INTO booking.trip_plan_prices (trip_plan_id, position, channel, season, valid_from, valid_to, pax_min,"
    " pax_max, currency, amount, net_amount, net_currency, effective_margin, priced_on, overridden, created_by)"
    " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) RETURNING id::text"
)


def _row(plan: str, **over: object) -> list[object]:
    row: dict[str, object] = {
        "position": 1,
        "channel": "b2c_tailor_made",
        "season": "Low",
        "valid_from": date(2027, 1, 1),
        "valid_to": date(2027, 3, 31),
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD",
        "amount": Decimal("1450"),
        "net_amount": None,
        "net_currency": None,
        "effective_margin": None,
        "priced_on": None,
        "overridden": False,
    }
    row.update(over)
    return [plan, *row.values(), STAFF]


async def test_registered_rerunnable_and_owned(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-032'") == 1
    owner = await admin.fetchval(
        "SELECT relowner::regrole::text FROM pg_class WHERE oid = 'booking.trip_plan_prices'::regclass"
    )
    assert owner == "bk_owner"
    fks = await admin.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'booking.trip_plans'::regclass AND contype = 'f'"
        " AND confrelid = 'booking.suppliers'::regclass"
    )
    assert fks == 1  # the re-run did not add a second FK
    assert not await admin.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'booking.trip_plan_prices'::regclass AND NOT tgisinternal"
    )


async def test_grants(admin: asyncpg.Connection) -> None:
    table = "booking.trip_plan_prices"
    for privilege in ("SELECT", "INSERT", "DELETE"):
        assert await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", table, privilege), privilege
    for privilege in ("UPDATE", "TRUNCATE"):
        assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", table, privilege), privilege
    assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", table)
    for privilege in ("INSERT", "UPDATE"):
        assert await admin.fetchval(
            "SELECT has_column_privilege('booking', 'booking.trip_plans', 'supplier_id', $1)", privilege
        )
    assert not await admin.fetchval("SELECT has_table_privilege('booking', 'booking.trip_plans', 'DELETE')")


async def test_app_replaces_the_set_and_links_a_supplier(
    app_conn: asyncpg.Connection, admin: asyncpg.Connection
) -> None:
    plan = await _plan(app_conn)
    supplier = (await ops.supply(admin, "Prices"))["supplier"]
    await app_conn.execute("UPDATE booking.trip_plans SET supplier_id = $1::uuid WHERE id = $2::uuid", supplier, plan)
    assert (
        await app_conn.fetchval("SELECT supplier_id::text FROM booking.trip_plans WHERE id = $1::uuid", plan)
        == supplier
    )

    async with app_conn.transaction():
        await app_conn.execute(ROW_SQL, *_row(plan))
        priced = {
            "position": 2,
            "pax_min": 3,
            "pax_max": None,
            "amount": Decimal("1200"),
            "net_amount": Decimal("820000"),
            "net_currency": "VND",
            "effective_margin": Decimal("0.2750"),
            "priced_on": date(2026, 10, 28),
        }
        await app_conn.execute(ROW_SQL, *_row(plan, **priced))
    assert (
        await app_conn.fetchval("SELECT count(*) FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid", plan)
        == 2
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE booking.trip_plan_prices SET amount = 1 WHERE trip_plan_id = $1::uuid", plan)
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.execute(ROW_SQL, *_row(plan, position=2))
    # The PUT replaces the set: delete + insert in one transaction.
    async with app_conn.transaction():
        await app_conn.execute("DELETE FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid", plan)
        await app_conn.execute(ROW_SQL, *_row(plan, amount=Decimal("1500")))
    assert (
        await app_conn.fetchval("SELECT amount::text FROM booking.trip_plan_prices WHERE trip_plan_id = $1::uuid", plan)
        == "1500.00"
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute(
            "UPDATE booking.trip_plans SET supplier_id = gen_random_uuid() WHERE id = $1::uuid", plan
        )


@pytest.mark.parametrize(
    "over",
    [
        {"position": 0},
        {"channel": "retail"},
        {"season": "  "},
        {"valid_to": date(2026, 12, 31)},
        {"pax_min": 0},
        {"pax_min": 4, "pax_max": 3},
        {"pax_max": 1000},
        {"currency": "usd"},
        {"amount": Decimal("0")},
        {"net_amount": Decimal("100")},  # net without its currency and pricing day
        {"net_amount": Decimal("-1"), "net_currency": "USD", "priced_on": date(2026, 10, 28)},
        {"effective_margin": Decimal("0.3")},  # a margin without a net cost
        {"overridden": True},  # an override of nothing (no net cost)
    ],
)
async def test_checks(app_conn: asyncpg.Connection, over: dict[str, object]) -> None:
    plan = await _plan(app_conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(ROW_SQL, *_row(plan, **over))


async def test_commercial_columns_are_tagged(admin: asyncpg.Connection) -> None:
    columns = {
        "booking.trip_plans": ("supplier_id",),
        "booking.trip_plan_prices": ("amount", "net_amount", "net_currency", "effective_margin"),
    }
    for rel, cols in columns.items():
        for col in cols:
            comment = await admin.fetchval(
                "SELECT col_description($1::regclass, (SELECT attnum FROM pg_attribute"
                " WHERE attrelid = $1::regclass AND attname = $2))",
                rel,
                col,
            )
            assert comment and comment.startswith("conf:commercial; "), (rel, col, comment)
