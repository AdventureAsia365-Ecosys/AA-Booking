"""bk-009 (AA-679, S237 D4) on a real PostgreSQL: booking.trip_plans is a commercial-only row per CIS catalog tour,
unique on (source_system, source_id), never deleted by role ``booking``, and it copies no catalog content."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.conftest import MIGRATIONS

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BK009 = next(p for p in MIGRATIONS if p.name.startswith("009_"))
STAFF = "it-staff-679"
SOURCE_ID = "6f1c3c1e-0000-4000-8000-000000000679"

# Catalog content columns of catalog.v_tour / ERD v2 trip_plans that must never be copied into Booking (O7).
CONTENT_COLUMNS = {"title", "slug", "subtitle", "summary", "description", "highlights", "itinerary_text", "days",
                   "what_it_has", "what_it_does_not_have", "cover_photo_path", "tour_id"}  # fmt: skip


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
        await conn.execute("DELETE FROM booking.trip_plans WHERE source_id LIKE '6f1c3c1e-%'")
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_registered_and_reapply_is_a_noop(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-009'") == 1
    await admin.execute(
        "INSERT INTO booking.trip_plans (source_id, sellable, created_by) VALUES ($1, true, $2)", SOURCE_ID, STAFF
    )
    await admin.execute(BK009.read_text(encoding="utf-8"))
    assert await admin.fetchval("SELECT sellable FROM booking.trip_plans WHERE source_id = $1", SOURCE_ID) is True
    assert await admin.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version = 'bk-009'") == 1


async def test_no_catalog_content_columns(admin: asyncpg.Connection) -> None:
    columns = {
        r["column_name"]
        for r in await admin.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'booking' AND table_name = 'trip_plans'"
        )
    }
    assert "source_id" in columns and "sellable" in columns
    assert not (columns & CONTENT_COLUMNS), f"content copied into booking.trip_plans: {columns & CONTENT_COLUMNS}"


async def test_one_row_per_catalog_tour(app_conn: asyncpg.Connection) -> None:
    await app_conn.execute("INSERT INTO booking.trip_plans (source_id, created_by) VALUES ($1, $2)", SOURCE_ID, STAFF)
    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.execute("INSERT INTO booking.trip_plans (source_id) VALUES ($1)", SOURCE_ID)
    # The upsert the API uses touches the same row.
    row = await app_conn.fetchrow(
        "INSERT INTO booking.trip_plans AS p (source_id, sellable, updated_by) VALUES ($1, true, $2)"
        " ON CONFLICT (source_system, source_id)"
        " DO UPDATE SET sellable = EXCLUDED.sellable, updated_by = EXCLUDED.updated_by"
        " RETURNING (xmax = 0) AS inserted, sellable",
        SOURCE_ID,
        STAFF,
    )
    assert row["inserted"] is False and row["sellable"] is True
    assert await app_conn.fetchval("SELECT count(*) FROM booking.trip_plans WHERE source_id = $1", SOURCE_ID) == 1


async def test_app_role_cannot_delete(app_conn: asyncpg.Connection) -> None:
    await app_conn.execute("INSERT INTO booking.trip_plans (source_id) VALUES ($1)", SOURCE_ID)
    for sql in ("DELETE FROM booking.trip_plans WHERE source_id = $1", "TRUNCATE booking.trip_plans"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(sql, *([SOURCE_ID] if "$1" in sql else []))
    await app_conn.execute("UPDATE booking.trip_plans SET sellable = false WHERE source_id = $1", SOURCE_ID)


HINT = "source_id, price_hint_amount, price_hint_currency, price_hint_basis"


@pytest.mark.parametrize(
    ("columns", "values"),
    [
        ("source_system, source_id", "'tripplanner', '6f1c3c1e-x'"),
        ("source_id", "'   '"),
        ("source_id, trip_style", f"'{SOURCE_ID}', ''"),
        ("source_id, default_channel", f"'{SOURCE_ID}', 'retail'"),
        ("source_id, price_hint_amount", f"'{SOURCE_ID}', 1200"),  # amount without currency / basis
        (HINT, f"'{SOURCE_ID}', 0, 'USD', 'per_person'"),
        (HINT, f"'{SOURCE_ID}', 10, 'usd', 'per_person'"),
        (HINT, f"'{SOURCE_ID}', 10, 'USD', 'per_room'"),
    ],
    ids=["system", "blank_id", "blank_style", "channel", "partial_hint", "zero", "currency", "basis"],
)
async def test_checks(app_conn: asyncpg.Connection, columns: str, values: str) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(f"INSERT INTO booking.trip_plans ({columns}) VALUES ({values})")  # noqa: S608 — test


async def test_full_commercial_row_and_updated_at(app_conn: asyncpg.Connection) -> None:
    row = await app_conn.fetchrow(
        "INSERT INTO booking.trip_plans (source_id, sellable, trip_style, default_channel, price_hint_amount,"
        " price_hint_currency, price_hint_basis, commercial_notes, created_by, updated_by)"
        " VALUES ($1, true, 'Private', 'b2c_tailor_made', 2450.00, 'USD', 'per_person', 'net from DMC', $2, $2)"
        " RETURNING updated_at",
        SOURCE_ID,
        STAFF,
    )
    later = await app_conn.fetchval(
        "UPDATE booking.trip_plans SET trip_style = 'Small group' WHERE source_id = $1 RETURNING updated_at", SOURCE_ID
    )
    assert later >= row["updated_at"]
