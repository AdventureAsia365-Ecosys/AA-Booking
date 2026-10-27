"""AA-682 S2 jobs on a real PostgreSQL (bk-027 + the bk-026 guard), through a pool of role ``booking``:

- ``bk_case_lifecycle`` DRY RUN lists the moves and writes nothing; live moves a B2B case confirmed → pre_departure →
  on_trip → completed in one run (catch-up), each move a history row with an "Auto:" note and an audit row;
- a B2C case whose pre-departure date is reached is blocked ``not_paid`` (+ ``not_booked``): one gate notice, never a
  second one on the next run;
- dates are the ops time zone's (Asia/Ho_Chi_Minh): with the pool's session in UTC-11 or UTC+14 the job still moves
  on the ops date and the guard agrees, and a start date of tomorrow (ops) never moves;
- a case a staff request holds (row lock) is skipped, not waited for;
- emergency contacts and notes of a trip that ended 90+ days ago: counted by the DRY RUN, cleared by live, once;
- ``bk_ops_reminders``: a supplier booking waiting past ``supplier_answer_days`` and a check due soon each notify
  once (to the requester / assignee), never again on the next run;
- the job handler: scoped manual runs, the DRY RUN default, ids-only payload validation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.ops import lifecycle as lc
from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SALES = "it-682-life"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (SALES, "sales"))
    try:
        yield conn
    finally:
        await ops.purge_ops(conn)
        await conn.close()


async def _pool(dsn: str, tz: str = "UTC") -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=3, server_settings={"timezone": tz})


@pytest_asyncio.fixture
async def pool(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await _pool(migrated_booking_dsn)
    try:
        yield p
    finally:
        await p.close()


async def _today(conn: asyncpg.Connection | asyncpg.Pool) -> date:
    return await conn.fetchval("SELECT (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date")


async def _status(conn: asyncpg.Connection, case_id: str) -> str:
    return await conn.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case_id)


def _job(payload: dict[str, Any]) -> Job:
    return Job(id="00000000-0000-4000-8000-000000000001", kind=lc.KIND, payload=payload, attempt=1, max_attempts=3)


async def test_dry_run_then_live_catch_up(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    today = await _today(admin)
    case = await ops.b2b_case(admin, tag="life-chain", start=today - timedelta(days=3), days=2)  # ended yesterday
    dry = await lc.run_lifecycle(pool, mode="dry_run", case_ids=[case["id"]])
    assert dry["mode"] == "dry_run" and dry["scoped"] and dry["timezone"] == "Asia/Ho_Chi_Minh"
    assert dry["today"] == today.isoformat()
    assert dry["moves"] == {"pre_departure": 1, "on_trip": 1, "completed": 1}
    assert dry["moved_cases"] == [
        {"code": case["code"], "from": "confirmed", "to": ["pre_departure", "on_trip", "completed"]}
    ]
    assert await _status(admin, case["id"]) == "confirmed"  # nothing written

    live = await lc.run_lifecycle(pool, mode="live", case_ids=[case["id"]])
    assert live["moves"] == dry["moves"] and live["refused"] == 0 and live["blocked"] == 0
    assert await _status(admin, case["id"]) == "completed"
    history = await admin.fetch(
        "SELECT from_status, to_status, note, changed_by FROM booking.trip_case_status_history"
        " WHERE trip_case_id = $1::uuid AND from_status IS NOT NULL",
        case["id"],
    )
    assert sorted((h["from_status"], h["to_status"]) for h in history) == sorted(
        [("confirmed", "pre_departure"), ("pre_departure", "on_trip"), ("on_trip", "completed")]
    )
    assert all(h["note"].startswith("Auto: travel dates") and h["changed_by"] is None for h in history)
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.audit_logs WHERE entity_id = $1::uuid AND actor_ref = $2",
            case["id"],
            lc.ACTOR,
        )
        == 3
    )
    again = await lc.run_lifecycle(pool, mode="live", case_ids=[case["id"]])
    assert again["candidates"] == 0  # completed cases are not candidates


async def test_b2c_blocked_not_paid_notified_once(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    today = await _today(admin)
    sup = await ops.supply(admin)
    sold = await ops.sold_case(admin, sup, tag="life-b2c")
    await admin.execute(
        "UPDATE booking.trip_cases SET start_date = $2::date, end_date = $2::date + 4 WHERE id = $1::uuid",
        sold["id"],
        today + timedelta(days=10),
    )
    first = await lc.run_lifecycle(pool, mode="live", case_ids=[sold["id"]])
    assert first["blocked"] == 1 and first["notified"] == 1 and first["moves"]["pre_departure"] == 0
    assert first["blocked_cases"][0]["reasons"] == ["not_booked", "not_paid"]
    note = await admin.fetchrow(
        "SELECT title, data::text AS data, recipient_ref FROM booking.notifications"
        " WHERE type = 'trip_case.gate_blocked' AND entity_id = $1::uuid",
        sold["id"],
    )
    assert "cannot move to Pre-departure" in note["title"] and '"open_checks": 3' in note["data"]
    second = await lc.run_lifecycle(pool, mode="live", case_ids=[sold["id"]])
    assert second["blocked"] == 1 and second["notified"] == 0
    assert await _status(admin, sold["id"]) == "confirmed"
    # The DRY RUN reports the same gate and notifies nobody.
    dry = await lc.run_lifecycle(pool, mode="dry_run", case_ids=[sold["id"]])
    assert dry["blocked"] == 1 and dry["notified"] == 0


@pytest.mark.parametrize("session_tz", ["Pacific/Pago_Pago", "Pacific/Kiritimati"])
async def test_ops_date_whatever_the_session_zone(
    admin: asyncpg.Connection, migrated_booking_dsn: str, session_tz: str
) -> None:
    """UTC-11 and UTC+14 sessions: 'today' is the ops date (UTC+7), and the guard's current_date agrees because the
    job sets the transaction's TimeZone to the ops zone."""
    today = await _today(admin)
    starts_today = await ops.b2b_case(admin, tag=f"tz-a-{session_tz}", status="confirmed", start=today)
    tomorrow = await ops.b2b_case(admin, tag=f"tz-b-{session_tz}", status="confirmed", start=today + timedelta(days=1))
    p = await _pool(migrated_booking_dsn, session_tz)
    try:
        result = await lc.run_lifecycle(p, mode="live", case_ids=[starts_today["id"], tomorrow["id"]])
    finally:
        await p.close()
    assert result["today"] == today.isoformat() and result["refused"] == 0
    assert await _status(admin, starts_today["id"]) == "on_trip"
    assert await _status(admin, tomorrow["id"]) == "pre_departure"


async def test_locked_case_is_skipped(admin: asyncpg.Connection, pool: asyncpg.Pool, migrated_booking_dsn: str) -> None:
    today = await _today(admin)
    case = await ops.b2b_case(admin, tag="life-lock", start=today + timedelta(days=5))
    holder = await asyncpg.connect(migrated_booking_dsn)
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT 1 FROM booking.trip_cases WHERE id = $1::uuid FOR UPDATE", case["id"])
        result = await asyncio.wait_for(lc.run_lifecycle(pool, mode="live", case_ids=[case["id"]]), timeout=10)
        assert result["candidates"] == 0
        await tx.rollback()
    finally:
        await holder.close()
    assert await _status(admin, case["id"]) == "confirmed"
    assert (await lc.run_lifecycle(pool, mode="live", case_ids=[case["id"]]))["moves"]["pre_departure"] == 1


async def test_contacts_cleared_after_keep_window(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    today = await _today(admin)
    old = await ops.b2c_case(admin, tag="life-old", status="confirmed", start=today - timedelta(days=100))  # ended -96
    recent = await ops.b2c_case(admin, tag="life-recent", status="confirmed", start=today - timedelta(days=60))
    for case in (old, recent):
        await admin.execute(
            "UPDATE booking.trip_case_checks SET emergency_contact_name = 'Anna',"
            " emergency_contact_phone = '+84 90 1234', note = 'call after 6pm'"
            " WHERE trip_case_id = $1::uuid AND code = 'emergency_contact'",
            case["id"],
        )
    scope = [old["id"], recent["id"]]
    dry = await lc.run_lifecycle(pool, mode="dry_run", case_ids=scope)
    assert (dry["cleared_checks"], dry["cleared_cases"]) == (1, 1)
    live = await lc.run_lifecycle(pool, mode="live", case_ids=scope)
    assert (live["cleared_checks"], live["cleared_cases"]) == (1, 1)
    rows = {
        r["trip_case_id"]: r
        for r in await admin.fetch(
            "SELECT trip_case_id::text, emergency_contact_name, note, cleared_at FROM booking.trip_case_checks"
            " WHERE trip_case_id = ANY($1::uuid[]) AND code = 'emergency_contact'",
            scope,
        )
    }
    assert rows[old["id"]]["emergency_contact_name"] is None and rows[old["id"]]["note"] is None
    assert rows[old["id"]]["cleared_at"] is not None
    assert rows[recent["id"]]["emergency_contact_name"] == "Anna" and rows[recent["id"]]["cleared_at"] is None
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.audit_logs WHERE action = 'trip_check.personal_data_cleared'"
            " AND entity_id = $1::uuid",
            old["id"],
        )
        == 1
    )
    assert (await lc.run_lifecycle(pool, mode="live", case_ids=scope))["cleared_checks"] == 0


async def test_ops_reminders_once(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    today = await _today(admin)
    sup = await ops.supply(admin)
    sold = await ops.sold_case(admin, sup, tag="life-remind")
    booking = await admin.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, version_item_id, supplier_id, item_description)"
        " VALUES ($1::uuid, $2::uuid, $3::uuid, 'Room') RETURNING id::text",
        sold["id"],
        sold["room_item"],
        sup["supplier"],
    )
    await admin.execute(
        "UPDATE booking.supplier_bookings SET status = 'requested', requested_at = now() - interval '3 days',"
        " requested_by = $2 WHERE id = $1::uuid",
        booking,
        SALES,
    )
    check = await admin.fetchval(
        "UPDATE booking.trip_case_checks SET due_date = $2::date + 1, assignee_ref = $3"
        " WHERE trip_case_id = $1::uuid AND code = 'insurance' RETURNING id::text",
        sold["id"],
        today,
        SALES,
    )
    await admin.execute(
        "UPDATE booking.trip_case_checks SET due_date = $2::date + 30 WHERE trip_case_id = $1::uuid"
        " AND code <> 'insurance'",
        sold["id"],
        today,
    )
    first = await lc.run_reminders(pool)
    assert first["supplier_waiting_notified"] >= 1 and first["checks_due_notified"] >= 1
    rows = await admin.fetch(
        "SELECT type, recipient_ref, entity_id::text AS entity_id, data ->> 'check_id' AS check_id"
        " FROM booking.notifications WHERE type IN ('supplier_booking.waiting', 'trip_check.due')"
        " AND (entity_id = $1::uuid OR entity_id = $2::uuid)",
        booking,
        sold["id"],
    )
    assert {(r["type"], r["recipient_ref"]) for r in rows} == {
        ("supplier_booking.waiting", SALES),
        ("trip_check.due", SALES),
    }
    assert next(r for r in rows if r["type"] == "trip_check.due")["check_id"] == check
    second = await lc.run_reminders(pool)
    assert second["supplier_waiting_notified"] == 0 and second["checks_due_notified"] == 0
    # A new due date reminds again.
    await admin.execute("UPDATE booking.trip_case_checks SET due_date = due_date - 1 WHERE id = $1::uuid", check)
    assert (await lc.run_reminders(pool))["checks_due_notified"] == 1


async def test_handler_scoped_runs_and_payload(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    today = await _today(admin)
    case = await ops.b2b_case(admin, tag="life-handler", start=today + timedelta(days=3))
    dry = await lc.handle_lifecycle(pool, _job({"trigger": "manual", "case_ids": [case["id"]]}))
    assert dry["mode"] == "dry_run" and dry["reason"] == "manual dry run" and dry["moves"]["pre_departure"] == 1
    assert await _status(admin, case["id"]) == "confirmed"
    live = await lc.handle_lifecycle(pool, _job({"trigger": "manual", "dry_run": False, "case_ids": [case["id"]]}))
    assert live["mode"] == "live" and live["scoped"] is True
    assert await _status(admin, case["id"]) == "pre_departure"
    with pytest.raises(PermanentError):
        await lc.handle_lifecycle(pool, _job({"trigger": "manual", "case_ids": ["not-a-uuid"]}))
    reminders = await lc.handle_reminders(pool, _job({"trigger": "schedule"}))
    assert reminders["trigger"] == "schedule" and "checks_due" in reminders
