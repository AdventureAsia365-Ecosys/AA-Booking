"""Shared set-up and clean-up of the AA-682 S1 integration tests (bk-026): a sold B2C trip case with an approved
version of two supply items (a room type and a service of one supplier, with cost rows), and the purge of every
supplier booking / operations file so the other modules' clean-ups (which delete cases, hotels and suppliers) never
meet a row that points at them."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

NOTE = "it-682"
LOCKED = ("booking.version_item_costs", "booking.trip_case_version_itinerary", "booking.trip_case_versions")
START = date.today() + timedelta(days=40)


async def _as_owner(conn: asyncpg.Connection) -> None:
    owner = await conn.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
    if owner != await conn.fetchval("SELECT current_user"):
        await conn.execute(f'SET LOCAL ROLE "{owner}"')


async def purge_ops(conn: asyncpg.Connection) -> None:
    """Every bk-026 row, plus the B2C cases, versions, customers and supply of these tests."""
    async with conn.transaction():
        await _as_owner(conn)
        await conn.execute("DELETE FROM booking.notifications WHERE type LIKE 'supplier_booking.%'")
        await conn.execute(
            "UPDATE booking.supplier_bookings SET voucher_file_id = NULL WHERE voucher_file_id IS NOT NULL"
        )
        await conn.execute("DELETE FROM booking.ops_files")
        await conn.execute("DELETE FROM booking.supplier_bookings")
        await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type IN ('supplier_booking', 'ops_file')")
        for table in LOCKED:
            await conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        cases = "SELECT id FROM booking.trip_cases WHERE notes LIKE 'it-682%'"
        versions = f"SELECT id FROM booking.trip_case_versions WHERE trip_case_id IN ({cases})"  # noqa: S608
        items = f"SELECT id FROM booking.trip_case_version_itinerary WHERE trip_case_version_id IN ({versions})"  # noqa: S608
        await conn.execute(f"DELETE FROM booking.version_item_costs WHERE version_item_id IN ({items})")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.trip_case_version_itinerary WHERE id IN ({items})")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.trip_case_versions WHERE id IN ({versions})")  # noqa: S608
        for table in LOCKED:
            await conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        await conn.execute(f"DELETE FROM booking.trip_case_status_history WHERE trip_case_id IN ({cases})")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.audit_logs WHERE entity_id IN ({cases})")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.notifications WHERE entity_id IN ({cases})")  # noqa: S608
        await conn.execute("DELETE FROM booking.trip_cases WHERE notes LIKE 'it-682%'")
        await conn.execute("DELETE FROM booking.customers WHERE email LIKE 'it-682-%'")
        await conn.execute(
            "DELETE FROM booking.hotel_room_types WHERE hotel_id IN"
            " (SELECT h.id FROM booking.hotels h JOIN booking.suppliers s ON s.id = h.supplier_id"
            "  WHERE s.company_name LIKE 'it-682 %')"
        )
        await conn.execute(
            "DELETE FROM booking.hotels WHERE supplier_id IN"
            " (SELECT id FROM booking.suppliers WHERE company_name LIKE 'it-682 %')"
        )
        await conn.execute(
            "DELETE FROM booking.services WHERE supplier_id IN"
            " (SELECT id FROM booking.suppliers WHERE company_name LIKE 'it-682 %')"
        )
        await conn.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-682 %'")


async def staff(conn: asyncpg.Connection, *rows: tuple[str, str]) -> None:
    for staff_id, role in rows:
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, $2, true, $1)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
            staff_id,
            role,
        )


async def supply(conn: asyncpg.Connection, name: str = "Delta") -> dict[str, str]:
    """One active supplier with a hotel room type and a service."""
    sid = await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code) VALUES ($1, ARRAY['hotel','activity'],"
        " 'VN') RETURNING id::text",
        f"it-682 {name}",
    )
    hotel = await conn.fetchval(
        "INSERT INTO booking.hotels (supplier_id, name) VALUES ($1::uuid, $2) RETURNING id::text", sid, f"{name} Hotel"
    )
    room = await conn.fetchval(
        "INSERT INTO booking.hotel_room_types (hotel_id, name) VALUES ($1::uuid, 'Deluxe') RETURNING id::text", hotel
    )
    service = await conn.fetchval(
        "INSERT INTO booking.services (supplier_id, service_type, title) VALUES ($1::uuid, 'activity', $2)"
        " RETURNING id::text",
        sid,
        f"{name} kayak",
    )
    return {"supplier": sid, "hotel": hotel, "room": room, "service": service}


async def sold_case(
    conn: asyncpg.Connection, sup: dict[str, str], *, tag: str = "a", costs: tuple[str, ...] = ("USD", "USD")
) -> dict[str, Any]:
    """A B2C trip case at ``confirmed`` with an approved V1: item 1 = 2 nights of the room (days 1-3: in day 1, out
    day 3, the B2C pricing rule ``supply_rates.quantity('room_night')`` = day_to - day_from; cost rows in
    ``costs``), item 2 = the service on day 3, item 3 = a free-text item (no supply)."""
    customer = await conn.fetchval(
        "INSERT INTO booking.customers (email) VALUES ($1) RETURNING id::text", f"it-682-{tag}@example.test"
    )
    case = await conn.fetchrow(
        "INSERT INTO booking.trip_cases (customer_id, status, start_date, end_date, pax, currency, notes)"
        " VALUES ($1::uuid, 'confirmed', $2, $3, 2, 'USD', $4) RETURNING id::text AS id, code",
        customer,
        START,
        START + timedelta(days=4),
        f"{NOTE}-{tag}",
    )
    version = await conn.fetchval(
        "INSERT INTO booking.trip_case_versions (trip_case_id, version_number, total_price, currency)"
        " VALUES ($1::uuid, 1, 2400, 'USD') RETURNING id::text",
        case["id"],
    )
    room_item = await conn.fetchval(
        "INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, position, title, day_from, day_to,"
        " hotel_room_type_id) VALUES ($1::uuid, 1, 'Two nights', 1, 3, $2::uuid) RETURNING id::text",
        version,
        sup["room"],
    )
    service_item = await conn.fetchval(
        "INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, position, title, day_from, day_to,"
        " service_id) VALUES ($1::uuid, 2, 'Kayak', 3, 3, $2::uuid) RETURNING id::text",
        version,
        sup["service"],
    )
    await conn.execute(
        "INSERT INTO booking.trip_case_version_itinerary (trip_case_version_id, position, title, day_from, day_to)"
        " VALUES ($1::uuid, 3, 'Free day', 4, 4)",
        version,
    )
    for currency, amount in zip(costs, (Decimal("300.00"), Decimal("100.50")), strict=False):
        await conn.execute(
            "INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency)"
            " VALUES ($1::uuid, $2, $3)",
            room_item,
            amount,
            currency,
        )
    await conn.execute(
        "INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency)"
        " VALUES ($1::uuid, 80, 'USD')",
        service_item,
    )
    await conn.execute(
        "UPDATE booking.trip_case_versions SET is_approved = true, approved_at = now() WHERE id = $1::uuid", version
    )
    return {**dict(case), "version": version, "room_item": room_item, "service_item": service_item}
