"""bk-025 (AA-992) on a real PostgreSQL: the data coverage contract views booking.v_ops_data_totals and
booking.v_ops_trip_plan_keys. Shape (the exact column lists are the contract; the keys view exposes no
conf:commercial column), counts from real rows, and the reader grants: cis_app reads the contract views and nothing
else in schema booking, role booking reads them but cannot write them.

Logged in as the non-superuser aa_cis_admin (member of bk_owner and cis_app, tests/integration/bootstrap_roles.py), as
on RDS. Rows are written inside a transaction that is rolled back, and every assertion compares the view before and
after, so other tests' rows in the session database do not matter."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TOTALS = "booking.v_ops_data_totals"
KEYS = "booking.v_ops_trip_plan_keys"
STAFF = "it-staff-992"
BK025 = Path(__file__).resolve().parents[3] / "migrations" / "025_ops_data_coverage_views.sql"
METRICS = {
    "trip_plans", "trip_plans_sellable", "trip_plan_components", "departures_open", "suppliers_active",
    "contracts_published", "rates_current", "media_active", "media_active_with_rights", "customers",
    "b2b_partners_active", "b2b_holds_active",
}

COLUMNS_SQL = """
SELECT column_name, data_type FROM information_schema.columns
 WHERE table_schema = 'booking' AND table_name = $1 ORDER BY ordinal_position"""

COMMERCIAL_SQL = """
SELECT a.attname::text FROM pg_attribute a
 WHERE a.attrelid = 'booking.trip_plans'::regclass AND a.attnum > 0 AND NOT a.attisdropped
   AND (col_description(a.attrelid, a.attnum) LIKE 'conf:%' OR col_description(a.attrelid, a.attnum) LIKE 'pii:%')"""


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ($1, 'contracting', true)"
        " ON CONFLICT (cis_user_id) DO NOTHING",
        STAFF,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def _totals(conn: asyncpg.Connection) -> dict[str, int]:
    return {r["metric"]: r["n"] for r in await conn.fetch(f"SELECT metric, n FROM {TOTALS}")}


async def test_bk025_is_registered_and_views_have_the_contract_columns(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-025'") == 1
    assert [tuple(r) for r in await admin.fetch(COLUMNS_SQL, "v_ops_data_totals")] == [
        ("metric", "text"), ("n", "bigint")]
    assert [tuple(r) for r in await admin.fetch(COLUMNS_SQL, "v_ops_trip_plan_keys")] == [
        ("source_system", "text"),
        ("source_id", "text"),
        ("sellable", "boolean"),
    ]
    owners = await admin.fetch(
        "SELECT relname, relkind::text, relowner::regrole::text AS owner,"
        " obj_description(oid, 'pg_class') AS comment FROM pg_class"
        " WHERE relnamespace = 'booking'::regnamespace AND relname IN ('v_ops_data_totals', 'v_ops_trip_plan_keys')"
    )
    assert {(r["relname"], r["relkind"], r["owner"]) for r in owners} == {
        ("v_ops_data_totals", "v", "bk_owner"),
        ("v_ops_trip_plan_keys", "v", "bk_owner"),
    }
    for r in owners:
        assert r["comment"].startswith("bk-025 (AA-992)") and "ops.data_coverage.v1" in r["comment"]


async def test_keys_view_exposes_no_tagged_column(admin: asyncpg.Connection) -> None:
    tagged = {r[0] for r in await admin.fetch(COMMERCIAL_SQL)}
    exposed = {r["column_name"] for r in await admin.fetch(COLUMNS_SQL, "v_ops_trip_plan_keys")}
    assert tagged, "trip_plans lost its conf:commercial tags: the guard below would prove nothing"
    assert not (tagged & exposed)


async def test_totals_list_every_metric_and_count_real_rows(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        before = await _totals(admin)
        assert set(before) == METRICS and all(n >= 0 for n in before.values())
        live, held = (f"6f1c3c1e-0000-4000-8000-{uuid.uuid4().hex[:12]}" for _ in range(2))
        await admin.execute(
            "INSERT INTO booking.trip_plans (source_id, sellable, created_by) VALUES ($1, true, $3), ($2, false, $3)",
            live,
            held,
            STAFF,
        )
        await admin.execute(
            "INSERT INTO booking.suppliers (company_name, categories, country_code, created_by)"
            " VALUES ($1, ARRAY['hotel'], 'VN', $2)",
            f"Coverage DMC {uuid.uuid4().hex[:8]}",
            STAFF,
        )
        after = await _totals(admin)
        delta = {k: after[k] - before[k] for k in after if after[k] != before[k]}
        assert delta == {"trip_plans": 2, "trip_plans_sellable": 1, "suppliers_active": 1}
        keys = {
            (r["source_system"], r["source_id"], r["sellable"])
            for r in await admin.fetch(f"SELECT * FROM {KEYS} WHERE source_id = ANY($1::text[])", [live, held])
        }
        assert keys == {("cis", live, True), ("cis", held, False)}
    finally:
        await tx.rollback()


async def test_cis_app_reads_the_contract_views_and_nothing_else(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE cis_app")
        await admin.fetch(f"SELECT * FROM {TOTALS}")
        await admin.fetch(f"SELECT * FROM {KEYS} LIMIT 1")
        for table in ("booking.trip_plans", "booking.suppliers", "booking.customers", "booking.b2b_holds"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with admin.transaction():
                    await admin.fetch(f"SELECT 1 FROM {table} LIMIT 1")
    finally:
        await tx.rollback()
    rows = await admin.fetch(
        "SELECT c.relname, a.privilege_type FROM pg_class c, aclexplode(c.relacl) a"
        " WHERE c.relnamespace = 'booking'::regnamespace AND a.grantee = 'cis_app'::regrole"
    )
    granted = {(r["relname"], r["privilege_type"]) for r in rows}
    assert {("v_ops_data_totals", "SELECT"), ("v_ops_trip_plan_keys", "SELECT")} <= granted
    assert all(p == "SELECT" and rel.startswith("v_") for rel, p in granted)


async def test_role_booking_reads_but_cannot_write_the_views(admin: asyncpg.Connection) -> None:
    for view in (TOTALS, KEYS):
        assert await admin.fetchval("SELECT has_table_privilege('booking', $1, 'SELECT')", view)
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", view, privilege)


async def test_guard_refuses_a_runner_other_than_bk_owner(admin: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="expected to run as bk_owner"):
        await admin.execute(BK025.read_text(encoding="utf-8"))
    await admin.execute("ROLLBACK")


async def test_reapply_as_bk_owner_is_a_noop(admin: asyncpg.Connection) -> None:
    first = await admin.fetchval("SELECT applied_at FROM shared.schema_versions WHERE version = 'bk-025'")
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(BK025.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    rows = await admin.fetch("SELECT applied_at FROM shared.schema_versions WHERE version = 'bk-025'")
    assert [r["applied_at"] for r in rows] == [first]
