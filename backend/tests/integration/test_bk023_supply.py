"""AA-835 (bk-023) behaviour on a real PostgreSQL, written as role ``booking`` (the app) and read as ``cis_app`` (the
CIS runtime), the way Dev runs it:

- a hotel / service insert writes one supply outbox row (supply.upsert, version 1); a material change bumps
  supply_version by exactly 1 and writes exactly one row; a non-material change (internal notes, links, the audit
  columns) writes none; any write of supply_version itself is one bump, never another value;
- a room type insert or material change bumps its hotel; a room type note does not;
- deactivate writes supply.deactivate, reactivate supply.upsert;
- a photo insert, a usage_rights flip and a removal bump the entity; a removed photo stays removed; a photo of a
  missing entity is refused; role booking can neither delete a photo nor move its key;
- the views: facts with room types and active photos only; v_supply_outbox shows only stamped supply rows;
- cis_app reads the four views and gets 42501 on the tables behind them;
- the b2b feed query (app/b2b/events.py) never returns a supply row.

Each test cleans its own rows (purge_supply), and outbox assertions filter on the test's own entity ids, so rows other
modules leave in the shared outbox do not matter."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app import outbox
from app.b2b import events
from tests.integration.supply import purge_supply

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "023_supply_outbox_media.sql"
STAFF = "it-835-contracting"
BUCKET = "aa-booking-dev-supply-media-000000000000"

EVENTS_SQL = (
    "SELECT event_type, supply_version FROM booking.outbox"
    " WHERE stream = 'supply' AND entity_type = $1 AND entity_id = $2::uuid ORDER BY id"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    # A replay of bk-022 (its tests re-run it) takes back the supply column grants on the outbox: replay bk-023.
    await conn.execute("SET ROLE bk_owner")
    await conn.execute(MIGRATION.read_text(encoding="utf-8"))
    await conn.execute("RESET ROLE")
    await conn.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ($1, 'contracting', true)"
        " ON CONFLICT (cis_user_id) DO NOTHING",
        STAFF,
    )
    await purge_supply(conn)
    try:
        yield conn
    finally:
        await purge_supply(conn)
        await conn.execute("DELETE FROM booking.suppliers WHERE created_by = $1", STAFF)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _supplier(conn: asyncpg.Connection) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, created_by)"
        " VALUES ($1, ARRAY['hotel', 'activity'], 'VN', $2) RETURNING id::text",
        f"Supply DMC {uuid.uuid4().hex[:8]}",
        STAFF,
    )


async def _hotel(conn: asyncpg.Connection, supplier: str, **cols: Any) -> str:
    values = {"supplier_id": supplier, "name": "Riverside Boutique", "created_by": STAFF, **cols}
    names = ", ".join(values)
    marks = ", ".join(f"${i + 1}" for i in range(len(values)))
    return await conn.fetchval(
        f"INSERT INTO booking.hotels ({names}) VALUES ({marks}) RETURNING id::text",  # noqa: S608 — test columns
        *values.values(),
    )


async def _service(conn: asyncpg.Connection, supplier: str) -> str:
    return await conn.fetchval(
        "INSERT INTO booking.services (supplier_id, service_type, title, created_by)"
        " VALUES ($1::uuid, 'activity', 'Lantern walk', $2) RETURNING id::text",
        supplier,
        STAFF,
    )


async def _media(conn: asyncpg.Connection, entity_type: str, entity_id: str, sha: str = "b" * 64) -> str:
    media_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO booking.entity_media (id, entity_type, entity_id, s3_bucket, s3_key, content_type, bytes, width,"
        " height, sha256, uploaded_by) VALUES ($1::uuid, $2, $3::uuid, $4, $5, 'image/webp', 2048, 1600, 1200, $6, $7)",
        media_id,
        entity_type,
        entity_id,
        BUCKET,
        f"supply-media/{entity_type}/{entity_id}/{media_id}.webp",
        sha,
        STAFF,
    )
    return media_id


async def _events(conn: asyncpg.Connection, entity_type: str, entity_id: str) -> list[tuple[str, int]]:
    return [(r["event_type"], r["supply_version"]) for r in await conn.fetch(EVENTS_SQL, entity_type, entity_id)]


async def _version(conn: asyncpg.Connection, table: str, entity_id: str) -> int:
    return await conn.fetchval(
        f"SELECT supply_version FROM booking.{table} WHERE id = $1::uuid",  # noqa: S608 — fixed table names
        entity_id,
    )


async def test_hotel_versions_and_events(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    hotel = await _hotel(app_conn, await _supplier(app_conn), supply_version=42)
    assert await _version(app_conn, "hotels", hotel) == 1  # an insert is version 1, whatever was sent
    assert await _events(app_conn, "hotel", hotel) == [("supply.upsert", 1)]

    # Material change: exactly one bump, one row.
    await app_conn.execute("UPDATE booking.hotels SET name = 'Riverside Boutique Hoi An' WHERE id = $1::uuid", hotel)
    assert await _version(app_conn, "hotels", hotel) == 2
    # Not material: notes, links, the audit column; a same-value write of a fact is no change either.
    await app_conn.execute(
        'UPDATE booking.hotels SET internal_notes = \'call the GM first\', links = \'[{"label": "site",'
        ' "url": "https://example.test"}]\'::jsonb, updated_by = $2, name = name WHERE id = $1::uuid',
        hotel,
        STAFF,
    )
    assert await _version(app_conn, "hotels", hotel) == 2
    # A direct write of supply_version is one bump, never the value sent (no jump, no going back).
    await app_conn.execute("UPDATE booking.hotels SET supply_version = 1 WHERE id = $1::uuid", hotel)
    assert await _version(app_conn, "hotels", hotel) == 3
    # Amenities and the geo point are facts.
    await app_conn.execute(
        "UPDATE booking.hotels SET amenities = ARRAY['pool'], latitude = 15.88, longitude = 108.33 WHERE id = $1::uuid",
        hotel,
    )
    # Deactivate, reactivate.
    await app_conn.execute("UPDATE booking.hotels SET status = 'inactive' WHERE id = $1::uuid", hotel)
    await app_conn.execute("UPDATE booking.hotels SET status = 'active' WHERE id = $1::uuid", hotel)
    assert await _events(app_conn, "hotel", hotel) == [
        ("supply.upsert", 1),
        ("supply.upsert", 2),
        ("supply.upsert", 3),
        ("supply.upsert", 4),
        ("supply.deactivate", 5),
        ("supply.upsert", 6),
    ]


async def test_room_types_bump_their_hotel(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    supplier = await _supplier(app_conn)
    hotel, other = await _hotel(app_conn, supplier), await _hotel(app_conn, supplier, name="Second hotel")
    room = await app_conn.fetchval(
        "INSERT INTO booking.hotel_room_types (hotel_id, name, max_occupancy, created_by)"
        " VALUES ($1::uuid, 'Deluxe River View', 2, $2) RETURNING id::text",
        hotel,
        STAFF,
    )
    assert await _version(app_conn, "hotels", hotel) == 2
    await app_conn.execute(
        "UPDATE booking.hotel_room_types SET internal_notes = 'no extra bed', updated_by = $2 WHERE id = $1::uuid",
        room,
        STAFF,
    )
    assert await _version(app_conn, "hotels", hotel) == 2
    await app_conn.execute(
        "UPDATE booking.hotel_room_types SET bed_types = ARRAY['twin'], status = 'inactive' WHERE id = $1::uuid", room
    )
    assert await _version(app_conn, "hotels", hotel) == 3
    # Moving a room type to another hotel changes both.
    await app_conn.execute("UPDATE booking.hotel_room_types SET hotel_id = $2::uuid WHERE id = $1::uuid", room, other)
    assert (await _version(app_conn, "hotels", hotel), await _version(app_conn, "hotels", other)) == (4, 2)
    assert [v for _, v in await _events(app_conn, "hotel", hotel)] == [1, 2, 3, 4]


async def test_service_versions_and_events(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    service = await _service(app_conn, await _supplier(app_conn))
    await app_conn.execute(
        "UPDATE booking.services SET internal_notes = 'cash only', source_system = 'cis', source_id = 'cmp-1'"
        " WHERE id = $1::uuid",
        service,
    )
    assert await _version(app_conn, "services", service) == 1
    await app_conn.execute(
        "UPDATE booking.services SET title = 'Lantern walk by night', duration_minutes = 90 WHERE id = $1::uuid",
        service,
    )
    await app_conn.execute("UPDATE booking.services SET status = 'inactive' WHERE id = $1::uuid", service)
    assert await _events(app_conn, "service", service) == [
        ("supply.upsert", 1),
        ("supply.upsert", 2),
        ("supply.deactivate", 3),
    ]


async def test_photos_bump_and_stay_append_only(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    supplier = await _supplier(app_conn)
    hotel, service = await _hotel(app_conn, supplier), await _service(app_conn, supplier)
    photo = await _media(app_conn, "hotel", hotel)
    assert await _version(app_conn, "hotels", hotel) == 2
    await app_conn.execute(
        "UPDATE booking.entity_media SET usage_rights = true, updated_by = $2 WHERE id = $1::uuid", photo, STAFF
    )
    assert await _version(app_conn, "hotels", hotel) == 3
    await app_conn.execute("UPDATE booking.entity_media SET updated_by = $2 WHERE id = $1::uuid", photo, STAFF)
    assert await _version(app_conn, "hotels", hotel) == 3  # an audit-only write is no change
    await app_conn.execute("UPDATE booking.entity_media SET status = 'removed' WHERE id = $1::uuid", photo)
    assert await _version(app_conn, "hotels", hotel) == 4
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await app_conn.execute("UPDATE booking.entity_media SET status = 'active' WHERE id = $1::uuid", photo)
    # The same bytes may be uploaded again once the old copy is removed, not twice while active.
    again = await _media(app_conn, "hotel", hotel)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _media(app_conn, "hotel", hotel)
    # Never deleted, key never moved.
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM booking.entity_media WHERE id = $1::uuid", again)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE booking.entity_media SET s3_key = s3_key WHERE id = $1::uuid", again)
    # A photo of an entity that does not exist (or of the wrong kind) is refused.
    for entity_type, entity_id in (("hotel", str(uuid.uuid4())), ("hotel", service)):
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await _media(app_conn, entity_type, entity_id, sha="c" * 64)
    await _media(app_conn, "service", service, sha="c" * 64)
    assert await _version(app_conn, "services", service) == 2


async def test_views_show_facts_room_types_and_active_photos(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    supplier = await _supplier(app_conn)
    hotel = await _hotel(app_conn, supplier, internal_notes="secret net deal", country_code="VN", star_rating=4)
    await app_conn.execute(
        "INSERT INTO booking.hotel_room_types (hotel_id, name, max_occupancy, bed_types, status, internal_notes)"
        " VALUES ($1::uuid, 'Deluxe', 2, ARRAY['double'], 'active', 'n1'), ($1::uuid, 'Suite', NULL, '{}', 'inactive',"
        " 'n2')",
        hotel,
    )
    kept = await _media(app_conn, "hotel", hotel)
    gone = await _media(app_conn, "hotel", hotel, sha="d" * 64)
    await app_conn.execute("UPDATE booking.entity_media SET status = 'removed' WHERE id = $1::uuid", gone)

    row = await app_conn.fetchrow("SELECT * FROM booking.v_supply_hotels WHERE id = $1::uuid", hotel)
    assert row["supply_version"] == await _version(app_conn, "hotels", hotel)
    assert (row["name"], row["country_code"], row["star_rating"], row["status"]) == (
        "Riverside Boutique",
        "VN",
        4,
        "active",
    )
    rooms = json.loads(row["room_types"])
    # Both rows came from one statement (same created_at): compare without the order.
    assert sorted((r["name"], r["status"], r["bed_types"]) for r in rooms) == [
        ("Deluxe", "active", ["double"]),
        ("Suite", "inactive", []),
    ]
    assert all(set(r) == {"id", "name", "max_occupancy", "bed_types", "status"} for r in rooms)
    media = json.loads(row["media"])
    assert [m["media_id"] for m in media] == [kept]
    assert set(media[0]) == {"media_id", "usage_rights", "content_type", "width", "height", "sha256"}
    assert media[0]["usage_rights"] is False
    listed = await app_conn.fetch(
        "SELECT media_id::text, s3_key FROM booking.v_supply_media WHERE entity_id = $1::uuid", hotel
    )
    assert [(r["media_id"], r["s3_key"]) for r in listed] == [(kept, f"supply-media/hotel/{hotel}/{kept}.webp")]

    service = await _service(app_conn, supplier)
    srow = await app_conn.fetchrow("SELECT * FROM booking.v_supply_services WHERE id = $1::uuid", service)
    assert (srow["working_title"], srow["unit"], json.loads(srow["media"])) == ("Lantern walk", "person", [])


async def test_supply_outbox_view_shows_stamped_supply_rows_only(
    admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    partner = str(uuid.uuid4())
    hotel = await _hotel(app_conn, await _supplier(app_conn))
    await app_conn.execute(
        "INSERT INTO booking.outbox (stream, event_type, partner_id, departure_id)"
        " VALUES ('b2b', 'departure.guaranteed', $1::uuid, gen_random_uuid())",
        partner,
    )
    view = "SELECT event_type, supply_version, feed_seq FROM booking.v_supply_outbox WHERE entity_id = $1::uuid"
    assert await app_conn.fetch(view, hotel) == []  # not stamped yet: invisible to readers
    assert await outbox.sequence_pending(app_conn) is not None
    rows = await app_conn.fetch(view, hotel)
    assert [(r["event_type"], r["supply_version"]) for r in rows] == [("supply.upsert", 1)]
    assert rows[0]["feed_seq"] is not None
    assert (
        await app_conn.fetchval("SELECT count(*) FROM booking.v_supply_outbox WHERE event_type LIKE 'departure.%'") == 0
    )
    # The b2b feed of that agency carries its b2b event and never a supply row.
    feed = await app_conn.fetch(events.FEED_SQL, partner, 0, 100)
    assert [r["event_type"] for r in feed] == ["departure.guaranteed"]
    supply_seq = rows[0]["feed_seq"]
    assert all(r["feed_seq"] != supply_seq for r in feed)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.outbox WHERE partner_id = $1::uuid", partner)


async def test_cis_app_reads_the_views_not_the_tables(admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    hotel = await _hotel(app_conn, await _supplier(app_conn))
    await outbox.sequence_pending(app_conn)
    tx = admin.transaction()
    await tx.start()
    try:
        await admin.execute("SET LOCAL ROLE cis_app")
        assert await admin.fetchval("SELECT name FROM booking.v_supply_hotels WHERE id = $1::uuid", hotel)
        assert (
            await admin.fetchval("SELECT count(*) FROM booking.v_supply_outbox WHERE entity_id = $1::uuid", hotel) == 1
        )
        await admin.fetch("SELECT * FROM booking.v_supply_services LIMIT 1")
        await admin.fetch("SELECT * FROM booking.v_supply_media LIMIT 1")
        for table in (
            "booking.hotels",
            "booking.services",
            "booking.hotel_room_types",
            "booking.entity_media",
            "booking.outbox",
            "booking.supplier_rates",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with admin.transaction():
                    await admin.fetch(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608 — fixed names
    finally:
        await tx.rollback()
