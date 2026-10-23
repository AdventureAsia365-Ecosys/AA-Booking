"""AA-823 hotels, room types, services and supplier rates over HTTP on a real PostgreSQL (bk-010), as role ``booking``.

RBAC (contracting/admin write, sales read), facts only (no description/photo field), the CIS place check, supplier
category, room types, rates under a draft contract (maker-checker through the contract), and the Linear done-when:
a hotel with two room types and a service used as items of a version, the net taken from the rate in force, and the
version priced by the AA-810 engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal as D
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration.conftest import MIGRATIONS
from tests.integration.supply import purge_supply
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER, CHECKER, SALES = "it-823-api-maker", "it-823-api-checker", "it-823-api-sales"
HANOI = "d0000000-0000-4000-8000-000000000001"
NOWHERE = "d0000000-0000-4000-8000-0000000000ff"
# Re-applied like test_bk004_pricing: other modules empty pricing_rules (FK created_by); bk-007 approves the seed.
POLICY = [p for p in MIGRATIONS if p.name[:3] in ("004", "006", "007")]
SUPPLY_ENTITIES = ["supplier", "supplier_contract", "hotel", "hotel_room_type", "service", "supplier_rate"]


async def _clean(conn: asyncpg.Connection) -> None:
    await purge_supply(conn)
    await conn.execute(
        "DELETE FROM booking.trip_case_versions WHERE trip_case_id IN"
        " (SELECT id FROM booking.trip_cases WHERE notes = 'it-823')"
    )
    await conn.execute("DELETE FROM booking.trip_cases WHERE notes = 'it-823'")
    await conn.execute("DELETE FROM booking.customers WHERE email LIKE 'it-823-%'")
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    await conn.execute("DELETE FROM booking.fx_rates")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type = ANY($1::text[])", SUPPLY_ENTITIES)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    for path in POLICY:
        await conn.execute(path.read_text(encoding="utf-8"))
    await _clean(conn)
    for staff_id, role in ((MAKER, "contracting"), (CHECKER, "admin"), (SALES, "sales")):
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


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


async def _supplier(c: httpx.AsyncClient, headers: dict[str, str], name: str, categories: list[str]) -> dict:
    body = {"company_name": name, "categories": categories, "country_code": "VN"}
    return await _ok(await c.post("/suppliers", json=body, headers=headers), 201)


async def test_hotels_and_services_crud_rbac_and_facts_only(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    maker, checker, sales = _auth(cis, MAKER), _auth(cis, CHECKER), _auth(cis, SALES)
    sup = await _supplier(c, maker, "Red River DMC", ["hotel", "activity", "transport"])
    meals_only = await _supplier(c, maker, "Pho Kitchen", ["meal"])
    body = {
        "supplier_id": sup["code"],
        "name": " Hanoi Emerald Hotel ",
        "star_rating": 4,
        "destination_id": HANOI,
        "amenities": ["Pool", "pool", " Spa ", ""],
        "links": [{"url": "https://emerald.example/extranet"}],
        "check_in_time": "14:00",
    }

    assert (await c.post("/hotels", json=body, headers=sales)).status_code == 403
    hotel = await _ok(await c.post("/hotels", json=body, headers=maker), 201)
    assert hotel["code"].startswith("HTL-") and hotel["name"] == "Hanoi Emerald Hotel"
    assert hotel["country_code"] == "VN" and hotel["destination"]["name"] == "Hanoi"
    assert hotel["amenities"] == ["Pool", "Spa"] and hotel["links"][0]["label"] == "https://emerald.example/extranet"
    assert hotel["supplier_code"] == sup["code"] and hotel["status"] == "active" and hotel["room_types"] == []
    assert hotel["check_in_time"] == "14:00:00"

    for bad, detail in (
        ({**body, "name": "hanoi emerald hotel"}, "hotel_exists"),
        ({**body, "name": "Geo", "latitude": "21.03"}, "geo_pair"),
        ({**body, "name": "Nowhere", "destination_id": NOWHERE}, "unknown_destination"),
        ({**body, "name": "Kitchen Inn", "supplier_id": meals_only["id"]}, "supplier_category_missing"),
    ):
        r = await c.post("/hotels", json=bad, headers=maker)
        assert r.status_code in (409, 422) and r.json()["detail"] == detail, r.text
    # Facts only (ADR 0004a d2, Q17): a description or photo field is refused, not stored.
    for field in ("description", "photo_url"):
        assert (await c.post("/hotels", json={**body, "name": "X", field: "y"}, headers=maker)).status_code == 422

    deluxe = await _ok(
        await c.post(
            f"/hotels/{hotel['code']}/room-types",
            json={"name": "Deluxe Double", "max_occupancy": 2, "bed_types": ["Queen"]},
            headers=maker,
        ),
        201,
    )
    await _ok(await c.post(f"/hotels/{hotel['id']}/room-types", json={"name": "Family Suite"}, headers=checker), 201)
    dup = await c.post(f"/hotels/{hotel['id']}/room-types", json={"name": "deluxe double"}, headers=maker)
    assert dup.status_code == 409 and dup.json()["detail"] == "room_type_exists"
    room = await _ok(await c.patch(f"/room-types/{deluxe['id']}", json={"max_occupancy": 3}, headers=maker))
    assert room["changed"] == ["max_occupancy"] and room["bed_types"] == ["Queen"]

    listed = await _ok(await c.get("/hotels", params={"q": "emerald", "star_rating": 4}, headers=sales))
    assert listed["total"] == 1 and listed["items"][0]["room_type_count"] == 2
    assert (await _ok(await c.get("/hotels", params={"supplier": meals_only["code"]}, headers=sales)))["total"] == 0
    got = await _ok(await c.get(f"/hotels/{hotel['code'].lower()}", headers=sales))
    assert [t["name"] for t in got["room_types"]] == ["Deluxe Double", "Family Suite"]
    assert (await c.patch(f"/hotels/{hotel['id']}", json={"supplier_id": sup["id"]}, headers=maker)).status_code == 422
    renamed = await _ok(await c.patch(f"/hotels/{hotel['id']}", json={"hotel_style": "Boutique"}, headers=maker))
    assert renamed["changed"] == ["hotel_style"]
    assert (await c.patch(f"/hotels/{hotel['id']}", json={"name": "x"}, headers=sales)).status_code == 403

    svc_body = {
        "supplier_id": sup["id"],
        "service_type": "activity",
        "title": "Old Quarter food walk",
        "destination_id": HANOI,
        "source_id": "cis-component-123",
        "max_group_size": 12,
        "duration_minutes": 180,
        "inclusions": ["Tastings", "Guide"],
    }
    svc = await _ok(await c.post("/services", json=svc_body, headers=maker), 201)
    assert svc["code"].startswith("SVC-") and svc["unit"] == "person" and svc["source_system"] == "cis"
    guide = await c.post("/services", json={**svc_body, "service_type": "guide", "title": "Guide"}, headers=maker)
    assert guide.status_code == 422 and guide.json()["detail"] == "supplier_category_missing"
    transfer = await _ok(
        await c.post(
            "/services",
            json={**svc_body, "service_type": "transfer", "title": "Airport pick-up", "unit": "vehicle"},
            headers=checker,
        ),
        201,
    )
    off = await _ok(
        await c.post(f"/services/{transfer['code']}/deactivate", json={"reason": "seasonal"}, headers=maker)
    )
    assert off["status"] == "inactive" and off["changed"] == ["status"]
    inactive = await _ok(await c.get("/services", params={"status": "inactive"}, headers=sales))
    assert [s["code"] for s in inactive["items"]] == [transfer["code"]]
    by_type = await _ok(await c.get("/services", params={"type": "activity"}, headers=sales))
    assert [s["code"] for s in by_type["items"]] == [svc["code"]]
    cleared = await _ok(await c.patch(f"/services/{svc['id']}", json={"source_id": None}, headers=maker))
    assert cleared["source_system"] is None and cleared["source_id"] is None

    actions = {
        r["action"]
        for r in await admin.fetch(
            "SELECT action FROM booking.audit_logs WHERE entity_type = ANY($1::text[])", SUPPLY_ENTITIES
        )
    }
    assert {"hotel.created", "hotel.updated", "hotel_room_type.created", "service.deactivated"} <= actions


async def test_rates_default_the_item_net_and_price_the_version(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    maker, checker, sales = _auth(cis, MAKER), _auth(cis, CHECKER), _auth(cis, SALES)
    sup = await _supplier(c, maker, "Red River DMC", ["hotel", "activity"])
    hotel = await _ok(
        await c.post(
            "/hotels", json={"supplier_id": sup["id"], "name": "Emerald", "destination_id": HANOI}, headers=maker
        ),
        201,
    )
    rooms = [
        await _ok(await c.post(f"/hotels/{hotel['id']}/room-types", json={"name": n}, headers=maker), 201)
        for n in ("Deluxe Double", "Family Suite")
    ]
    svc = await _ok(
        await c.post(
            "/services",
            json={"supplier_id": sup["id"], "service_type": "activity", "title": "Food walk", "destination_id": HANOI},
            headers=maker,
        ),
        201,
    )
    contract = await _ok(
        await c.post(
            f"/suppliers/{sup['id']}/contracts",
            json={"title": "2027 rates", "valid_from": "2027-01-01", "valid_to": "2027-12-31", "currency": "VND"},
            headers=checker,
        ),
        201,
    )
    rates_url = f"/contracts/{contract['id']}/rates"
    room_rate = await _ok(
        await c.post(
            rates_url,
            json={"hotel_room_type_id": rooms[0]["id"], "meal_plan": "BB", "season": "Low", "net_amount": "1200000"},
            headers=maker,
        ),
        201,
    )
    assert room_rate["unit"] == "room_night" and room_rate["currency"] == "VND"
    assert room_rate["valid_from"] == "2027-01-01" and room_rate["valid_to"] == "2027-12-31"
    await _ok(
        await c.post(rates_url, json={"hotel_room_type_id": rooms[1]["id"], "net_amount": "2000000"}, headers=maker),
        201,
    )
    small = await _ok(
        await c.post(
            rates_url, json={"service_id": svc["id"], "pax_min": 1, "pax_max": 4, "net_amount": "300000"}, headers=maker
        ),
        201,
    )
    await _ok(
        await c.post(
            rates_url,
            json={"service_id": svc["id"], "pax_min": 5, "pax_max": 10, "net_amount": "250000"},
            headers=maker,
        ),
        201,
    )
    for bad, detail in (
        ({"service_id": svc["id"], "meal_plan": "BB", "net_amount": "1"}, "meal_plan_rooms_only"),
        ({"service_id": svc["id"], "valid_from": "2026-12-01", "net_amount": "1"}, "rate_outside_contract"),
        ({"service_id": svc["id"], "pax_min": 3, "pax_max": 2, "net_amount": "1"}, "invalid_pax_band"),
    ):
        r = await c.post(rates_url, json=bad, headers=maker)
        assert r.status_code == 422 and r.json()["detail"] == detail, r.text
    assert (
        await c.post(rates_url, json={"service_id": svc["id"], "net_amount": "1"}, headers=sales)
    ).status_code == 403
    edited = await _ok(await c.patch(f"/rates/{small['id']}", json={"net_amount": "320000.00"}, headers=maker))
    assert edited["changed"] == ["net_amount"] and edited["net_amount"] == "320000.00"

    # The rates made MAKER the contract's maker, though CHECKER created it: CHECKER publishes, MAKER cannot.
    mine = await c.patch(f"/contracts/{contract['id']}", json={"status": "published"}, headers=maker)
    assert mine.status_code == 403 and mine.json()["detail"] == "maker_checker"
    await _ok(await c.patch(f"/contracts/{contract['id']}", json={"status": "published"}, headers=checker))
    locked = await c.patch(f"/rates/{small['id']}", json={"net_amount": "1"}, headers=maker)
    assert locked.status_code == 409 and locked.json()["detail"] == "contract_locked"
    assert (await c.delete(f"/rates/{small['id']}", headers=maker)).status_code == 409
    listed = await _ok(await c.get(f"/hotels/{hotel['code']}/rates", headers=sales))
    assert len(listed["items"]) == 2 and {r["contract_status"] for r in listed["items"]} == {"published"}
    in_force = await _ok(
        await c.get(
            "/supply/rate-in-force", params={"service_id": svc["id"], "on": "2027-03-11", "pax": 6}, headers=sales
        )
    )
    assert in_force["rate"]["net_amount"] == "250000.00"
    options = await _ok(await c.get("/supply/options", params={"q": "emerald"}, headers=sales))
    assert [o["label"] for o in options["items"]] == ["Emerald · Deluxe Double", "Emerald · Family Suite"]

    # A draft rate is not a price yet: a cost line may not name it.
    draft = await _ok(
        await c.post(
            f"/suppliers/{sup['id']}/contracts",
            json={"title": "2028 rates", "valid_from": "2028-01-01", "currency": "VND"},
            headers=maker,
        ),
        201,
    )
    draft_rate = await _ok(
        await c.post(
            f"/contracts/{draft['id']}/rates", json={"service_id": svc["id"], "net_amount": "9"}, headers=maker
        ),
        201,
    )
    gone = await _ok(
        await c.post(
            f"/contracts/{draft['id']}/rates",
            json={"hotel_room_type_id": rooms[1]["id"], "net_amount": "7"},
            headers=maker,
        ),
        201,
    )
    assert (await c.delete(f"/rates/{gone['id']}", headers=maker)).status_code == 204

    # AA-910: the rate rows (deleted rate included) show on the supplier, hotel and service pages.
    def rate_rows(record: dict[str, Any]) -> list[str]:
        return sorted(a["action"] for a in record["recent_activity"] if a["entity_type"] == "supplier_rate")

    sup_page = await _ok(await c.get(f"/suppliers/{sup['id']}", headers=sales))
    hotel_page = await _ok(await c.get(f"/hotels/{hotel['code']}", headers=sales))
    svc_page = await _ok(await c.get(f"/services/{svc['code']}", headers=sales))
    assert rate_rows(sup_page) == ["supplier_rate.created"] * 6 + ["supplier_rate.deleted", "supplier_rate.updated"]
    assert rate_rows(hotel_page) == ["supplier_rate.created"] * 3 + ["supplier_rate.deleted"]
    assert rate_rows(svc_page) == ["supplier_rate.created"] * 3 + ["supplier_rate.updated"]
    deleted = next(a for a in hotel_page["recent_activity"] if a["action"] == "supplier_rate.deleted")
    assert deleted["entity_label"].endswith("Emerald · Family Suite") and deleted["summary"] == "Removed rate 7.00 VND"

    customer_id = await admin.fetchval(
        "INSERT INTO booking.customers (email) VALUES ('it-823-api@example.test') RETURNING id"
    )
    case = await admin.fetchrow(
        "INSERT INTO booking.trip_cases (customer_id, start_date, pax, notes) VALUES ($1, '2027-03-10', 2, 'it-823')"
        " RETURNING id, code",
        customer_id,
    )
    await admin.execute(
        "INSERT INTO booking.trip_case_versions (trip_case_id, version_number) VALUES ($1, 1)", case["id"]
    )
    await admin.execute(
        "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source)"
        " VALUES ('VND', 25000, 0, DATE '2026-10-01', 'test')"
    )
    await admin.execute(
        "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by)"
        " SELECT 'fx_rate', id, 'it-checker' FROM booking.fx_rates ON CONFLICT (fx_rate_id) DO NOTHING"
    )
    base = f"/trip-cases/{case['code']}/versions/1"
    items = [
        {"title": "Emerald, 2 nights", "day_from": 1, "day_to": 3, "hotel_room_type_id": rooms[0]["id"]},
        {"title": "Food walk", "day_from": 2, "day_to": 2, "service_id": svc["id"]},
    ]
    bad_rate = {**items[1], "costs": [{"net_amount": "9", "net_currency": "VND", "supplier_rate_id": draft_rate["id"]}]}
    r = await c.put(f"{base}/items", json={"items": [items[0], bad_rate]}, headers=sales)
    assert r.status_code == 422 and r.json()["detail"] == "unknown_rate"

    saved = await _ok(await c.put(f"{base}/items", json={"items": items}, headers=sales))
    defaults = {d["position"]: d for d in saved["rate_defaults"]}
    assert defaults[1]["rate_id"] == room_rate["id"] and defaults[1]["on"] == "2027-03-10"
    room_cost, walk_cost = (i["costs"] for i in saved["items"])
    # 1,200,000 VND x 2 nights; 320,000 VND x 2 pax (pax band 1-4).
    assert [(x["net_amount"], x["net_currency"]) for x in room_cost] == [("2400000.00", "VND")]
    assert room_cost[0]["supplier_rate_id"] == room_rate["id"] and "x 2 night" in room_cost[0]["supplier_note"]
    assert [(x["net_amount"], x["supplier_rate_id"]) for x in walk_cost] == [("640000.00", small["id"])]
    assert saved["items"][0]["supply"]["label"] == "Emerald · Deluxe Double"

    # Saving again without costs keeps the lines (no second default, no price reset).
    again = await _ok(
        await c.put(
            f"{base}/items",
            json={"items": [{**it, "id": saved["items"][n]["id"]} for n, it in enumerate(items)]},
            headers=sales,
        )
    )
    assert again["rate_defaults"] == [] and [len(i["costs"]) for i in again["items"]] == [1, 1]

    priced = await _ok(await c.post(f"{base}/price", json={"channel": "b2c_tailor_made"}, headers=sales))
    # 3,040,000 VND = 121.60 USD net; the AA-810 engine prices it (seeded b2c_tailor_made rule).
    assert D(priced["quote"]["net_total_usd"]) == D("121.60") and D(priced["version"]["total_price"]) > D("121.60")

    # The builder sends the same lines back without supplier_rate_id: lines (and provenance) and the price stay.
    plain = [
        {
            **it,
            "id": again["items"][n]["id"],
            "costs": [
                {k: x[k] for k in ("net_amount", "net_currency", "supplier_note")} for x in again["items"][n]["costs"]
            ],
        }
        for n, it in enumerate(items)
    ]
    kept = await _ok(await c.put(f"{base}/items", json={"items": plain}, headers=sales))
    assert kept["total_price"] == priced["version"]["total_price"]
    assert kept["items"][0]["costs"][0]["supplier_rate_id"] == room_rate["id"]

    # Deactivated supply cannot be newly booked.
    await _ok(await c.post(f"/services/{svc['id']}/deactivate", headers=maker))
    third = {"title": "Walk again", "day_from": 3, "day_to": 3, "service_id": svc["id"]}
    r = await c.put(
        f"{base}/items",
        json={"items": [*({**it, "id": again["items"][n]["id"]} for n, it in enumerate(items)), third]},
        headers=sales,
    )
    assert r.status_code == 422 and r.json()["detail"] == "supply_inactive"
