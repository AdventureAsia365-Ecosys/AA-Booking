"""AA-1128 B1 on a real PostgreSQL: the season calendar step of ``bk_trip_plan_sync`` really assigns calendars and
never hides a cut read.

1. A run gives a plan the calendar of its first day's destination, counts every plan it picked (assigned, no tour,
   no destination) and answers ``outcome: ok`` with the batch size and read timeout of the pricing policy (bk-059).
2. A catalog read held up longer than the read timeout (another session holds a lock on the catalog view) is cut:
   only that read rolls back (a savepoint), the batch ends there, the rest is counted ``not_tried``, the plans already
   assigned in that run keep their calendar, and the connection stays usable.
3. The Seasons list returns the last finished run's counts and how many plans still wait for a calendar.
4. S291 (bk-075): a plan whose first day names no destination is remembered; the next run does not read the catalog
   for it (``deferred``), it is read again once ``season_sync_recheck_days`` have passed or a CIS lifecycle event of
   its tour arrived after the check, and it loses its record when it gets a calendar.

Invented tours and places only (the catalog stub of tests/integration/stub_catalog.sql)."""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from app.catalog import trip_plan_sync
from app.pricing import season_sync, seasons_admin
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
    await admin.execute("DELETE FROM booking.season_sync_checks")  # bk-075: the run above remembered both plans
    waiting = await admin.fetchval("SELECT count(*) FROM booking.trip_plans WHERE season_calendar_id IS NULL")
    blocker = await asyncpg.connect(migrated_admin_dsn)
    try:
        tx = blocker.transaction()
        await tx.start()
        await blocker.execute("LOCK TABLE catalog.v_tour IN ACCESS EXCLUSIVE MODE")
        async with pool.acquire() as conn, conn.transaction():
            out = await season_sync.assign_default_calendars(conn, int(waiting), "salt-it", 0.3, 7)
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


async def _check(admin: asyncpg.Connection, source_id: str) -> asyncpg.Record | None:  # noqa: F811
    return await admin.fetchrow(
        "SELECT c.outcome, c.tour_id::text AS tour_id, c.checked_at FROM booking.season_sync_checks c"
        " JOIN booking.trip_plans p ON p.id = c.trip_plan_id WHERE p.source_id = $1",
        source_id,
    )


async def _tour_reads(pool: asyncpg.Pool, recheck_days: int = 7) -> tuple[dict[str, int], list[str]]:  # noqa: F811
    """One step run (batch 500) and the source keys it read from catalog.v_tour, in order."""
    read: list[str] = []

    def log(q: object) -> None:
        if "FROM catalog.v_tour t" in q.query and q.args:  # type: ignore[attr-defined]
            read.append(q.args[1])  # type: ignore[attr-defined]

    async with pool.acquire() as conn:
        conn.add_query_logger(log)
        try:
            async with conn.transaction():
                out = await season_sync.assign_default_calendars(conn, 500, "salt-recheck", 5.0, recheck_days)
        finally:
            conn.remove_query_logger(log)
    return out, read


async def test_a_plan_without_a_destination_waits_for_the_interval_or_a_cis_change(
    admin: asyncpg.Connection,  # noqa: F811
    pool: asyncpg.Pool,  # noqa: F811
    seasons_clean: None,  # noqa: F811
) -> None:
    await _tours(admin)
    # The bridge makes both plans and its step reads both: neither tour has a first day with a place yet.
    first = (await trip_plan_sync.run(pool))["calendars"]
    assert first["no_destination"] >= 2 and first["deferred"] == 0
    recorded = await _check(admin, STAFF_TOUR)
    assert recorded is not None and recorded["outcome"] == "no_destination" and recorded["tour_id"]

    # The next run leaves both out: no catalog read, counted deferred, nothing degraded.
    again, read = await _tour_reads(pool)
    assert STAFF_TOUR not in read and TOUR not in read
    assert again["deferred"] >= 2 and again["timed_out"] == 0
    assert again["picked"] == sum(again[k] for k in ("assigned", "no_tour", "no_destination", "timed_out", "not_tried"))

    # A CIS lifecycle event of TOUR received after the check: TOUR is read again at once (and now has a destination).
    await _day_one(admin)
    await admin.execute(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, status, processed_at)"
        " VALUES ('cis', 'catalog.tour', $1, $2::jsonb, 'processed', now())",
        JOB_KEY + ":lifecycle",
        json.dumps({"source_system": "cis", "source_id": TOUR, "event": "superseded"}),
    )
    try:
        changed, read = await _tour_reads(pool)
        assert TOUR in read and STAFF_TOUR not in read
        assert changed["assigned"] >= 1 and await _check(admin, TOUR) is None  # assigned: the record is gone

        # The interval: a check older than season_sync_recheck_days is read again.
        await admin.execute(
            "UPDATE booking.season_sync_checks SET checked_at = now() - interval '8 days'"
            " WHERE trip_plan_id = (SELECT id FROM booking.trip_plans WHERE source_id = $1)",
            STAFF_TOUR,
        )
        later, read = await _tour_reads(pool)
        assert STAFF_TOUR in read and later["no_destination"] >= 1
        refreshed = await _check(admin, STAFF_TOUR)
        assert refreshed is not None and refreshed["checked_at"] > recorded["checked_at"]
        # ... and with a longer interval in force it would have waited.
        await admin.execute(
            "UPDATE booking.season_sync_checks SET checked_at = now() - interval '8 days'"
            " WHERE trip_plan_id = (SELECT id FROM booking.trip_plans WHERE source_id = $1)",
            STAFF_TOUR,
        )
        _, read = await _tour_reads(pool, recheck_days=30)
        assert STAFF_TOUR not in read
    finally:
        await admin.execute("DELETE FROM catalog.v_tour_day WHERE source_id = $1", TOUR)
        await admin.execute("DELETE FROM booking.inbox_events WHERE idempotency_key = $1", JOB_KEY + ":lifecycle")

    # bk-075: the interval is an approved pricing policy parameter, version 1 = 7 days.
    seeded = await admin.fetchval(
        "SELECT p.value::text FROM booking.pricing_policy p"
        " JOIN booking.pricing_policy_events a ON a.policy_id = p.id AND a.event = 'approved'"
        " WHERE p.created_by = 'seed:bk-075' AND p.param_key = 'season_sync_recheck_days'"
    )
    assert json.loads(seeded) == 7
