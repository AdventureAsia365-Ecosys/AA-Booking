"""AA-682 S2 (bk-027) on a real PostgreSQL, as role ``booking`` where the app writes: registration and re-run no-op,
grants (no DELETE, column UPDATE only, cis_app reads nothing), the seeding trigger on both entry paths (a B2B case
inserted at ``confirmed`` as ``holds._open_case`` does, and a B2C case moved ``proposal_sent → confirmed`` by the
version approval), due dates that follow the start date, the check guard (edges, done ⇔ done_at, fixed code / title,
no emergency contact on a B2B case, cleared is final), the backfill, the board view (stored + derived checks), the
reminder dedupe index and the DM-6.1 tags in the same file. The HTTP flows and the job are in
``test_bk027_trip_checks_api.py`` and ``test_bk027_lifecycle.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from tests.integration import ops

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "027_trip_checks_lifecycle.sql"
STAFF = "it-682-checks"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (STAFF, "sales"))
    try:
        yield conn
    finally:
        await ops.purge_ops(conn)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _checks(conn: asyncpg.Connection, case_id: str) -> dict[str, Any]:
    rows = await conn.fetch(
        "SELECT id::text AS id, code, title, status, due_date, emergency_contact_name FROM booking.trip_case_checks"
        " WHERE trip_case_id = $1::uuid ORDER BY created_at, code",
        case_id,
    )
    return {r["code"]: dict(r) for r in rows if r["code"] != "custom"}


async def _as_owner(conn: asyncpg.Connection, sql: str, *args: Any) -> Any:
    await conn.execute("SET ROLE bk_owner")
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.execute("RESET ROLE")


async def test_registered_rerunnable_and_grants(admin: asyncpg.Connection) -> None:
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute(MIGRATION.read_text(encoding="utf-8"))  # a second run is a no-op
    finally:
        await admin.execute("RESET ROLE")
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-027'") == 1
    for rel in ("trip_case_checks", "v_trip_check_board"):
        owner = await admin.fetchval(
            f"SELECT relowner::regrole::text FROM pg_class WHERE oid = 'booking.{rel}'::regclass"
        )
        assert owner == "bk_owner", rel
        for privilege in ("DELETE", "TRUNCATE"):
            assert not await admin.fetchval(
                "SELECT has_table_privilege('booking', $1, $2)", f"booking.{rel}", privilege
            )
        assert not await admin.fetchval("SELECT has_table_privilege('cis_app', $1, 'SELECT')", f"booking.{rel}")
    assert not await admin.fetchval("SELECT has_table_privilege('booking', 'booking.trip_case_checks', 'UPDATE')")
    for col, allowed in (("status", True), ("emergency_contact_name", True), ("trip_case_id", False), ("code", False)):
        assert (
            await admin.fetchval(
                "SELECT has_column_privilege('booking', 'booking.trip_case_checks', $1, 'UPDATE')", col
            )
            is allowed
        ), col
    settings = {r["key"] for r in await admin.fetch("SELECT key FROM booking.settings")}
    assert {"trip_check_reminder_days", "emergency_contact_keep_days"} <= settings
    assert await admin.fetchval("SELECT to_regclass('booking.notifications_reminder_uq') IS NOT NULL")


async def test_tags_in_the_same_file(admin: asyncpg.Connection) -> None:
    comments = {
        r["col"]: r["c"]
        for r in await admin.fetch(
            "SELECT a.attname AS col, col_description(a.attrelid, a.attnum) AS c FROM pg_attribute a"
            " WHERE a.attrelid = 'booking.trip_case_checks'::regclass AND a.attnum > 0"
        )
    }
    assert comments["emergency_contact_name"].startswith("pii:emergency_contact; also pii:name")
    assert comments["emergency_contact_phone"].startswith("pii:emergency_contact; also pii:contact")
    assert comments["note"].startswith("pii:free_text")
    assert comments["title"].startswith("pii:free_text")


async def test_seeded_on_insert_at_confirmed_b2b(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    """The B2B confirm inserts the case at confirmed (holds._open_case): the trigger seeds without touching holds.py;
    the emergency contact is not applicable on an agency trip (DM-6.8)."""
    case = await ops.b2b_case(app_conn)
    checks = await _checks(app_conn, case["id"])
    assert set(checks) == {"pre_trip_recheck", "emergency_contact", "insurance"}
    assert checks["emergency_contact"]["status"] == "not_applicable"
    assert checks["emergency_contact"]["due_date"] is None
    assert checks["insurance"]["status"] == "open"
    assert checks["insurance"]["due_date"] == ops.START - timedelta(days=14)
    # The DB refuses any emergency contact on the B2B case, whatever the writer.
    with pytest.raises(asyncpg.CheckViolationError, match="no emergency contact on a B2B trip case"):
        await app_conn.execute(
            "UPDATE booking.trip_case_checks SET emergency_contact_name = 'X',"
            " emergency_contact_phone = '+84 90 000 0000' WHERE id = $1::uuid",
            checks["emergency_contact"]["id"],
        )
    with pytest.raises(asyncpg.CheckViolationError, match="no emergency contact on a B2B trip case"):
        await app_conn.execute(
            "UPDATE booking.trip_case_checks SET status = 'open' WHERE id = $1::uuid", checks["emergency_contact"]["id"]
        )


async def test_seeded_on_approval_once(app_conn: asyncpg.Connection) -> None:
    """B2C: entering confirmed by an UPDATE (the version approval) seeds once; leaving and coming back adds nothing."""
    case = await ops.b2c_case(app_conn)
    assert await _checks(app_conn, case["id"]) == {}
    await app_conn.execute("UPDATE booking.trip_cases SET status = 'confirmed' WHERE id = $1::uuid", case["id"])
    checks = await _checks(app_conn, case["id"])
    assert {c["status"] for c in checks.values()} == {"open"}
    assert checks["emergency_contact"]["due_date"] == ops.START - timedelta(days=14)
    # confirmed -> cancelled -> new_inquiry ... -> confirmed again: no duplicate standard row.
    await app_conn.execute(
        "UPDATE booking.trip_case_checks SET status = 'done', done_at = now(), done_by = $2 WHERE id = $1::uuid",
        checks["insurance"]["id"],
        STAFF,
    )
    for status in ("cancelled", "new_inquiry", "discovery", "building", "proposal_sent", "confirmed"):
        await app_conn.execute("UPDATE booking.trip_cases SET status = $2 WHERE id = $1::uuid", case["id"], status)
    again = await _checks(app_conn, case["id"])
    assert len(again) == 3 and again["insurance"]["status"] == "done"
    assert (
        await app_conn.fetchval(
            "SELECT count(*) FROM booking.trip_case_checks WHERE trip_case_id = $1::uuid", case["id"]
        )
        == 3
    )


async def test_due_dates_follow_the_start_date(app_conn: asyncpg.Connection) -> None:
    case = await ops.b2c_case(app_conn, status="confirmed")
    checks = await _checks(app_conn, case["id"])
    await app_conn.execute(
        "UPDATE booking.trip_case_checks SET status = 'done', done_at = now() WHERE id = $1::uuid",
        checks["insurance"]["id"],
    )
    custom = await app_conn.fetchval(
        "INSERT INTO booking.trip_case_checks (trip_case_id, code, title, due_date, created_by)"
        " VALUES ($1::uuid, 'custom', 'Visa letter', $2, $3) RETURNING id::text",
        case["id"],
        ops.START - timedelta(days=14),
        STAFF,
    )
    new_start = ops.START + timedelta(days=10)
    await app_conn.execute(
        "UPDATE booking.trip_cases SET start_date = $2::date, end_date = $2::date + 4 WHERE id = $1::uuid",
        case["id"],
        new_start,
    )
    moved = await _checks(app_conn, case["id"])
    assert moved["pre_trip_recheck"]["due_date"] == new_start - timedelta(days=14)
    assert moved["emergency_contact"]["due_date"] == new_start - timedelta(days=14)
    assert moved["insurance"]["due_date"] == ops.START - timedelta(days=14)  # done: kept as it was
    assert await app_conn.fetchval(
        "SELECT due_date FROM booking.trip_case_checks WHERE id = $1::uuid", custom
    ) == ops.START - timedelta(days=14)  # custom: staff's own date


async def test_check_guard(app_conn: asyncpg.Connection) -> None:
    case = await ops.b2c_case(app_conn, status="confirmed")
    checks = await _checks(app_conn, case["id"])
    rid = checks["emergency_contact"]["id"]
    # done <=> done_at
    with pytest.raises(asyncpg.CheckViolationError, match="trip_case_checks_done_chk"):
        await app_conn.execute("UPDATE booking.trip_case_checks SET status = 'done' WHERE id = $1::uuid", rid)
    await app_conn.execute(
        "UPDATE booking.trip_case_checks SET emergency_contact_name = 'Anna Example',"
        " emergency_contact_phone = '+84 90 123 4567', status = 'done', done_at = now(), done_by = $2"
        " WHERE id = $1::uuid",
        rid,
        STAFF,
    )
    # done -> not_applicable is not an edge (reopen first).
    with pytest.raises(asyncpg.CheckViolationError, match="illegal trip check status transition"):
        await app_conn.execute(
            "UPDATE booking.trip_case_checks SET status = 'not_applicable', done_at = NULL, done_by = NULL"
            " WHERE id = $1::uuid",
            rid,
        )
    await app_conn.execute(
        "UPDATE booking.trip_case_checks SET status = 'open', done_at = NULL, done_by = NULL WHERE id = $1::uuid", rid
    )
    with pytest.raises(asyncpg.CheckViolationError, match="trip_case_checks_contact_phone_chk"):
        await app_conn.execute(
            "UPDATE booking.trip_case_checks SET emergency_contact_phone = 'call me' WHERE id = $1::uuid", rid
        )
    # Contact fields only on the emergency_contact row.
    with pytest.raises(asyncpg.CheckViolationError, match="trip_case_checks_contact_row_chk"):
        await app_conn.execute(
            "UPDATE booking.trip_case_checks SET emergency_contact_name = 'X' WHERE id = $1::uuid",
            checks["insurance"]["id"],
        )
    # A standard check keeps its title; code and case are not updatable by role booking at all.
    with pytest.raises(asyncpg.CheckViolationError, match="keeps its title"):
        await app_conn.execute("UPDATE booking.trip_case_checks SET title = 'Other' WHERE id = $1::uuid", rid)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE booking.trip_case_checks SET code = 'custom' WHERE id = $1::uuid", rid)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.trip_case_checks WHERE id = $1::uuid", rid)
    # One standard row of each code; custom rows need a title and may repeat.
    with pytest.raises(asyncpg.UniqueViolationError, match="trip_case_checks_standard_uq"):
        await app_conn.execute(
            "INSERT INTO booking.trip_case_checks (trip_case_id, code, title) VALUES ($1::uuid, 'insurance', 'Again')",
            case["id"],
        )
    with pytest.raises(asyncpg.NotNullViolationError):
        await app_conn.execute(
            "INSERT INTO booking.trip_case_checks (trip_case_id, code) VALUES ($1::uuid, 'custom')", case["id"]
        )
    for _ in range(2):
        await app_conn.execute(
            "INSERT INTO booking.trip_case_checks (trip_case_id, code, title) VALUES ($1::uuid, 'custom', 'Visa')",
            case["id"],
        )
    # Cleared after the trip: the personal fields stay empty for good.
    await app_conn.execute(
        "UPDATE booking.trip_case_checks SET emergency_contact_name = NULL, emergency_contact_phone = NULL,"
        " note = NULL, cleared_at = now() WHERE id = $1::uuid",
        rid,
    )
    with pytest.raises(asyncpg.CheckViolationError, match="trip_case_checks_cleared_chk"):
        await app_conn.execute(
            "UPDATE booking.trip_case_checks SET emergency_contact_name = 'Back' WHERE id = $1::uuid", rid
        )
    with pytest.raises(asyncpg.CheckViolationError, match="cleared is final"):
        await app_conn.execute("UPDATE booking.trip_case_checks SET cleared_at = NULL WHERE id = $1::uuid", rid)


async def test_backfill_seeds_sold_cases_only(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    sold = await ops.b2c_case(app_conn, tag="bf-sold", status="confirmed")
    started = await ops.b2b_case(app_conn, tag="bf-trip", status="confirmed")
    presale = await ops.b2c_case(app_conn, tag="bf-pre", status="building")
    # Cases sold before bk-027: no rows yet.
    await _as_owner(
        admin,
        "DELETE FROM booking.trip_case_checks WHERE trip_case_id = ANY($1::uuid[])",
        [sold["id"], started["id"], presale["id"]],
    )
    await app_conn.execute("UPDATE booking.trip_cases SET status = 'pre_departure' WHERE id = $1::uuid", started["id"])
    await _as_owner(admin, MIGRATION.read_text(encoding="utf-8"))
    assert len(await _checks(app_conn, sold["id"])) == 3
    b2b = await _checks(app_conn, started["id"])
    assert len(b2b) == 3 and b2b["emergency_contact"]["status"] == "not_applicable"
    assert await _checks(app_conn, presale["id"]) == {}


async def test_board_view(app_conn: asyncpg.Connection) -> None:
    sup = await ops.supply(app_conn)
    sold = await ops.sold_case(app_conn, sup, tag="board")
    b2b = await ops.b2b_case(app_conn, tag="board-b2b")
    await app_conn.execute(
        "UPDATE booking.trip_case_checks SET due_date = current_date - 1 WHERE trip_case_id = $1::uuid"
        " AND code = 'insurance'",
        sold["id"],
    )
    rows = {
        r["trip_case_id"]: dict(r)
        for r in await app_conn.fetch(
            "SELECT *, trip_case_id::text AS trip_case_id FROM booking.v_trip_check_board"
            " WHERE trip_case_id = ANY($1::uuid[])",
            [sold["id"], b2b["id"]],
        )
    }
    a, b = rows[sold["id"]], rows[b2b["id"]]
    assert (a["checks_total"], a["checks_open"], a["checks_overdue"]) == (3, 3, 1)
    assert a["supply_state"] == "confirming" and a["suppliers_confirmed"] is False
    assert a["payment_verified"] is None
    assert a["next_due_date"] == await app_conn.fetchval("SELECT current_date - 1")
    assert (b["checks_open"], b["checks_not_applicable"]) == (2, 1)
    assert b["supply_state"] == "none" and b["suppliers_confirmed"] is True
    # Cases outside confirmed .. on_trip are not on the board.
    await app_conn.execute("UPDATE booking.trip_cases SET status = 'cancelled' WHERE id = $1::uuid", b2b["id"])
    assert not await app_conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM booking.v_trip_check_board WHERE trip_case_id = $1::uuid)", b2b["id"]
    )


async def test_reminder_dedupe_index(app_conn: asyncpg.Connection) -> None:
    case = await ops.b2b_case(app_conn, tag="remind")
    sql = (
        "INSERT INTO booking.notifications (recipient_type, recipient_id, type, title, entity_type, entity_id, data)"
        " VALUES ($1, $2::uuid, 'trip_check.due', 't', 'trip_case', $3::uuid, $4::jsonb)"
    )
    data = '{"reminder_key": "trip_check.due:x:1"}'
    await app_conn.execute(sql, "staff", None, case["id"], data)
    with pytest.raises(asyncpg.UniqueViolationError, match="notifications_reminder_uq"):
        await app_conn.execute(sql, "staff", None, case["id"], data)
    # The customer row of the same reminder does not collide with the staff row (critic B7).
    customer = await app_conn.fetchval(
        "INSERT INTO booking.customers (email) VALUES ('it-682-remind@example.test') RETURNING id::text"
    )
    await app_conn.execute(sql, "customer", customer, case["id"], data)
    # Rows without a reminder key never collide.
    for _ in range(2):
        await app_conn.execute(sql, "staff", None, case["id"], "{}")
