"""Clean-up shared by the bk-010 integration tests (hotels, room types, services, supplier rates).

Rates of a published contract are fixed and sent / approved version items are locked by triggers, and role
``booking`` may not delete hotels or services at all. The owner of the tables (the live owner of schema booking:
``aa_cis_admin`` before bk-014, ``bk_owner`` after it, X1) switches the user triggers off for the clean-up only,
inside one transaction, so the next test module (bk-008 deletes every supplier and contract) starts from tables with
no bk-010 rows pointing at them.
"""

from __future__ import annotations

import asyncpg

TRIGGERED = ("booking.version_item_costs", "booking.trip_case_version_itinerary", "booking.supplier_rates")


async def purge_supply(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        owner = await conn.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
        if owner != await conn.fetchval("SELECT current_user"):
            await conn.execute(f'SET LOCAL ROLE "{owner}"')
        # bk-026 (AA-682): supplier bookings point at suppliers, room types, services, cases and version items.
        if await conn.fetchval("SELECT to_regclass('booking.supplier_bookings') IS NOT NULL"):
            await conn.execute("UPDATE booking.supplier_bookings SET voucher_file_id = NULL")
            await conn.execute("DELETE FROM booking.ops_files")
            await conn.execute("DELETE FROM booking.supplier_bookings")
        for table in TRIGGERED:
            await conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        await conn.execute(
            "UPDATE booking.version_item_costs SET supplier_rate_id = NULL WHERE supplier_rate_id IS NOT NULL"
        )
        await conn.execute(
            "UPDATE booking.trip_case_version_itinerary SET hotel_room_type_id = NULL, service_id = NULL"
            " WHERE hotel_room_type_id IS NOT NULL OR service_id IS NOT NULL"
        )
        await conn.execute("DELETE FROM booking.supplier_rates")
        for table in TRIGGERED:
            await conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        # bk-019 (AA-972): plan components point at room types and services.
        await conn.execute("DELETE FROM booking.trip_plan_components")
        # bk-023 (AA-835): supplier photos name a hotel or a service (no FK, so they would outlive them).
        await conn.execute("DELETE FROM booking.entity_media")
        await conn.execute("DELETE FROM booking.hotel_room_types")
        await conn.execute("DELETE FROM booking.hotels")
        await conn.execute("DELETE FROM booking.services")
