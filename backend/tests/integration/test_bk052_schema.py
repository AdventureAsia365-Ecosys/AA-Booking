"""AA-1088 (bk-052, S278 lane l18) on a real PostgreSQL: the version locks keep refusing every write to an approved
version and its items, and open exactly one door for the one-off legacy B2B display fix.

Registration and re-run no-op; both functions are bk_owner's and carry the exception; without the setting an approved
version or item is refused as before (same SQLSTATE and message); with the setting a B2B version with no
confirmed hold (so no placeholder to replace) stays locked.
The B2B paths (placeholder title, longer span, everything else refused) run end to end with the app code in the code
PR's tests/integration/test_aa1088_legacy_cases.py. All names are invented.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "052_b2b_legacy_display_fix.sql"
FUNCTIONS = ("trip_case_version_lock", "trip_case_version_item_lock")
NOTE = "it-bk052"
TENANT = "00000000-0000-4000-8000-0000000b0056"
BODIES = (
    "SELECT p.proname, p.prosrc, pg_get_userbyid(p.proowner) AS owner FROM pg_proc p"
    " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'booking' AND p.proname = ANY($1::text[])"
    " ORDER BY p.proname"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def approved(admin: asyncpg.Connection) -> AsyncIterator[dict[str, str]]:
    """A B2B trip case with no hold, an approved version and one item (seeded as bk_owner)."""
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        case = await admin.fetchval(
            "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, start_date, end_date, pax, currency,"
            " source_system, notes) VALUES ('b2b', $3::uuid, 'confirmed', $1, $1, 2, 'USD', 'crm', $2)"
            " RETURNING id::text",
            date(2027, 4, 2),
            NOTE,
            TENANT,
        )
        version = await admin.fetchval(
            "INSERT INTO booking.trip_case_versions (trip_case_id, version_number, title, channel)"
            " VALUES ($1::uuid, 1, 'Willowmere Week', 'b2b') RETURNING id::text",
            case,
        )
        item = await admin.fetchval(
            "INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, position, title, day_from, day_to)"
            " VALUES ($1::uuid, 1, 'Willowmere Week', 1, 1) RETURNING id::text",
            version,
        )
        await admin.execute(
            "UPDATE booking.trip_case_versions SET is_approved = true, approved_at = now() WHERE id = $1::uuid",
            version,
        )
    try:
        yield {"case": case, "version": version, "item": item}
    finally:
        async with admin.transaction():
            await admin.execute("SET LOCAL ROLE bk_owner")
            for t in ("booking.trip_case_versions", "booking.trip_case_version_itinerary"):
                await admin.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
            await admin.execute("DELETE FROM booking.trip_case_version_itinerary WHERE id = $1::uuid", item)
            await admin.execute("DELETE FROM booking.trip_case_versions WHERE id = $1::uuid", version)
            for t in ("booking.trip_case_versions", "booking.trip_case_version_itinerary"):
                await admin.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
            await admin.execute("DELETE FROM booking.trip_case_status_history WHERE trip_case_id = $1::uuid", case)
            await admin.execute("DELETE FROM booking.trip_cases WHERE id = $1::uuid", case)


async def test_registered_and_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    assert await admin.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-052'") == 1
    before = [dict(r) for r in await admin.fetch(BODIES, list(FUNCTIONS))]
    await admin.execute("SET ROLE bk_owner")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    await admin.execute("RESET ROLE")
    assert [dict(r) for r in await admin.fetch(BODIES, list(FUNCTIONS))] == before


async def test_both_functions_carry_the_exception_and_stay_bk_owners(admin: asyncpg.Connection) -> None:
    rows = await admin.fetch(BODIES, list(FUNCTIONS))
    assert [r["proname"] for r in rows] == sorted(FUNCTIONS)
    for r in rows:
        assert r["owner"] == "bk_owner"
        assert "current_setting('booking.legacy_display_fix', true) = 'aa1088'" in r["prosrc"]
        assert "approved and locked" in r["prosrc"]


async def test_an_approved_version_stays_locked(
    admin: asyncpg.Connection, approved: dict[str, str], migrated_booking_dsn: str
) -> None:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        for setting in (None, "aa1088"):
            async with conn.transaction():
                if setting:
                    await conn.execute("SELECT set_config('booking.legacy_display_fix', $1, true)", setting)
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="approved and locked"):
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE booking.trip_case_versions SET title = 'Other' WHERE id = $1::uuid",
                            approved["version"],
                        )
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="approved and locked"):
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE booking.trip_case_version_itinerary SET day_to = 3 WHERE id = $1::uuid",
                            approved["item"],
                        )
                with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="approved and locked"):
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM booking.trip_case_version_itinerary WHERE id = $1::uuid", approved["item"]
                        )
    finally:
        await conn.close()
