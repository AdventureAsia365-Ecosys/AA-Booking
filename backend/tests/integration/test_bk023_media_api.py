"""AA-835 S4 over HTTP on a real PostgreSQL (bk-023), as role ``booking``, with a fake bucket:

- photo upload: supply.write only; the row satisfies the bk-023 CHECKs (derived key, bucket name, sha256) and bumps
  the hotel's supply_version (one more supply outbox row); the same bytes twice is 409; a renamed file is 422; a failed
  put leaves no row; usage_rights and removal go through PATCH, removed is final; the list signs active photos only;
  the hotel's recent activity shows the photo rows;
- read-back: without ``catalog.v_supply_content`` the detail carries ``content: null`` (the UI hides the panel); with a
  stand-in of the CIS 278 view (created around the one test that needs it, then dropped: lessons.md, stand-ins only
  around the code that needs them) the detail carries the CIS row and proposal items carry only published text.
"""

from __future__ import annotations

import base64
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.catalog import liveness
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration.supply import purge_supply
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "023_supply_outbox_media.sql"
MAKER, SALES = "it-835-media-maker", "it-835-media-sales"
HANOI = "d0000000-0000-4000-8000-000000000001"
BUCKET = "aa-booking-dev-supply-media-000000000000"
NOTE = "it-835-media"
AUDITED = ["supplier", "hotel", "hotel_room_type", "service", "entity_media"]

# The columns Booking reads from CIS 278 (app.supply.content.CONTENT_VIEW), as a plain table standing in for the view.
STAND_IN = """
CREATE TABLE catalog.v_supply_content (
    id              UUID NOT NULL DEFAULT gen_random_uuid(),
    source_system   TEXT NOT NULL DEFAULT 'booking',
    source_id       TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    content_status  TEXT NOT NULL,
    content_version INTEGER,
    description     TEXT,
    highlights      TEXT[],
    good_to_know    TEXT[],
    photo_urls      TEXT[],
    edit_url        TEXT
);
GRANT SELECT ON catalog.v_supply_content TO booking;
"""
DROP_STAND_IN = "DROP TABLE IF EXISTS catalog.v_supply_content"


def png(width: int = 640, height: int = 480, salt: bytes = b"") -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00" * 20 + salt


def jpeg(width: int = 800, height: int = 600) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + sof + b"\xff\xda" + b"\x00" * 16 + b"\xff\xd9"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class FakeStore:
    bucket = BUCKET

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        if self.fail:
            raise OSError("s3 down")
        self.objects[key] = data

    async def urls(self, keys: list[str]) -> dict[str, str]:
        return {k: f"https://signed.example/{k}" for k in keys}


async def _clean(conn: asyncpg.Connection) -> None:
    await purge_supply(conn)
    await conn.execute(
        "DELETE FROM booking.trip_case_versions WHERE trip_case_id IN"
        " (SELECT id FROM booking.trip_cases WHERE notes = $1)",
        NOTE,
    )
    await conn.execute("DELETE FROM booking.trip_cases WHERE notes = $1", NOTE)
    await conn.execute("DELETE FROM booking.customers WHERE email LIKE 'it-835-media-%'")
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type = ANY($1::text[])", AUDITED)
    await conn.execute(DROP_STAND_IN)
    liveness.reset_cache()


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    # A replay of bk-022 (its tests re-run it) takes back the supply column grants on the outbox: replay bk-023.
    await conn.execute("SET ROLE bk_owner")
    await conn.execute(MIGRATION.read_text(encoding="utf-8"))
    await conn.execute("RESET ROLE")
    await _clean(conn)
    for staff_id, role in ((MAKER, "contracting"), (SALES, "sales")):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, $2, true, $1)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
            staff_id,
            role,
        )
    try:
        yield conn
    finally:
        await _clean(conn)
        await conn.close()


@pytest_asyncio.fixture
async def client(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis, FakeStore]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis, store = Database(settings), LocalCis(), FakeStore()
    app = create_app(settings, db, cis.verifier(), media_store=store)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking/booking") as c:
            yield c, cis, store
    finally:
        await db.close()


def _auth(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


async def _hotel_and_service(c: httpx.AsyncClient, maker: dict[str, str]) -> tuple[dict, dict, dict]:
    sup_body = {"company_name": "Red River DMC", "categories": ["hotel", "activity"], "country_code": "VN"}
    sup = await _ok(await c.post("/suppliers", json=sup_body, headers=maker), 201)
    hotel_body = {"supplier_id": sup["code"], "name": "Emerald", "destination_id": HANOI}
    hotel = await _ok(await c.post("/hotels", json=hotel_body, headers=maker), 201)
    room = await _ok(await c.post(f"/hotels/{hotel['code']}/room-types", json={"name": "Deluxe"}, headers=maker), 201)
    svc_body = {"supplier_id": sup["id"], "service_type": "activity", "title": "Food walk", "destination_id": HANOI}
    svc = await _ok(await c.post("/services", json=svc_body, headers=maker), 201)
    return hotel, room, svc


async def _version(admin: asyncpg.Connection, hotel_id: str) -> int:
    return await admin.fetchval("SELECT supply_version FROM booking.hotels WHERE id = $1::uuid", hotel_id)


async def test_photo_upload_rights_and_removal(
    client: tuple[httpx.AsyncClient, LocalCis, FakeStore], admin: asyncpg.Connection
) -> None:
    c, cis, store = client
    maker, sales = _auth(cis, MAKER), _auth(cis, SALES)
    hotel, _, _ = await _hotel_and_service(c, maker)
    before = await _version(admin, hotel["id"])
    data = png()
    upload = {"content_type": "image/png", "data_base64": b64(data), "filename": "front.png"}

    assert (await c.post(f"/hotels/{hotel['code']}/media", json=upload, headers=sales)).status_code == 403
    photo = await _ok(await c.post(f"/hotels/{hotel['code']}/media", json=upload, headers=maker), 201)
    assert (
        photo["usage_rights"] is False
        and photo["status"] == "active"
        and (photo["width"], photo["height"])
        == (
            640,
            480,
        )
    )
    key = f"supply-media/hotel/{hotel['id']}/{photo['id']}.png"
    assert store.objects == {key: data} and photo["url"] == f"https://signed.example/{key}"
    row = await admin.fetchrow("SELECT s3_bucket, s3_key, bytes FROM booking.entity_media WHERE id = $1", photo["id"])
    assert dict(row) == {"s3_bucket": BUCKET, "s3_key": key, "bytes": len(data)}
    assert await _version(admin, hotel["id"]) == before + 1

    dup = await c.post(f"/hotels/{hotel['id']}/media", json=upload, headers=maker)
    assert dup.status_code == 409 and dup.json()["detail"] == "media_exists"
    spoof = await c.post(
        f"/hotels/{hotel['id']}/media", json={"content_type": "image/png", "data_base64": b64(jpeg())}, headers=maker
    )
    assert spoof.status_code == 422 and spoof.json()["detail"] == "content_type_mismatch"
    store.fail = True
    failed = await c.post(
        f"/hotels/{hotel['id']}/media", json={**upload, "data_base64": b64(png(salt=b"x"))}, headers=maker
    )
    store.fail = False
    assert failed.status_code == 503 and failed.json()["detail"] == "media_storage_unavailable"
    assert await admin.fetchval("SELECT count(*) FROM booking.entity_media WHERE entity_id = $1", hotel["id"]) == 1
    assert await _version(admin, hotel["id"]) == before + 1  # the failed upload rolled its bump back too

    listed = await _ok(await c.get(f"/hotels/{hotel['code']}/media", headers=sales))
    assert [p["id"] for p in listed["items"]] == [photo["id"]] and listed["items"][0]["url"]

    assert (await c.patch(f"/media/{photo['id']}", json={"usage_rights": True}, headers=sales)).status_code == 403
    cleared = await _ok(await c.patch(f"/media/{photo['id']}", json={"usage_rights": True}, headers=maker))
    assert cleared["usage_rights"] is True and cleared["changed"] == ["usage_rights"]
    removed = await _ok(await c.patch(f"/media/{photo['id']}", json={"status": "removed"}, headers=maker))
    assert removed["status"] == "removed" and removed["url"] is None
    again = await c.patch(f"/media/{photo['id']}", json={"usage_rights": False}, headers=maker)
    assert again.status_code == 409 and again.json()["detail"] == "media_removed"
    assert await _version(admin, hotel["id"]) == before + 3

    assert (await _ok(await c.get(f"/hotels/{hotel['id']}/media", headers=sales)))["items"] == []
    with_removed = await _ok(
        await c.get(f"/hotels/{hotel['id']}/media", params={"include_removed": "true"}, headers=sales)
    )
    assert [(p["status"], p["url"]) for p in with_removed["items"]] == [("removed", None)]
    assert key in store.objects  # removal never deletes the object

    page = await _ok(await c.get(f"/hotels/{hotel['code']}", headers=sales))
    photo_actions = sorted(a["action"] for a in page["recent_activity"] if a["entity_type"] == "entity_media")
    assert photo_actions == ["entity_media.removed", "entity_media.updated", "entity_media.uploaded"]
    assert page["content"] is None  # no catalog.v_supply_content: read-back off

    events = await admin.fetch(
        "SELECT event_type, supply_version FROM booking.outbox WHERE stream = 'supply' AND entity_id = $1::uuid"
        " ORDER BY id",
        hotel["id"],
    )
    assert [e["supply_version"] for e in events] == list(range(1, before + 4))


async def test_read_back_and_published_text_on_proposal_items(
    client: tuple[httpx.AsyncClient, LocalCis, FakeStore], admin: asyncpg.Connection
) -> None:
    c, cis, _ = client
    maker, sales = _auth(cis, MAKER), _auth(cis, SALES)
    hotel, room, svc = await _hotel_and_service(c, maker)
    customer_id = await admin.fetchval(
        "INSERT INTO booking.customers (email) VALUES ('it-835-media-1@example.test') RETURNING id"
    )
    case = await admin.fetchrow(
        "INSERT INTO booking.trip_cases (customer_id, start_date, pax, notes) VALUES ($1, '2027-03-10', 2, $2)"
        " RETURNING id, code",
        customer_id,
        NOTE,
    )
    await admin.execute(
        "INSERT INTO booking.trip_case_versions (trip_case_id, version_number) VALUES ($1, 1)", case["id"]
    )
    base = f"/trip-cases/{case['code']}/versions/1"
    items = [
        {"title": "Emerald, 2 nights", "day_from": 1, "day_to": 3, "hotel_room_type_id": room["id"]},
        {"title": "Food walk", "day_from": 2, "day_to": 2, "service_id": svc["id"]},
    ]
    saved = await _ok(await c.put(f"{base}/items", json={"items": items}, headers=sales))
    # Without CIS 278 the items still load; the supply carries no text.
    assert [i["supply"]["description"] for i in saved["items"]] == [None, None]
    assert saved["items"][0]["supply"]["entity_id"] == hotel["id"]

    await admin.execute(STAND_IN)
    liveness.reset_cache()
    await admin.execute(
        "INSERT INTO catalog.v_supply_content (source_id, entity_type, content_status, content_version, description,"
        " highlights, photo_urls, edit_url) VALUES"
        " ($1, 'hotel', 'published', 2, 'A quiet garden hotel.', ARRAY['Garden'], ARRAY['https://img.example/a.webp'],"
        "  '/admin/supply-content/11111111-1111-4111-8111-111111111111'),"
        " ($2, 'service', 'draft', 1, NULL, NULL, NULL, '/admin/supply-content/22222222-2222-4222-8222-222222222222')",
        hotel["id"],
        svc["id"],
    )
    hotel_page = await _ok(await c.get(f"/hotels/{hotel['code']}", headers=sales))
    assert hotel_page["content"] == {
        "content_status": "published",
        "content_version": 2,
        "description": "A quiet garden hotel.",
        "highlights": ["Garden"],
        "photo_urls": ["https://img.example/a.webp"],
        "edit_url": "/admin/supply-content/11111111-1111-4111-8111-111111111111",
    }
    svc_page = await _ok(await c.get(f"/services/{svc['code']}", headers=sales))
    assert svc_page["content"]["content_status"] == "draft" and svc_page["content"]["description"] is None

    got = await _ok(await c.get(f"{base}/items", headers=sales))
    room_supply, svc_supply = (i["supply"] for i in got["items"])
    assert room_supply["description"] == "A quiet garden hotel." and room_supply["highlights"] == ["Garden"]
    assert svc_supply["description"] is None and svc_supply["highlights"] == []

    await admin.execute(
        "UPDATE catalog.v_supply_content SET content_status = 'stale' WHERE source_id = $1", hotel["id"]
    )
    got = await _ok(await c.get(f"{base}/items", headers=sales))
    assert got["items"][0]["supply"]["description"] is None  # stale: no text on a proposal

    await admin.execute("REVOKE SELECT ON catalog.v_supply_content FROM booking")
    liveness.reset_cache()
    assert (await _ok(await c.get(f"/hotels/{hotel['code']}", headers=sales)))["content"] is None

    # CIS declares source_id as uuid: feature detection checks column names, not types, so Booking compares the keys
    # as text and both the read-back and a proposal write keep working (review round, AA-835 S4).
    await admin.execute("GRANT SELECT ON catalog.v_supply_content TO booking")
    await admin.execute("ALTER TABLE catalog.v_supply_content ALTER COLUMN source_id TYPE uuid USING source_id::uuid")
    await admin.execute(
        "UPDATE catalog.v_supply_content SET content_status = 'published' WHERE source_id = $1::uuid", hotel["id"]
    )
    liveness.reset_cache()
    assert (await _ok(await c.get(f"/hotels/{hotel['code']}", headers=sales)))["content"]["content_status"] == (
        "published"
    )
    saved = await _ok(await c.put(f"{base}/items", json={"items": items}, headers=sales))
    assert saved["items"][0]["supply"]["description"] == "A quiet garden hotel."


async def test_hotel_list_photo_summary(
    client: tuple[httpx.AsyncClient, LocalCis, FakeStore], admin: asyncpg.Connection
) -> None:
    """The staff hotel list (photo grid, S254 Figma audit #19): per row the number of active photos and the first one
    (upload order), presigned; a hotel without photos has 0 / null; a removed photo no longer counts."""
    c, cis, _ = client
    maker, sales = _auth(cis, MAKER), _auth(cis, SALES)
    hotel, _, _ = await _hotel_and_service(c, maker)
    bare = await _ok(
        await c.post(
            "/hotels",
            json={"supplier_id": hotel["supplier_id"], "name": "Bare", "destination_id": HANOI},
            headers=maker,
        ),
        201,
    )
    first = await _ok(
        await c.post(
            f"/hotels/{hotel['code']}/media",
            json={"content_type": "image/png", "data_base64": b64(png(salt=b"a")), "filename": "a.png"},
            headers=maker,
        ),
        201,
    )
    second = await _ok(
        await c.post(
            f"/hotels/{hotel['code']}/media",
            json={"content_type": "image/png", "data_base64": b64(png(salt=b"b")), "filename": "b.png"},
            headers=maker,
        ),
        201,
    )
    first_url = f"https://signed.example/supply-media/hotel/{hotel['id']}/{first['id']}.png"

    async def summary() -> dict[str, tuple[int, str | None]]:
        rows = (await _ok(await c.get("/hotels", params={"limit": 100}, headers=sales)))["items"]
        return {r["code"]: (r["photo_count"], r["cover_photo_url"]) for r in rows}

    assert (await summary()) == {hotel["code"]: (2, first_url), bare["code"]: (0, None)}
    await _ok(await c.patch(f"/media/{first['id']}", json={"status": "removed"}, headers=maker))
    second_url = f"https://signed.example/supply-media/hotel/{hotel['id']}/{second['id']}.png"
    assert (await summary()) == {hotel["code"]: (1, second_url), bare["code"]: (0, None)}
