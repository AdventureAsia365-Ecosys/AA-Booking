"""AA-996 Catalog → Countries / Destinations over HTTP on a real PostgreSQL (bk-029 + the CI stub of
``catalog.v_destination`` / ``v_tour``, role ``booking``): the destination list (filters, summary over the whole
catalog), detail (Booking supply at the place; photos fail soft without ``v_destination_photo``), deactivate /
activate (soft, audited, admin only), and the country list / detail / update derived from CIS places and tours: grouped
by CIS's ISO code (``v_country`` / ``v_destination.country_iso2``), including Pakistan (in ``booking.countries`` since
bk-031) and a country still outside it (read-only there), with images = distinct CIS photo paths of its places and its
place-less tour photos."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

HANOI = "d0000000-0000-4000-8000-000000000001"
LUANG_PRABANG = "d0000000-0000-4000-8000-000000000002"
HOI_AN = "d0000000-0000-4000-8000-000000000003"
LAHORE = "d0000000-0000-4000-8000-000000000005"  # stub_catalog.sql: a Pakistani place (PK: bk-031)
UNKNOWN = "d0000000-0000-4000-8000-0000000000ff"
TOUR_VN = "aa996000-0000-4000-8000-00000000000a"
TOUR_LA = "aa996000-0000-4000-8000-00000000000b"
ADMIN, SALES, CONTENT = "it-996-admin", "it-996-sales", "it-996-content"


async def _purge(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.execute("DELETE FROM booking.country_profiles")
        await conn.execute("DELETE FROM booking.destination_profiles")
        await conn.execute("DELETE FROM booking.audit_logs WHERE actor_ref LIKE 'it-996-%'")
    finally:
        await conn.execute("RESET ROLE")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await conn.execute("TRUNCATE catalog.v_tour, catalog.v_tour_photo, catalog.v_destination_photo")
    for staff_id, role in ((ADMIN, "admin"), (SALES, "sales"), (CONTENT, "content_creator")):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, name, role, active) VALUES ($1, $2, $3, true)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
            staff_id,
            staff_id.replace("it-996-", "").title(),
            role,
        )
    await conn.executemany(
        "INSERT INTO catalog.v_tour (source_id, sku, title, country, duration_days) VALUES ($1, $2, $3, $4, $5)",
        [(TOUR_VN, "VN-7D", "Northern Vietnam", "Vietnam", 7), (TOUR_LA, "LA-5D", "Mekong Slow Boat", "Laos", 5)],
    )
    # Photos (CIS 290: copies of one file share one path). VN: a and b at its places, c and a again on its tour = 3.
    # LA: d on its tour; b is tied to Hanoi, so it counts for Vietnam, not for the Lao tour that carries it.
    await conn.executemany(
        "INSERT INTO catalog.v_destination_photo (destination_id, path, position) VALUES ($1::uuid, $2, $3)",
        [(HANOI, "/content/photos/a", 1), (HOI_AN, "/content/photos/a", 1), (HANOI, "/content/photos/b", 2)],
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour_photo (source_id, path, destination_id, position) VALUES ($1, $2, $3::uuid, $4)",
        [
            (TOUR_VN, "/content/photos/c", None, 1),
            (TOUR_VN, "/content/photos/a", None, 2),
            (TOUR_LA, "/content/photos/b", HANOI, 1),
            (TOUR_LA, "/content/photos/d", None, 2),
        ],
    )
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.execute("TRUNCATE catalog.v_tour, catalog.v_tour_photo, catalog.v_destination_photo")
        await conn.close()


@pytest_asyncio.fixture
async def client(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


def _auth(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


async def test_destinations_list_detail_and_soft_deactivate(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    content, sales, boss = _auth(cis, CONTENT), _auth(cis, SALES), _auth(cis, ADMIN)

    listing = (await c.get("/catalog/geo/destinations", headers=content)).json()
    assert listing["total"] == 5 and listing["summary"]["total"] == 5 and listing["summary"]["active"] == 5
    assert listing["summary"]["countries"] == 3  # VN, LA, PK: CIS's ISO codes ("Nowhere" has none)
    assert [i["name"] for i in listing["items"]] == ["Atlantis", "Hanoi", "Hoi An", "Lahore", "Luang Prabang"]
    assert {i["country_code"] for i in listing["items"]} == {"VN", "LA", "PK", None}  # "Nowhere" has no ISO code
    vn = (await c.get("/catalog/geo/destinations", params={"country": "vn"}, headers=content)).json()
    assert [i["name"] for i in vn["items"]] == ["Hanoi", "Hoi An"]

    detail = await c.get(f"/catalog/geo/destinations/{HANOI}", headers=content)
    assert detail.status_code == 200, detail.text
    # Photos come from the v_destination_photo stub, which other tests in this module may fill (order-independent).
    photos = detail.json()["photos"]
    assert isinstance(photos, list) and all(p.get("photo_id") for p in photos)
    assert detail.json()["active_suppliers"] == 0
    assert (await c.get(f"/catalog/geo/destinations/{UNKNOWN}", headers=content)).status_code == 404

    body = {"status": "inactive", "status_reason": "Closed for the monsoon"}
    assert (await c.patch(f"/catalog/geo/destinations/{HANOI}", json=body, headers=sales)).status_code == 403
    r = await c.patch(f"/catalog/geo/destinations/{HANOI}", json=body, headers=boss)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "inactive" and r.json()["changed"] == ["status", "status_reason"]
    again = await c.patch(f"/catalog/geo/destinations/{HANOI}", json=body, headers=boss)
    assert again.json()["changed"] == []
    assert (await c.patch(f"/catalog/geo/destinations/{UNKNOWN}", json=body, headers=boss)).status_code == 404

    inactive = (await c.get("/catalog/geo/destinations", params={"status": "inactive"}, headers=content)).json()
    assert [i["name"] for i in inactive["items"]] == ["Hanoi"] and inactive["summary"]["inactive"] == 1

    back = await c.patch(f"/catalog/geo/destinations/{HANOI}", json={"status": "active"}, headers=boss)
    assert back.json()["status"] == "active" and back.json()["status_reason"] is None
    actions = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_type = 'destination_profile' ORDER BY created_at"
    )
    assert [a["action"] for a in actions] == ["destination.deactivated", "destination.activated"]
    # Soft state only: the profile row stays.
    assert await admin.fetchval("SELECT count(*) FROM booking.destination_profiles") == 1


async def test_countries_list_detail_and_update(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    content, boss = _auth(cis, CONTENT), _auth(cis, ADMIN)

    listing = (await c.get("/catalog/geo/countries", headers=content)).json()
    by_code = {i["country_code"]: i for i in listing["items"]}
    assert set(by_code) == {"LA", "PK", "VN"}
    assert by_code["VN"]["destinations"] == 2 and by_code["VN"]["tours"] == 1 and by_code["VN"]["status"] == "active"
    assert listing["unmapped"] == [{"name": "Nowhere", "destinations": 1}]
    # Images: distinct CIS photo paths of the country's places + its tours' photos not tied to a place.
    assert by_code["VN"]["photos"] == 3 and by_code["LA"]["photos"] == 1 and by_code["PK"]["photos"] == 0
    # Pakistan was outside Booking's reference list until bk-031 (read-only on Dev); it now takes Booking state.
    pk = by_code["PK"]
    assert pk["name"] == "Pakistan" and pk["destinations"] == 1 and pk["booking_reference"] is True
    assert by_code["VN"]["booking_reference"] is True
    pk_detail = (await c.get("/catalog/geo/countries/pk", headers=content)).json()
    assert [d["destination_id"] for d in pk_detail["destination_items"]] == [LAHORE]
    # A CIS country still outside the list (stub: Kazakhstan) is readable, but Booking cannot keep state for it.
    kz_detail = await c.get("/catalog/geo/countries/KZ", headers=content)
    assert kz_detail.status_code == 200 and kz_detail.json()["booking_reference"] is False
    refused = await c.patch("/catalog/geo/countries/KZ", json={"status": "inactive"}, headers=boss)
    assert refused.status_code == 409 and refused.json()["detail"] == "country_not_in_booking"
    assert await admin.fetchval("SELECT count(*) FROM booking.country_profiles") == 0

    vn = (await c.get("/catalog/geo/countries/vn", headers=content)).json()
    assert [d["name"] for d in vn["destination_items"]] == ["Hanoi", "Hoi An"] and vn["photos"] == 3
    assert [t["source_id"] for t in vn["tour_items"]] == [TOUR_VN]
    assert (await c.get("/catalog/geo/countries/ZZ", headers=content)).status_code == 404

    r = await c.patch(
        "/catalog/geo/countries/LA",
        json={"status": "inactive", "area": "Indochina", "status_reason": "No guides"},
        headers=boss,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "inactive" and r.json()["area"] == "Indochina"
    assert (await c.patch("/catalog/geo/countries/LA", json={"area": "x"}, headers=content)).status_code == 403

    inactive = (await c.get("/catalog/geo/countries", params={"status": "inactive"}, headers=content)).json()
    assert [i["country_code"] for i in inactive["items"]] == ["LA"] and inactive["areas"] == ["Indochina"]
    # A country with a profile but no CIS place or tour stays listed (Booking state is kept).
    await c.patch("/catalog/geo/countries/JP", json={"area": "North Asia"}, headers=boss)
    codes = [i["country_code"] for i in (await c.get("/catalog/geo/countries", headers=content)).json()["items"]]
    assert "JP" in codes
    # "Add country" with the drawer defaults (nothing to change) still lists the country.
    added = await c.patch("/catalog/geo/countries/TH", json={"status": "active"}, headers=boss)
    assert added.status_code == 200, added.text
    assert added.json()["changed"] == [] and added.json()["status"] == "active"
    codes = [i["country_code"] for i in (await c.get("/catalog/geo/countries", headers=content)).json()["items"]]
    assert "TH" in codes
    assert (
        await admin.fetchval(
            "SELECT action FROM booking.audit_logs WHERE entity_type = 'country_profile' AND entity_label LIKE '%(TH)'"
        )
        == "country.added"
    )
    again = await c.patch("/catalog/geo/countries/TH", json={}, headers=boss)
    assert again.json()["changed"] == []
    assert await admin.fetchval("SELECT count(*) FROM booking.audit_logs WHERE entity_label LIKE '%(TH)'") == 1
    # The ISO code matches only a two-letter query: "Laos" finds Laos, not a country whose code starts "LA".
    found = (await c.get("/catalog/geo/countries", params={"q": "la"}, headers=content)).json()["items"]
    assert "LA" in [i["country_code"] for i in found]
    row = await admin.fetchrow(
        "SELECT action, entity_label FROM booking.audit_logs WHERE entity_type = 'country_profile'"
        " ORDER BY created_at LIMIT 1"
    )
    assert row["action"] == "country.deactivated" and row["entity_label"] == "Laos (LA)"
    # bk-031: Pakistan takes Booking state now (it answered 409 country_not_in_booking before).
    pk_edit = await c.patch("/catalog/geo/countries/PK", json={"area": "South Asia"}, headers=boss)
    assert pk_edit.status_code == 200, pk_edit.text
    assert pk_edit.json()["area"] == "South Asia" and pk_edit.json()["booking_reference"] is True
