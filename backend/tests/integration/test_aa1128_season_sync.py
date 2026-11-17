"""AA-1128 B1 on a real PostgreSQL: the season calendar step of ``bk_trip_plan_sync`` really assigns calendars and
never hides a cut read.

1. A run gives a plan the calendar of its first day's destination, counts every plan it picked (assigned, no tour,
   no destination) and answers ``outcome: ok`` with the batch size and read timeout of the pricing policy (bk-059).
2. A catalog read held up longer than the read timeout (another session holds a lock on the catalog view) is cut:
   only that read rolls back (a savepoint), the batch ends there, the rest is counted ``not_tried``, the plans already
   assigned in that run keep their calendar, and the connection stays usable.
3. The Seasons list returns the last finished run's counts and how many plans still wait for a calendar.

Invented tours and places only (the catalog stub of tests/integration/stub_catalog.sql)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from app.catalog import trip_plan_sync
from app.pricing import seasons_admin
from tests.integration.test_aa1029_price_chain import (  # noqa: F401 — fixtures used by name
    STAFF_TOUR,
    TOUR,
    _staff,
    _tours,
    admin,
    pool,
)
from tests.integration.test_aa1091_price_versions import HANOI, seasons_clean  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

JOB_KEY = "aa1128-it-season-sync"


async def _day_one(admin: asyncpg.Connection) -> None:  # noqa: F811
    await admin.execute(
        "INSERT INTO catalog.v_tour_day (source_id, day_number, title, body, places)"
        " VALUES ($1, 1, 'Arrival', 'Invented day', ARRAY['Hanoi'])",
        TOUR,
    )


async def test_a_run_assigns_counts_every_plan_and_reads_the_policy(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    seasons_clean: None,  # noqa: F811
) -> None:
    await _tours(admin)
    await _day_one(admin)
    try:
        out = await trip_plan_sync.run(pool)
        step = out["calendars"]
        assert (out["outcome"], out["degraded"]) == ("ok", [])
        assert step["assigned"] >= 1 and step["timed_out"] == 0 and step["not_tried"] == 0
        assert step["picked"] == sum(
            step[k] for k in ("assigned", "no_tour", "no_destination", "timed_out", "not_tried")
        )
        cal = await admin.fetchval(
            "SELECT c.destination_id::text FROM booking.trip_plans p"
            " JOIN booking.season_calendars c ON c.id = p.season_calendar_id WHERE p.source_id = $1",
            TOUR,
        )
        assert cal == HANOI
        # the plan whose first day names no known place is counted, not silently skipped
        assert step["no_destination"] >= 1
        # bk-059: the two values are approved pricing policy parameters in force today
        rows = await admin.fetch(
            "SELECT p.param_key, p.value::text AS value FROM booking.pricing_policy p"
            " JOIN booking.pricing_policy_events a ON a.policy_id = p.id AND a.event = 'approved'"
            " WHERE p.created_by IN ('seed:bk-059') ORDER BY p.param_key"
        )
        assert [(r["param_key"], json.loads(r["value"])) for r in rows] == [
            ("season_sync_batch_size", 50),
            ("season_sync_read_timeout_seconds", 5),
        ]
    finally:
        await admin.execute("DELETE FROM catalog.v_tour_day WHERE source_id = $1", TOUR)


async def test_a_cut_catalog_read_ends_the_batch_and_keeps_the_connection(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    seasons_clean: None,  # noqa: F811
    migrated_admin_dsn: str,
) -> None:
    await _tours(admin)
    await trip_plan_sync.run(pool)  # the bridge makes both plans; their calendars are cleared below
    await admin.execute(
        "UPDATE booking.trip_plans SET season_calendar_id = NULL WHERE source_id = ANY($1::text[])", [TOUR, STAFF_TOUR]
    )
    waiting = await admin.fetchval("SELECT count(*) FROM booking.trip_plans WHERE season_calendar_id IS NULL")
    blocker = await asyncpg.connect(migrated_admin_dsn)
    try:
        tx = blocker.transaction()
        await tx.start()
        await blocker.execute("LOCK TABLE catalog.v_tour IN ACCESS EXCLUSIVE MODE")
        async with pool.acquire() as conn, conn.transaction():
            out = await seasons_admin.assign_default_calendars(conn, int(waiting), "salt-it", 0.3)
            # the savepoint rolled back the cut read alone: the transaction still answers
            assert await conn.fetchval("SELECT 1") == 1
        await tx.rollback()
    finally:
        await blocker.close()
    assert out["timed_out"] == 1
    assert out["assigned"] == 0 and out["not_tried"] == out["picked"] - 1
    # after the lock is gone the next run assigns again
    await _day_one(admin)
    try:
        again = await asyncio.wait_for(trip_plan_sync.run(pool), timeout=30)
        assert again["outcome"] == "ok" and again["calendars"]["assigned"] >= 1
    finally:
        await admin.execute("DELETE FROM catalog.v_tour_day WHERE source_id = $1", TOUR)


async def test_the_seasons_list_shows_the_last_run(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    seasons_clean: None,  # noqa: F811
) -> None:
    result = {
        "outcome": "degraded",
        "degraded": ["calendars"],
        "calendars": {"picked": 4, "assigned": 1, "calendars_created": 1, "timed_out": 1, "not_tried": 2},
    }
    await admin.execute("DELETE FROM booking.job WHERE idempotency_key = $1", JOB_KEY)
    await admin.execute(
        "INSERT INTO booking.job (kind, payload, status, idempotency_key, max_attempts, created_by, started_at,"
        " finished_at, result, created_at)"
        " VALUES ('bk_trip_plan_sync', '{}'::jsonb, 'succeeded', $1, 1, 'it-1128', now(), now(), $2::jsonb,"
        " now() + interval '1 hour')",
        JOB_KEY,
        json.dumps(result),
    )
    try:
        async with pool.acquire() as conn:
            out = await seasons_admin.list_calendars(conn, _staff("it-1128-reader", "sales"))
        sync = out["sync"]
        assert sync is not None
        assert (sync["picked"], sync["assigned"], sync["timed_out"], sync["not_tried"], sync["degraded"]) == (
            4,
            1,
            1,
            2,
            True,
        )
        total = await admin.fetchval("SELECT count(*) FROM booking.trip_plans")
        without = await admin.fetchval("SELECT count(*) FROM booking.trip_plans WHERE season_calendar_id IS NULL")
        assert (out["plans"], out["plans_without_calendar"]) == (total, without)
    finally:
        await admin.execute("DELETE FROM booking.job WHERE idempotency_key = $1", JOB_KEY)
