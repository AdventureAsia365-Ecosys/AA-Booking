"""bk-055 (AA-1091, root ADR 0005b d4) on a real PostgreSQL: season calendars, season and stop-sale ranges, holiday
events and the trip plan's calendar. Maker-checker in CHECKs (approver and rejecter are never the maker; a removal is
approved by someone else than who asked for it), a reason on every proposal, withdrawal, removal request and
rejection, fixed dates or a holiday event (festive only), one live event per country, code and year, a plan's
calendar change as a request with a reason, and role booking deletes or redates nothing. Every test runs in one
transaction logged in as role booking and is rolled back. Names and dates are invented. No import of app code."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import apply_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER = "it-bk055-maker"
CHECKER = "it-bk055-checker"
DEST = "d0000000-0000-4000-8000-0000000000aa"


@contextlib.asynccontextmanager
async def refused(c: asyncpg.Connection, exc: type[Exception], match: str | None = None) -> AsyncIterator[None]:
    with pytest.raises(exc, match=match):
        async with c.transaction():
            yield


@pytest_asyncio.fixture
async def app(migrated_admin_dsn: str, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    admin = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(admin, only=[54])
    finally:
        await admin.close()
    c = await asyncpg.connect(migrated_booking_dsn)
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


async def _calendar(c: asyncpg.Connection) -> str:
    return str(
        await c.fetchval(
            "INSERT INTO booking.season_calendars (owner_kind, destination_id, name, created_by)"
            " VALUES ('destination', $1::uuid, 'Test bay', $2) RETURNING id",
            DEST,
            MAKER,
        )
    )


async def test_calendar_ranges_and_plan(app: asyncpg.Connection) -> None:
    cal = await _calendar(app)
    assert await app.fetchval("SELECT default_season FROM booking.season_calendars WHERE id = $1::uuid", cal) == "low"
    async with refused(app, asyncpg.UniqueViolationError):
        await _calendar(app)
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "INSERT INTO booking.season_calendars (owner_kind, name, created_by) VALUES ('destination', 'x', $1)",
            MAKER,
        )
    # A proposal carries its reason (note).
    async with refused(app, asyncpg.NotNullViolationError):
        await app.execute(
            "INSERT INTO booking.season_ranges (calendar_id, kind, season_code, date_from, date_to, created_by)"
            " VALUES ($1::uuid, 'season', 'high', $2, $3, $4)",
            cal,
            date(2031, 11, 1),
            date(2032, 3, 1),
            MAKER,
        )
    high = await app.fetchval(
        "INSERT INTO booking.season_ranges (calendar_id, kind, season_code, date_from, date_to, note, created_by)"
        " VALUES ($1::uuid, 'season', 'high', $2, $3, 'Dry season demand', $4) RETURNING id",
        cal,
        date(2031, 11, 1),
        date(2032, 3, 1),
        MAKER,
    )
    # A festive range names a holiday event instead of dates; never both, and festive only.
    await app.execute(
        "INSERT INTO booking.season_ranges (calendar_id, kind, season_code, holiday_country, holiday_code,"
        " days_before, days_after, note, created_by) VALUES ($1::uuid, 'season', 'festive', 'VN', 'lunar_new_year', 2,"
        " 3, 'New year week', $2)",
        cal,
        MAKER,
    )
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "INSERT INTO booking.season_ranges (calendar_id, kind, season_code, holiday_country, holiday_code,"
            " note, created_by) VALUES ($1::uuid, 'season', 'high', 'VN', 'lunar_new_year', 'x', $2)",
            cal,
            MAKER,
        )
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "INSERT INTO booking.season_ranges (calendar_id, kind, season_code, date_from, date_to, note, created_by)"
            " VALUES ($1::uuid, 'stop_sale', 'peak', $2, $3, 'x', $4)",
            cal,
            date(2031, 12, 20),
            date(2031, 12, 27),
            MAKER,
        )
    # Maker-checker.
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.season_ranges SET status = 'approved', decided_by = $2, decided_at = now() WHERE id = $1",
            high,
            MAKER,
        )
    await app.execute(
        "UPDATE booking.season_ranges SET status = 'approved', decided_by = $2, decided_at = now() WHERE id = $1",
        high,
        CHECKER,
    )
    # A removal request carries its reason.
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.season_ranges SET end_requested_by = $2, end_requested_at = now() WHERE id = $1",
            high,
            CHECKER,
        )
    await app.execute(
        "UPDATE booking.season_ranges SET end_requested_by = $2, end_requested_at = now(),"
        " end_reason = 'Season moved' WHERE id = $1",
        high,
        CHECKER,
    )
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.season_ranges SET ended_by = $2, ended_at = now() WHERE id = $1", high, CHECKER
        )
    await app.execute("UPDATE booking.season_ranges SET ended_by = $2, ended_at = now() WHERE id = $1", high, MAKER)
    async with refused(app, asyncpg.InsufficientPrivilegeError):
        await app.execute("UPDATE booking.season_ranges SET date_to = date_to + 1 WHERE id = $1", high)
    async with refused(app, asyncpg.InsufficientPrivilegeError):
        await app.execute("DELETE FROM booking.season_ranges WHERE id = $1", high)

    # A withdrawal carries its reason.
    stop = await app.fetchval(
        "INSERT INTO booking.season_ranges (calendar_id, kind, date_from, date_to, note, created_by)"
        " VALUES ($1::uuid, 'stop_sale', $2, $3, 'Allotment closed', $4) RETURNING id",
        cal,
        date(2031, 12, 20),
        date(2031, 12, 27),
        MAKER,
    )
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.season_ranges SET status = 'withdrawn', decided_by = $2, decided_at = now() WHERE id = $1",
            stop,
            MAKER,
        )
    await app.execute(
        "UPDATE booking.season_ranges SET status = 'withdrawn', decided_by = $2, decided_at = now(),"
        " withdraw_reason = 'Entered twice' WHERE id = $1",
        stop,
        MAKER,
    )
    # A default season change carries its reason.
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.season_calendars SET pending_default_season = 'high', default_requested_by = $2,"
            " default_requested_at = now() WHERE id = $1::uuid",
            cal,
            MAKER,
        )

    plan = await app.fetchval(
        "INSERT INTO booking.trip_plans (source_system, source_id, sellable, season_calendar_id)"
        " VALUES ('cis', $1, false, $2::uuid) RETURNING id",
        f"it-bk055-{uuid.uuid4().hex[:8]}",
        cal,
    )
    assert await app.fetchval("SELECT season_calendar_id::text FROM booking.trip_plans WHERE id = $1", plan) == cal
    # A change of the plan's calendar is a request: who, when and why together.
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.trip_plans SET pending_season_calendar_id = $2::uuid WHERE id = $1", plan, cal
        )
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.trip_plans SET season_calendar_requested_by = $2, season_calendar_requested_at = now()"
            " WHERE id = $1",
            plan,
            MAKER,
        )
    await app.execute(
        "UPDATE booking.trip_plans SET season_calendar_requested_by = $2, season_calendar_requested_at = now(),"
        " season_calendar_request_reason = 'Starts in the gateway city' WHERE id = $1",
        plan,
        MAKER,
    )


async def test_holiday_events(app: asyncpg.Connection) -> None:
    sql = (
        "INSERT INTO booking.holiday_events (country_iso2, code, name, year, date_from, date_to, reason, created_by)"
        " VALUES ('VN', 'lunar_new_year', 'Lunar New Year', $1, $2, $3, 'Official calendar', $4) RETURNING id"
    )
    async with refused(app, asyncpg.CheckViolationError):
        await app.fetchval(sql.replace("'Official calendar'", "' '"), 2034, date(2034, 2, 1), date(2034, 2, 3), MAKER)
    event = await app.fetchval(sql, 2031, date(2031, 1, 22), date(2031, 1, 27), MAKER)
    async with refused(app, asyncpg.UniqueViolationError):
        await app.fetchval(sql, 2031, date(2031, 1, 23), date(2031, 1, 28), MAKER)
    async with refused(app, asyncpg.CheckViolationError):
        await app.fetchval(sql, 2032, date(2031, 12, 30), date(2032, 1, 2), MAKER)
    async with refused(app, asyncpg.ForeignKeyViolationError):
        await app.fetchval(sql.replace("'VN'", "'ZZ'"), 2033, date(2033, 1, 1), date(2033, 1, 2), MAKER)
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.holiday_events SET status = 'rejected', decided_by = $2, decided_at = now() WHERE id = $1",
            event,
            CHECKER,
        )
    await app.execute(
        "UPDATE booking.holiday_events SET status = 'rejected', decided_by = $2, decided_at = now(),"
        " reject_reason = 'Wrong dates' WHERE id = $1",
        event,
        CHECKER,
    )
    # A rejected row frees the key: the corrected row can be entered.
    fixed = await app.fetchval(sql, 2031, date(2031, 1, 23), date(2031, 1, 28), MAKER)
    async with refused(app, asyncpg.CheckViolationError):
        await app.execute(
            "UPDATE booking.holiday_events SET status = 'withdrawn', decided_by = $2, decided_at = now() WHERE id = $1",
            fixed,
            MAKER,
        )
    await app.execute(
        "UPDATE booking.holiday_events SET status = 'withdrawn', decided_by = $2, decided_at = now(),"
        " withdraw_reason = 'Duplicate' WHERE id = $1",
        fixed,
        MAKER,
    )
    async with refused(app, asyncpg.InsufficientPrivilegeError):
        await app.execute("DELETE FROM booking.holiday_events WHERE id = $1", event)
