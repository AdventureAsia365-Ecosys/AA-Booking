"""AA-972 (bk-019) schema on a real PostgreSQL: registration, re-run no-op, the settings row, grants (quotes
append-only, partners never deleted, components replace-set), owner bk_owner, and no ``supplier_net`` column anywhere
in schema booking (ADR 0005a decision 2, Fix B19)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "019_b2b_partners_quotes.sql"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-019'") == 1
    before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before
    sql = "SELECT value::text FROM booking.settings WHERE key = 'b2b_quote_valid_hours'"  # gitleaks:allow
    assert await admin.fetchval(sql) == "48"


async def test_grants(admin: asyncpg.Connection) -> None:
    def has(table: str, priv: str) -> Any:
        return admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", f"booking.{table}", priv)

    for table in ("b2b_quotes", "b2b_quote_lines"):
        assert await has(table, "SELECT") and await has(table, "INSERT")
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not await has(table, priv), (table, priv)
    assert await has("b2b_partners", "UPDATE") and not await has("b2b_partners", "DELETE")
    assert await has("trip_plan_components", "DELETE") and not await has("trip_plan_components", "UPDATE")
    owners = await admin.fetch(
        "SELECT c.relname, c.relowner::regrole::text AS owner FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'booking'"
        " AND c.relname IN ('b2b_partners', 'b2b_quotes', 'b2b_quote_lines', 'trip_plan_components')"
    )
    assert {r["owner"] for r in owners} == {"bk_owner"} and len(owners) == 4


async def test_no_supplier_net_column_in_booking(admin: asyncpg.Connection) -> None:
    """Fix B19: the agency-facing price is b2b_price; nothing in schema booking is named supplier_net."""
    names = await admin.fetch(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'booking' AND column_name ILIKE '%supplier_net%'"
    )
    assert names == []
