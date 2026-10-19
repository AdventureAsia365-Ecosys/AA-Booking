"""bk-017 (AA-858) on a real PostgreSQL: the ops overview contract views booking.v_ops_funnel_daily and
booking.v_ops_case_status. Shape (aggregates only, no other column), metrics from real rows written through the
status triggers, and the reader grants: cis_app reads the two views and nothing else in schema booking, role booking
reads them but cannot write them.

Logged in as the non-superuser aa_cis_admin (member of bk_owner and cis_app, tests/integration/bootstrap_roles.py), as
on RDS. Rows are written inside a transaction that is rolled back, and every assertion compares the view before and
after, so other tests' rows in the session database do not matter."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FUNNEL = "booking.v_ops_funnel_daily"
CASE_STATUS = "booking.v_ops_case_status"

COLUMNS_SQL = """
SELECT column_name, data_type FROM information_schema.columns
 WHERE table_schema = 'booking' AND table_name = $1 ORDER BY ordinal_position"""

FUNNEL_TODAY_SQL = f"""
SELECT metric, source_system, n FROM {FUNNEL} WHERE day = (now() AT TIME ZONE 'UTC')::date"""


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _funnel_today(conn: asyncpg.Connection) -> dict[tuple[str, str], int]:
    return {(r["metric"], r["source_system"]): r["n"] for r in await conn.fetch(FUNNEL_TODAY_SQL)}


async def _case_status(conn: asyncpg.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in await conn.fetch(f"SELECT status, n FROM {CASE_STATUS}")}


def _delta(after: dict, before: dict) -> dict:
    keys = set(after) | set(before)
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys if after.get(k, 0) != before.get(k, 0)}


async def _case(conn: asyncpg.Connection, source_system: str) -> str:
    customer = await conn.fetchval(
        "INSERT INTO booking.customers (name, email) VALUES ('Ops view lead', $1) RETURNING id",
        f"{uuid.uuid4().hex}@x.test",
    )
    return await conn.fetchval(
        "INSERT INTO booking.trip_cases (customer_id, source_system, inquiry_snapshot, currency)"
        " VALUES ($1, $2, $3::jsonb, 'USD') RETURNING id::text",
        customer,
        source_system,
        json.dumps({}),
    )


async def _walk(conn: asyncpg.Connection, case_id: str, *statuses: str) -> None:
    for status in statuses:
        await conn.execute("UPDATE booking.trip_cases SET status = $2 WHERE id = $1::uuid", case_id, status)


async def test_bk017_is_registered_and_views_expose_aggregates_only(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-017'") == 1
    funnel = [tuple(r) for r in await admin.fetch(COLUMNS_SQL, "v_ops_funnel_daily")]
    assert funnel == [("day", "date"), ("metric", "text"), ("source_system", "text"), ("n", "bigint")]
    status = [tuple(r) for r in await admin.fetch(COLUMNS_SQL, "v_ops_case_status")]
    assert status == [("status", "text"), ("n", "bigint")]
    owners = await admin.fetch(
        "SELECT relname, relkind::text, relowner::regrole::text AS owner FROM pg_class"
        " WHERE relnamespace = 'booking'::regnamespace AND relname IN ('v_ops_funnel_daily', 'v_ops_case_status')"
    )
    assert {(r["relname"], r["relkind"], r["owner"]) for r in owners} == {
        ("v_ops_funnel_daily", "v", "bk_owner"),
        ("v_ops_case_status", "v", "bk_owner"),
    }


async def test_funnel_counts_inquiries_cases_and_first_reach_of_each_milestone(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        funnel_before = await _funnel_today(admin)
        status_before = await _case_status(admin)

        for status in ("processed", "parked", "failed"):
            await admin.execute(
                "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, status)"
                " VALUES ('tripplanner', 'inquiry.submitted', $1, '{}'::jsonb, $2)",
                f"bk017-{uuid.uuid4().hex}",
                status,
            )
        # A row of another contract is not an inquiry.
        await admin.execute(
            "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, status)"
            " VALUES ('cis', 'something.else', $1, '{}'::jsonb, 'processed')",
            f"bk017-{uuid.uuid4().hex}",
        )

        # Case A (tripplanner): sent twice, then confirmed -> proposal_sent counts once.
        a = await _case(admin, "tripplanner")
        await _walk(admin, a, "discovery", "building", "proposal_sent", "building", "proposal_sent", "confirmed")
        # Case B (manual): cancelled, reopened, cancelled again -> cancelled counts once.
        b = await _case(admin, "manual")
        await _walk(admin, b, "cancelled", "new_inquiry", "cancelled")
        # Case C (tripplanner): open, then soft-deleted -> counted nowhere.
        c = await _case(admin, "tripplanner")
        await _walk(admin, c, "discovery", "building", "proposal_sent")
        await admin.execute("UPDATE booking.trip_cases SET deleted_at = now() WHERE id = $1::uuid", c)

        assert _delta(await _funnel_today(admin), funnel_before) == {
            ("inquiry_received", "tripplanner"): 3,
            ("inquiry_parked", "tripplanner"): 1,
            ("inquiry_failed", "tripplanner"): 1,
            ("trip_case_created", "tripplanner"): 1,
            ("trip_case_created", "manual"): 1,
            ("proposal_sent", "tripplanner"): 1,
            ("confirmed", "tripplanner"): 1,
            ("cancelled", "manual"): 1,
        }
        assert _delta(await _case_status(admin), status_before) == {"confirmed": 1, "cancelled": 1}
    finally:
        await tx.rollback()


async def test_rows_older_than_400_days_are_left_out(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        old_day = await admin.fetchval("SELECT ((now() - interval '401 days') AT TIME ZONE 'UTC')::date")
        await admin.execute(
            "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, status, received_at)"
            " VALUES ('tripplanner', 'inquiry.submitted', $1, '{}'::jsonb, 'processed', now() - interval '401 days')",
            f"bk017-{uuid.uuid4().hex}",
        )
        assert await admin.fetchval(f"SELECT count(*) FROM {FUNNEL} WHERE day <= $1", old_day) == 0
    finally:
        await tx.rollback()


async def test_cis_app_reads_the_two_views_and_nothing_else(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE cis_app")
        await admin.fetch(f"SELECT * FROM {FUNNEL} LIMIT 1")
        await admin.fetch(f"SELECT * FROM {CASE_STATUS} LIMIT 1")
        for table in ("booking.customers", "booking.trip_cases", "booking.inbox_events"):
            # A savepoint per read: the expected 42501 rolls it back and the outer transaction goes on.
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with admin.transaction():
                    await admin.fetch(f"SELECT 1 FROM {table} LIMIT 1")
    finally:
        await tx.rollback()
    rows = await admin.fetch(
        "SELECT c.relname, a.privilege_type FROM pg_class c, aclexplode(c.relacl) a"
        " WHERE c.relnamespace = 'booking'::regnamespace AND a.grantee = 'cis_app'::regrole"
    )
    assert {(r["relname"], r["privilege_type"]) for r in rows} == {
        ("v_ops_funnel_daily", "SELECT"),
        ("v_ops_case_status", "SELECT"),
    }
    assert await admin.fetchval("SELECT has_schema_privilege('cis_app', 'booking', 'USAGE')")
    assert not await admin.fetchval("SELECT has_schema_privilege('cis_app', 'booking', 'CREATE')")


async def test_role_booking_reads_but_cannot_write_the_views(admin: asyncpg.Connection) -> None:
    for view in (FUNNEL, CASE_STATUS):
        assert await admin.fetchval("SELECT has_table_privilege('booking', $1, 'SELECT')", view)
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", view, privilege)
