"""Shared set-up and clean-up of the AA-682 integration tests (bk-026 .. bk-028): a sold B2C trip case with an approved
version of two supply items (a room type and a service of one supplier, with cost rows), helpers for a sent invoice
(bk-028, in SQL as the service writes it), and the purge of every supplier booking / operations file / invoice /
payment so the other modules' clean-ups (which delete cases, hotels and suppliers) never meet a row that points at
them."""

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
    """Every bk-026 row, plus the B2C cases, versions, customers and supply of these tests (bk-027 trip checks go
    with their case: ON DELETE CASCADE)."""
    async with conn.transaction():
        await _as_owner(conn)
        await conn.execute(
            "DELETE FROM booking.notifications WHERE type LIKE 'supplier_booking.%' OR type LIKE 'trip_check.%'"
            " OR type = 'trip_case.gate_blocked'"
        )
        await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type = 'trip_case_check'")
        await conn.execute(
            "UPDATE booking.supplier_bookings SET voucher_file_id = NULL WHERE voucher_file_id IS NOT NULL"
        )
        await purge_billing(conn)
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


BILLING = ("booking.payments", "booking.payment_schedule", "booking.invoice_items", "booking.invoices")


async def purge_billing(conn: asyncpg.Connection) -> None:
    """Every bk-028 row (payments, schedules, lines, invoices) with their guards off: verified payments and sent
    invoices are never deleted by the app. Runs inside :func:`purge_ops`'s owner transaction (or alone)."""
    if await conn.fetchval("SELECT to_regclass('booking.invoices')") is None:
        return
    await conn.execute("DELETE FROM booking.notifications WHERE type LIKE 'invoice.%' OR type LIKE 'payment.%'")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type IN ('invoice', 'payment')")
    for table in BILLING:
        await conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
    try:
        for table in BILLING:
            await conn.execute(f"DELETE FROM {table}")  # noqa: S608 — constant names
    finally:
        for table in BILLING:
            await conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")


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


async def b2b_case(
    conn: asyncpg.Connection, *, tag: str = "b2b", status: str = "confirmed", start: date | None = START, days: int = 4
) -> dict[str, Any]:
    """A B2B trip case (no customer, an agency tenant) inserted at ``status``, the way ``holds._open_case`` inserts a
    confirmed one; no version (nothing to book: supply state ``none``)."""
    row = await conn.fetchrow(
        "INSERT INTO booking.trip_cases (channel, party_tenant_id, status, start_date, end_date, pax, currency,"
        " source_system, notes) VALUES ('b2b', gen_random_uuid(), $1, $2, $3, 2, 'USD', 'crm', $4)"
        " RETURNING id::text AS id, code",
        status,
        start,
        start + timedelta(days=days) if start else None,
        f"{NOTE}-{tag}",
    )
    return dict(row)


async def b2c_case(
    conn: asyncpg.Connection, *, tag: str = "b2c", status: str = "proposal_sent", start: date | None = START
) -> dict[str, Any]:
    """A B2C trip case of a fresh customer inserted at ``status`` (no version)."""
    customer = await conn.fetchval(
        "INSERT INTO booking.customers (email) VALUES ($1) RETURNING id::text", f"it-682-{tag}@example.test"
    )
    row = await conn.fetchrow(
        "INSERT INTO booking.trip_cases (customer_id, status, start_date, end_date, pax, currency, notes)"
        " VALUES ($1::uuid, $2, $3, $4, 2, 'USD', $5) RETURNING id::text AS id, code",
        customer,
        status,
        start,
        start + timedelta(days=4) if start else None,
        f"{NOTE}-{tag}",
    )
    return dict(row)


# ── Invoices (bk-028), written in SQL the way app.ops.billing writes them ────────


def key(tag: str) -> str:
    """An Idempotency-Key and the request hash that goes with it (any 64 hex chars in SQL-level tests)."""
    return f"it-682-{tag}".ljust(8, "x")


SHA = "0" * 64


async def draft_invoice(
    conn: asyncpg.Connection,
    case: dict[str, Any],
    *,
    by: str,
    schedule: tuple[tuple[str, str, date], ...] | None = None,
    adjustments: tuple[tuple[str, str], ...] = (),
    tag: str = "inv",
) -> dict[str, Any]:
    """A draft invoice of a sold B2C case (``sold_case``: V1 2400 USD): the version total line, the adjustments
    ``(description, unit_price)`` and the schedule rows ``(kind, amount, due_date)`` (default 30 / 70)."""
    customer = await conn.fetchval("SELECT customer_id::text FROM booking.trip_cases WHERE id = $1::uuid", case["id"])
    inv = await conn.fetchrow(
        "INSERT INTO booking.invoices (trip_case_id, version_id, customer_id, currency, idempotency_key,"
        " request_sha256, created_by, updated_by) VALUES ($1::uuid, $2::uuid, $3::uuid, 'USD', $4, $5, $6, $6)"
        " RETURNING id::text AS id, invoice_number",
        case["id"],
        case["version"],
        customer,
        key(tag),
        SHA,
        by,
    )
    await conn.execute(
        "INSERT INTO booking.invoice_items (invoice_id, position, kind, description, quantity, unit_price, amount)"
        " VALUES ($1::uuid, 1, 'version_total', 'Trip price', 1, 2400, 2400)",
        inv["id"],
    )
    for i, (description, unit) in enumerate(adjustments, start=2):
        await conn.execute(
            "INSERT INTO booking.invoice_items (invoice_id, position, kind, description, quantity, unit_price, amount,"
            " reason) VALUES ($1::uuid, $2, 'adjustment', $3, 1, $4::numeric, $4::numeric, 'test')",
            inv["id"],
            i,
            description,
            unit,
        )
    due = START - timedelta(days=30)
    rows = schedule or (("deposit", "720.00", date.today() + timedelta(days=3)), ("final", "1680.00", due))
    for seq, (kind, amount, when) in enumerate(rows, start=1):
        await conn.execute(
            "INSERT INTO booking.payment_schedule (invoice_id, seq, kind, amount, due_date)"
            " VALUES ($1::uuid, $2, $3, $4::numeric, $5)",
            inv["id"],
            seq,
            kind,
            amount,
            when,
        )
    return dict(inv)


async def send_invoice(conn: asyncpg.Connection, invoice_id: str, *, by: str) -> None:
    await conn.execute(
        "UPDATE booking.invoices SET status = 'sent', issued_at = now(), sent_by = $2, bill_to_name = 'Test Customer',"
        " bill_to_email = 'it-682@example.test' WHERE id = $1::uuid",
        invoice_id,
        by,
    )


async def record_payment(
    conn: asyncpg.Connection,
    invoice_id: str,
    amount: str,
    *,
    by: str,
    kind: str = "receipt",
    method: str = "cash",
    tag: str | None = None,
) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.payments (invoice_id, kind, amount, currency, method, received_on, idempotency_key,"
        " request_sha256, recorded_by) VALUES ($1::uuid, $2, $3::numeric, 'USD', $4, current_date, $5, $6, $7)"
        " RETURNING id::text",
        invoice_id,
        kind,
        amount,
        method,
        key(tag or f"{kind}-{amount}-{by}"),
        SHA,
        by,
    )


async def verify_payment(conn: asyncpg.Connection, payment_id: str, *, by: str) -> None:
    await conn.execute(
        "UPDATE booking.payments SET verified_by = $2, verified_at = now() WHERE id = $1::uuid", payment_id, by
    )
