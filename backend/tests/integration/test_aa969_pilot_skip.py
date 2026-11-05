"""AA-969 part 2 (root ADR 0019 d16) on PostgreSQL 15 with the RDS role shape, as role ``booking``: the clean-up jobs
skip the pilot tenant.

``catalog.v_tenant_account_v1`` (CIS 357) is stood in by a plain table of 357's exact columns around each test (as
``test_aa1028_tenant_pull``). Covers:

- ``bk_case_lifecycle``: the trip-check notes of a B2B case of the pilot tenant stay after the keep window (a B2B case
  never holds an emergency contact, DM-6.8; DRY RUN and live leave it out); once CIS clears the marker the same run
  clears them;
- ``bk_retention_inbox_redact``: an old processed inbox row whose trip case belongs to the pilot keeps its payload, the
  other agency's row is redacted; the result says one pilot tenant was skipped;
- without the view the pilot is unknown (None): fail closed, the callers skip their destructive step.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import pilot
from app.catalog import liveness
from app.jobs.queue import Job
from app.ops import lifecycle as lc
from app.retention import jobs
from app.retention.policy import INBOX_SPEC
from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CIS_357_COLUMNS = (
    "tenant_id uuid, name text, account_status text, suspend_reason text, in_service boolean, kind text, "
    "is_pilot boolean, trade_book boolean"
)
STUB = (
    f"CREATE TABLE catalog.v_tenant_account_v1 ({CIS_357_COLUMNS}); "
    "GRANT SELECT ON catalog.v_tenant_account_v1 TO booking;"
)
DROP = "DROP TABLE IF EXISTS catalog.v_tenant_account_v1"
ADD = (
    "INSERT INTO catalog.v_tenant_account_v1 (tenant_id, name, account_status, suspend_reason, in_service, kind, "
    "is_pilot, trade_book) VALUES ($1, 'Pilot Coast Travel', 'active', NULL, true, 'agency', $2, true)"
)
SALES = "it-969-pilot"
PILOT = uuid.UUID("00000000-0000-4000-8000-00000000f11e")
NOTE_SQL = (
    "UPDATE booking.trip_case_checks SET note = 'invented check note'"
    " WHERE trip_case_id = $1::uuid AND code = 'pre_trip_recheck'"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (SALES, "sales"))
    await conn.execute(DROP)
    await conn.execute(STUB)
    await conn.execute(ADD, PILOT, True)
    liveness.reset_cache()
    try:
        yield conn
    finally:
        await conn.execute(DROP)
        liveness.reset_cache()
        await ops.purge_ops(conn)
        await conn.close()


@pytest_asyncio.fixture
async def pool(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


async def _pilot_case(admin: asyncpg.Connection, tag: str, tenant: uuid.UUID) -> dict[str, Any]:
    today = await admin.fetchval("SELECT (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date")
    case = await ops.b2b_case(admin, tag=tag, status="confirmed", start=today - timedelta(days=100))
    await admin.execute("UPDATE booking.trip_cases SET party_tenant_id = $2 WHERE id = $1::uuid", case["id"], tenant)
    await admin.execute(NOTE_SQL, case["id"])
    return case


async def test_pilot_tenants_reads_the_marker(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        assert await pilot.pilot_tenants(conn) == [PILOT]
    await admin.execute(DROP)
    liveness.reset_cache()
    async with pool.acquire() as conn:
        assert await pilot.pilot_tenants(conn) is None  # fail closed without the view: the pilot is unknown


async def test_case_lifecycle_never_clears_the_pilot_tenants_check_notes(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    case = await _pilot_case(admin, "pilot", PILOT)
    other = await _pilot_case(admin, "other", uuid.uuid4())
    scope = [case["id"], other["id"]]
    dry = await lc.run_lifecycle(pool, mode="dry_run", case_ids=scope)
    assert (dry["cleared_checks"], dry["cleared_cases"]) == (1, 1)  # the other agency's only
    live = await lc.run_lifecycle(pool, mode="live", case_ids=scope)
    assert (live["cleared_checks"], live["cleared_cases"]) == (1, 1)
    kept = await admin.fetchrow(
        "SELECT note IS NOT NULL AS has_note, cleared_at FROM booking.trip_case_checks"
        " WHERE trip_case_id = $1::uuid AND code = 'pre_trip_recheck'",
        case["id"],
    )
    assert kept["has_note"] is True and kept["cleared_at"] is None
    # CIS clears the marker (the pilot ended): the next run treats it like any other agency
    await admin.execute("UPDATE catalog.v_tenant_account_v1 SET is_pilot = false WHERE tenant_id = $1", PILOT)
    assert (await lc.run_lifecycle(pool, mode="live", case_ids=scope))["cleared_checks"] == 1


async def _inbox(admin: asyncpg.Connection, key: str) -> str:
    return await admin.fetchval(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, received_at, status)"
        " VALUES ('tripplanner', 'inquiry.submitted', $1, $2::jsonb, now() - interval '120 days', 'processed')"
        " RETURNING id::text",
        key,
        json.dumps({"contract": "inquiry.submitted", "customer": {"name": "Invented Person"}}),
    )


async def test_inbox_redaction_keeps_the_pilot_tenants_rows(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    pilot_case = await _pilot_case(admin, "inbox-pilot", PILOT)
    other_case = await _pilot_case(admin, "inbox-other", uuid.uuid4())
    kept_id = await _inbox(admin, f"it-969-{uuid.uuid4()}")
    gone_id = await _inbox(admin, f"it-969-{uuid.uuid4()}")
    for case, event in ((pilot_case, kept_id), (other_case, gone_id)):
        await admin.execute(
            "UPDATE booking.trip_cases SET inbox_event_id = $2::uuid WHERE id = $1::uuid", case["id"], event
        )
    job = Job(
        id=str(uuid.uuid4()),
        kind=INBOX_SPEC.kind,
        payload={"trigger": "manual", "dry_run": False, "max_rows": 1000},
        attempt=1,
        max_attempts=1,
    )
    try:
        result = await jobs.run_spec(pool, job, INBOX_SPEC, s3_factory=lambda: None, bucket_factory=lambda: "unused")
        assert result["pilot_tenants_skipped"] == 1 and result["mode"] == "live"
        rows = {
            r["id"]: r
            for r in await admin.fetch(
                "SELECT id::text, redacted_at, payload->'customer'->>'name' AS name FROM booking.inbox_events"
                " WHERE id = ANY($1::uuid[])",
                [kept_id, gone_id],
            )
        }
        assert rows[kept_id]["redacted_at"] is None and rows[kept_id]["name"] == "Invented Person"
        assert rows[gone_id]["redacted_at"] is not None and rows[gone_id]["name"] is None
    finally:
        await admin.execute(
            "UPDATE booking.trip_cases SET inbox_event_id = NULL WHERE id = ANY($1::uuid[])",
            [pilot_case["id"], other_case["id"]],
        )
        await admin.execute("DELETE FROM booking.inbox_events WHERE id = ANY($1::uuid[])", [kept_id, gone_id])
