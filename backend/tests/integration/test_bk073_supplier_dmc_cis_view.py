"""bk-073 (AA-1148, the CIS content workspace links a tour to an AA supplier) on a real PostgreSQL: a contract file (no
migrate directive), registered and re-runnable; the category dmc is accepted, at most seven categories and nothing
unknown; cis_app reads booking.v_supply_suppliers (supply.supplier.v1: exactly the seven identity columns) and nothing
behind it; the guard refuses a runner other than bk_owner.

Logged in as the non-superuser aa_cis_admin (member of bk_owner and cis_app, tests/integration/bootstrap_roles.py), as
on RDS. Invented supplier names only; the rows are removed by the fixture."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import as_booking_owner

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK073 = Path(__file__).resolve().parents[3] / "migrations" / "073_supplier_dmc_cis_view.sql"
PREFIX = "it-1148 "
CONTRACT_COLUMNS = ["id", "code", "company_name", "categories", "country_code", "status", "updated_at"]


async def _purge(conn: asyncpg.Connection) -> None:
    async with as_booking_owner(conn):
        await conn.execute("DELETE FROM booking.suppliers WHERE company_name LIKE $1", PREFIX + "%")


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


async def _supplier(conn: asyncpg.Connection, name: str, categories: list[str]) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code) VALUES ($1, $2::text[], 'VN')"
        " RETURNING id::text",
        PREFIX + name,
        categories,
    )


async def test_file_is_a_contract_file() -> None:
    """No `-- migrate:` directive: a replaced CHECK and a cross-owner grant (root ADR 0017 d3)."""
    assert not [ln for ln in BK073.read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith("-- migrate:")]


async def test_registered_and_rerunnable(admin: asyncpg.Connection) -> None:
    async with as_booking_owner(admin):
        await admin.execute(BK073.read_text(encoding="utf-8"))  # a second run is a no-op
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-073'") == 1


async def test_dmc_is_a_category_and_the_list_stays_closed(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn, "Zephyr Delta Journeys", ["dmc", "hotel", "transport"])
    assert sid
    every = ["hotel", "transport", "guide", "entrance", "activity", "meal", "dmc"]
    assert await _supplier(app_conn, "Quillmoor All Services", every)
    with pytest.raises(asyncpg.CheckViolationError):
        await _supplier(app_conn, "Ombre Tours", ["tour"])
    with pytest.raises(asyncpg.CheckViolationError):
        await _supplier(app_conn, "Ombre Nothing", [])


async def test_cis_app_reads_the_identity_columns_only(app_conn: asyncpg.Connection, admin: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn, "Fennel Bay Travel", ["dmc"])
    cols = await admin.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'booking'"
        " AND table_name = 'v_supply_suppliers' ORDER BY ordinal_position"
    )
    assert [r["column_name"] for r in cols] == CONTRACT_COLUMNS
    assert await admin.fetchval("SELECT has_table_privilege('cis_app', 'booking.v_supply_suppliers', 'SELECT')")
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not await admin.fetchval(
            "SELECT has_table_privilege('cis_app', 'booking.v_supply_suppliers', $1)", privilege
        ), privilege
    await admin.execute("SET ROLE cis_app")
    try:
        row = await admin.fetchrow(
            "SELECT company_name, categories, country_code, status FROM booking.v_supply_suppliers WHERE id = $1::uuid",
            sid,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await admin.fetch("SELECT contact_email FROM booking.suppliers LIMIT 1")
    finally:
        await admin.execute("RESET ROLE")
    assert dict(row) == {
        "company_name": PREFIX + "Fennel Bay Travel",
        "categories": ["dmc"],
        "country_code": "VN",
        "status": "active",
    }


async def test_guard_refuses_a_runner_other_than_bk_owner(admin: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="expected to run as bk_owner"):
        await admin.execute(BK073.read_text(encoding="utf-8"))
    await admin.execute("ROLLBACK")
