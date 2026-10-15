"""bk-010 (AA-823) on a real PostgreSQL, as role ``booking``: hotel / service codes and checks, no deletes, no
customer-facing columns (Q17), the supplier-rate guard (draft contracts only, contract currency and validity, same
supplier) and the reverse guard on contracts, and the supply references on version items.

Schema-level only (no API code): it ships with the migration-only PR so the DB rules are proven before apply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from tests.integration.supply import purge_supply

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER = "it-823-maker"
CHECKER = "it-823-checker"
MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "010_hotels_services_rates.sql"
PUBLISH = (
    "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
    " WHERE id = $1::uuid"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active)"
        " VALUES ($1, 'contracting', true), ($2, 'admin', true) ON CONFLICT (cis_user_id) DO NOTHING",
        MAKER,
        CHECKER,
    )
    await purge_supply(conn)
    try:
        yield conn
    finally:
        await purge_supply(conn)
        await conn.execute("DELETE FROM booking.supplier_contracts")
        await conn.execute("DELETE FROM booking.suppliers")
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _insert(conn: asyncpg.Connection, table: str, **cols: object) -> str:
    names = ", ".join(cols)
    marks = ", ".join(f"${i + 1}" for i in range(len(cols)))
    return await conn.fetchval(
        f"INSERT INTO booking.{table} ({names}) VALUES ({marks}) RETURNING id::text",  # noqa: S608 — test columns
        *cols.values(),
    )


async def _supplier(conn: asyncpg.Connection, name: str = "Red River DMC") -> str:
    return await _insert(
        conn,
        "suppliers",
        company_name=name,
        categories=["hotel", "activity", "transport"],
        country_code="VN",
        created_by=MAKER,
    )


async def _contract(conn: asyncpg.Connection, supplier_id: str, currency: str = "VND") -> str:
    return await _insert(
        conn,
        "supplier_contracts",
        supplier_id=supplier_id,
        title="2027 rates",
        valid_from=date(2027, 1, 1),
        valid_to=date(2027, 12, 31),
        currency=currency,
        created_by=MAKER,
    )


async def _hotel(conn: asyncpg.Connection, supplier_id: str, name: str = "Hanoi Emerald Hotel") -> str:
    return await _insert(conn, "hotels", supplier_id=supplier_id, name=name, created_by=MAKER)


async def _room(conn: asyncpg.Connection, hotel_id: str, name: str = "Deluxe Double") -> str:
    return await _insert(conn, "hotel_room_types", hotel_id=hotel_id, name=name, max_occupancy=2, created_by=MAKER)


async def _service(conn: asyncpg.Connection, supplier_id: str, title: str = "Old Quarter walk") -> str:
    return await _insert(
        conn, "services", supplier_id=supplier_id, service_type="activity", title=title, created_by=MAKER
    )


async def _rate(conn: asyncpg.Connection, contract_id: str, **cols: object) -> str:
    values: dict[str, object] = {
        "supplier_contract_id": contract_id,
        "valid_from": date(2027, 1, 1),
        "valid_to": date(2027, 4, 30),
        "net_amount": 1_200_000,
        "currency": "VND",
        "created_by": MAKER,
        **cols,
    }
    return await _insert(conn, "supplier_rates", **values)


async def test_registered(app_conn: asyncpg.Connection) -> None:
    assert await app_conn.fetchval("SELECT 1 FROM booking.v_schema_versions WHERE version = 'bk-010'") == 1


async def test_rerun_is_a_no_op(admin: asyncpg.Connection) -> None:
    before = await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint")
    await admin.execute(MIGRATION.read_text(encoding="utf-8"))
    assert await admin.fetchval("SELECT count(*) FROM pg_catalog.pg_constraint") == before


async def test_codes_and_defaults(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    hotel = await app_conn.fetchrow(
        "SELECT code, status, amenities, links::text AS links FROM booking.hotels WHERE id = $1::uuid",
        await _hotel(app_conn, sid),
    )
    assert hotel["code"].startswith("HTL-") and len(hotel["code"]) == 10
    assert (hotel["status"], hotel["amenities"], hotel["links"]) == ("active", [], "[]")
    service = await app_conn.fetchrow(
        "SELECT code, unit, status FROM booking.services WHERE id = $1::uuid", await _service(app_conn, sid)
    )
    assert service["code"].startswith("SVC-") and (service["unit"], service["status"]) == ("person", "active")


async def test_no_customer_facing_columns(app_conn: asyncpg.Connection) -> None:
    """ADR 0004a decision 2 / O7 / Q17: descriptions, highlights and photos of hotels and services are CIS content."""
    rows = await app_conn.fetch(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'booking' AND table_name IN ('hotels', 'hotel_room_types', 'services')"
    )
    assert {r["table_name"] for r in rows} == {"hotels", "hotel_room_types", "services"}
    forbidden = ("description", "highlight", "photo", "image", "media", "cover", "summary", "good_to_know", "seo")
    leaked = [f"{r['table_name']}.{r['column_name']}" for r in rows if any(w in r["column_name"] for w in forbidden)]
    assert leaked == []


@pytest.mark.parametrize(
    ("table", "cols", "constraint"),
    [
        ("hotels", {"star_rating": 6}, "hotels_star_chk"),
        ("hotels", {"latitude": 21.0}, "hotels_geo_chk"),
        ("hotels", {"latitude": 91.0, "longitude": 10.0}, "hotels_geo_chk"),
        ("hotels", {"status": "deleted"}, "hotels_status_chk"),
        ("services", {"service_type": "hotel"}, "services_type_chk"),
        ("services", {"unit": "room_night"}, "services_unit_chk"),
        ("services", {"source_system": "cis"}, "services_source_chk"),
        ("services", {"source_system": "tp", "source_id": "x"}, "services_source_chk"),
        ("services", {"max_group_size": 0}, "services_group_chk"),
    ],
)
async def test_checks(app_conn: asyncpg.Connection, table: str, cols: dict, constraint: str) -> None:
    sid = await _supplier(app_conn)
    base = (
        {"supplier_id": sid, "name": "Check Hotel", "created_by": MAKER}
        if table == "hotels"
        else {"supplier_id": sid, "service_type": "activity", "title": "Check", "created_by": MAKER}
    )
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await _insert(app_conn, table, **{**base, **cols})
    assert exc.value.constraint_name == constraint


async def test_hotel_links_are_a_short_array(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    for links in ("{}", "[" + ",".join(['{"label": "x", "url": "https://x.test"}'] * 11) + "]"):
        with pytest.raises(asyncpg.CheckViolationError) as exc:
            await app_conn.execute(
                "INSERT INTO booking.hotels (supplier_id, name, links) VALUES ($1::uuid, 'Link Hotel', $2::jsonb)",
                sid,
                links,
            )
        assert exc.value.constraint_name == "hotels_links_chk"


async def test_names_unique_per_supplier_and_place(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    hid = await _hotel(app_conn, sid)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _hotel(app_conn, sid, "  hanoi emerald hotel ")
    await _hotel(app_conn, await _supplier(app_conn, "Other DMC"))  # same hotel through another supplier: fine
    await _room(app_conn, hid)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _room(app_conn, hid, "deluxe double")


async def test_app_cannot_delete(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    await _room(app_conn, await _hotel(app_conn, sid))
    await _service(app_conn, sid)
    for table in ("hotel_room_types", "hotels", "services"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(f"DELETE FROM booking.{table}")  # noqa: S608 — fixed names


async def test_rates_follow_the_draft_contract(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    cid = await _contract(app_conn, sid)
    room = await _room(app_conn, await _hotel(app_conn, sid))
    rid = await _rate(app_conn, cid, hotel_room_type_id=room, unit="room_night", meal_plan="BB", season="Low")
    await app_conn.execute("UPDATE booking.supplier_rates SET net_amount = 1300000 WHERE id = $1::uuid", rid)
    spare = await _rate(
        app_conn,
        cid,
        hotel_room_type_id=room,
        unit="room_night",
        valid_from=date(2027, 5, 1),
        valid_to=date(2027, 9, 30),
    )
    await app_conn.execute("DELETE FROM booking.supplier_rates WHERE id = $1::uuid", spare)  # draft: may go

    await app_conn.execute(PUBLISH, cid, CHECKER)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await app_conn.execute("UPDATE booking.supplier_rates SET net_amount = 1 WHERE id = $1::uuid", rid)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await app_conn.execute("DELETE FROM booking.supplier_rates WHERE id = $1::uuid", rid)
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await _rate(app_conn, cid, hotel_room_type_id=room, unit="room_night")


async def test_rate_must_fit_its_contract(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    cid = await _contract(app_conn, sid)
    room = await _room(app_conn, await _hotel(app_conn, sid))
    other_room = await _room(app_conn, await _hotel(app_conn, await _supplier(app_conn, "Other DMC")))
    cases = [
        {"hotel_room_type_id": room, "unit": "room_night", "currency": "USD"},  # not the contract currency
        {"hotel_room_type_id": room, "unit": "room_night", "valid_from": date(2026, 12, 1)},  # starts before
        {"hotel_room_type_id": room, "unit": "room_night", "valid_to": None},  # open-ended in a dated contract
        {"hotel_room_type_id": other_room, "unit": "room_night"},  # another supplier's room
    ]
    for cols in cases:
        with pytest.raises(asyncpg.CheckViolationError):
            await _rate(app_conn, cid, **cols)


@pytest.mark.parametrize(
    ("cols", "constraint"),
    [
        ({"unit": "person"}, "supplier_rates_unit_chk"),  # a room rate is per room night
        ({"unit": "room_night", "meal_plan": "XX"}, "supplier_rates_meal_plan_chk"),
        ({"unit": "room_night", "pax_min": 3, "pax_max": 2}, "supplier_rates_pax_chk"),
        ({"unit": "room_night", "net_amount": -1}, "supplier_rates_amount_chk"),
    ],
)
async def test_rate_checks(app_conn: asyncpg.Connection, cols: dict, constraint: str) -> None:
    sid = await _supplier(app_conn)
    cid = await _contract(app_conn, sid)
    room = await _room(app_conn, await _hotel(app_conn, sid))
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await _rate(app_conn, cid, hotel_room_type_id=room, **cols)
    assert exc.value.constraint_name == constraint


async def test_service_rate_unit_and_single_target(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    cid = await _contract(app_conn, sid)
    svc = await _service(app_conn, sid)
    await _rate(app_conn, cid, service_id=svc, unit="person", pax_min=2, pax_max=4)
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await _rate(app_conn, cid, service_id=svc, unit="room_night")
    assert exc.value.constraint_name == "supplier_rates_unit_chk"
    room = await _room(app_conn, await _hotel(app_conn, sid))
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await _rate(app_conn, cid, service_id=svc, hotel_room_type_id=room, unit="room_night")
    assert exc.value.constraint_name == "supplier_rates_target_chk"


async def test_contract_with_rates_keeps_currency_and_validity(app_conn: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    cid = await _contract(app_conn, sid)
    await _rate(app_conn, cid, service_id=await _service(app_conn, sid), unit="person")
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute("UPDATE booking.supplier_contracts SET currency = 'USD' WHERE id = $1::uuid", cid)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute("UPDATE booking.supplier_contracts SET valid_to = '2027-03-31' WHERE id = $1::uuid", cid)
    # Growing the validity, or editing other terms, is fine.
    await app_conn.execute("UPDATE booking.supplier_contracts SET valid_to = '2028-03-31' WHERE id = $1::uuid", cid)
    await app_conn.execute("UPDATE booking.supplier_contracts SET payment_terms = '30 days' WHERE id = $1::uuid", cid)


async def test_version_item_references_one_supply(app_conn: asyncpg.Connection, admin: asyncpg.Connection) -> None:
    sid = await _supplier(app_conn)
    cid = await _contract(app_conn, sid)
    room = await _room(app_conn, await _hotel(app_conn, sid))
    svc = await _service(app_conn, sid)
    rate = await _rate(app_conn, cid, service_id=svc, unit="person")
    customer_id = await admin.fetchval(
        "INSERT INTO booking.customers (email) VALUES ('it-823-schema@example.test') RETURNING id"
    )
    case_id = await admin.fetchval("INSERT INTO booking.trip_cases (customer_id) VALUES ($1) RETURNING id", customer_id)
    version_id = await admin.fetchval(
        "INSERT INTO booking.trip_case_versions (trip_case_id, version_number) VALUES ($1, 1) RETURNING id", case_id
    )
    insert = (
        "INSERT INTO booking.trip_case_version_itinerary"
        " (trip_case_version_id, position, title, day_from, day_to, hotel_room_type_id, service_id)"
        " VALUES ($1, $2, 'Item', 1, 1, $3::uuid, $4::uuid) RETURNING id"
    )
    try:
        await app_conn.fetchval(insert, version_id, 1, room, None)
        item = await app_conn.fetchval(insert, version_id, 2, None, svc)
        await app_conn.execute(
            "INSERT INTO booking.version_item_costs (version_item_id, net_amount, net_currency, supplier_rate_id)"
            " VALUES ($1, 1200000, 'VND', $2::uuid)",
            item,
            rate,
        )
        with pytest.raises(asyncpg.CheckViolationError) as exc:
            await app_conn.fetchval(insert, version_id, 3, room, svc)
        assert exc.value.constraint_name == "trip_case_version_itinerary_supply_chk"
    finally:
        await purge_supply(admin)
        await admin.execute("DELETE FROM booking.trip_case_versions WHERE trip_case_id = $1", case_id)
        await admin.execute("DELETE FROM booking.trip_cases WHERE id = $1", case_id)
        await admin.execute("DELETE FROM booking.customers WHERE id = $1", customer_id)


async def test_commercial_columns_are_tagged(app_conn: asyncpg.Connection) -> None:
    rows = await app_conn.fetch(
        "SELECT c.relname || '.' || a.attname AS col, col_description(c.oid, a.attnum) AS comment"
        " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'booking' AND c.relname IN ('hotels', 'hotel_room_types', 'services', 'supplier_rates')"
        " AND a.attnum > 0"
    )
    tags = {r["col"]: (r["comment"] or "").split(";")[0] for r in rows}
    for col in (
        "hotels.internal_notes",
        "hotel_room_types.internal_notes",
        "services.internal_notes",
        "supplier_rates.net_amount",
        "supplier_rates.notes",
    ):
        assert tags[col] == "conf:commercial", col
