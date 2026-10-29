"""bk-035 (AA-1003 R2) on a real PostgreSQL: the inquiry inbox key contract view booking.v_ops_inquiry_inbox. Shape (the
exact column list is the contract: no payload, error text or customer column), the 30-day window and the contract
filter, has_trip_case from the trip case's idempotency key, and the reader grants: cis_app reads the contract views and
nothing else in schema booking, role booking reads the view but cannot write it.

Logged in as the non-superuser aa_cis_admin (member of bk_owner and cis_app, tests/integration/bootstrap_roles.py), as
on RDS. Rows are written inside a transaction that is rolled back."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

VIEW = "booking.v_ops_inquiry_inbox"
BK035 = Path(__file__).resolve().parents[3] / "migrations" / "035_ops_inquiry_inbox_view.sql"

COLUMNS_SQL = """
SELECT column_name, data_type FROM information_schema.columns
 WHERE table_schema = 'booking' AND table_name = $1 ORDER BY ordinal_position"""

INBOX_SQL = """
INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, received_at, status, error)
VALUES ($1, $2, $3, $4::jsonb, now() - $5::text::interval, $6, $7)"""


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_file_is_a_contract_file() -> None:
    """No `-- migrate: expand` header: the cis_app grant is a cross-owner grant (root ADR 0017 'What stays manual'),
    which the AA-CIS-Infra expand lint refuses; Migration Apply stops here and a person applies it."""
    assert not [ln for ln in BK035.read_text(encoding="utf-8").splitlines() if ln.startswith("-- migrate:")]


async def test_bk035_is_registered_and_the_view_has_the_contract_columns(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-035'") == 1
    assert [tuple(r) for r in await admin.fetch(COLUMNS_SQL, "v_ops_inquiry_inbox")] == [
        ("source", "text"),
        ("idempotency_key", "text"),
        ("status", "text"),
        ("received_at", "timestamp with time zone"),
        ("processed_at", "timestamp with time zone"),
        ("has_trip_case", "boolean"),
    ]
    row = await admin.fetchrow(
        "SELECT relkind::text, relowner::regrole::text AS owner, obj_description(oid, 'pg_class') AS comment"
        " FROM pg_class WHERE oid = 'booking.v_ops_inquiry_inbox'::regclass"
    )
    assert (row["relkind"], row["owner"]) == ("v", "bk_owner")
    assert row["comment"].startswith("bk-035 (AA-1003)") and "ops.overview.v1" in row["comment"]


async def test_view_lists_recent_inquiries_with_their_case_and_no_content(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        tag = uuid.uuid4().hex[:8]
        keys = {name: f"{uuid.uuid4()}:{tag}-{name}" for name in ("cased", "parked", "failed", "old", "other")}
        secret = {"customer": {"name": "Jane Traveller", "email": f"jane-{tag}@x.test"}}
        rows = (
            ("tripplanner", "inquiry.submitted", keys["cased"], "2 minutes", "processed", None),
            ("tripplanner", "inquiry.submitted", keys["parked"], "3 hours", "parked", "no contact for Jane"),
            ("tripplanner", "inquiry.submitted", keys["failed"], "29 days", "failed", "bad payload from Jane"),
            ("tripplanner", "inquiry.submitted", keys["old"], "31 days", "processed", None),
            ("cis", "tour.lifecycle", keys["other"], "1 minute", "processed", None),
        )
        for source, contract, key, age, status, error in rows:
            await admin.execute(INBOX_SQL, source, contract, key, json.dumps(secret), age, status, error)
        customer = await admin.fetchval(
            "INSERT INTO booking.customers (name, email) VALUES ('Inbox view lead', $1) RETURNING id",
            f"{uuid.uuid4().hex}@x.test",
        )
        await admin.execute(
            "INSERT INTO booking.trip_cases (customer_id, source_system, idempotency_key, currency)"
            " VALUES ($1, 'tripplanner', $2, 'USD')",
            customer,
            keys["cased"],
        )
        await admin.execute("SET LOCAL ROLE cis_app")
        seen = {
            r["idempotency_key"]: dict(r)
            for r in await admin.fetch(
                f"SELECT * FROM {VIEW} WHERE idempotency_key = ANY($1::text[])", list(keys.values())
            )
        }
    finally:
        await tx.rollback()
    assert set(seen) == {keys["cased"], keys["parked"], keys["failed"]}
    assert {k: (v["source"], v["status"], v["has_trip_case"]) for k, v in seen.items()} == {
        keys["cased"]: ("tripplanner", "processed", True),
        keys["parked"]: ("tripplanner", "parked", False),
        keys["failed"]: ("tripplanner", "failed", False),
    }
    assert all(v["received_at"] is not None for v in seen.values())
    text = json.dumps([{k: str(x) for k, x in v.items()} for v in seen.values()])
    assert "Jane" not in text and "x.test" not in text


async def test_cis_app_reads_the_contract_views_and_nothing_else(admin: asyncpg.Connection) -> None:
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE cis_app")
        await admin.fetch(f"SELECT * FROM {VIEW} LIMIT 1")
        for table in ("booking.inbox_events", "booking.trip_cases", "booking.customers"):
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
    assert ("v_ops_inquiry_inbox", "SELECT") in granted
    assert all(p == "SELECT" and rel.startswith("v_") for rel, p in granted)


async def test_role_booking_reads_but_cannot_write_the_view(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT has_table_privilege('booking', $1, 'SELECT')", VIEW)
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not await admin.fetchval("SELECT has_table_privilege('booking', $1, $2)", VIEW, privilege)


async def test_guard_refuses_a_runner_other_than_bk_owner(admin: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="expected to run as bk_owner"):
        await admin.execute(BK035.read_text(encoding="utf-8"))
    await admin.execute("ROLLBACK")


async def test_reapply_as_bk_owner_is_a_noop(admin: asyncpg.Connection) -> None:
    first = await admin.fetchval("SELECT applied_at FROM shared.schema_versions WHERE version = 'bk-035'")
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(BK035.read_text(encoding="utf-8"))
    finally:
        await admin.execute("RESET ROLE")
    rows = await admin.fetch("SELECT applied_at FROM shared.schema_versions WHERE version = 'bk-035'")
    assert [r["applied_at"] for r in rows] == [first]
