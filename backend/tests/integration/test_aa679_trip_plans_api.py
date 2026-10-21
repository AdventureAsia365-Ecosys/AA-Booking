"""AA-679 trip plans over HTTP on a real PostgreSQL (bk-009 + a stub of the CIS catalog views, role ``booking``):
list / facets / detail joined live to catalog.v_tour, the commercial upsert (one row per tour, audited once per
change), "Use for trip case" (new case with V1 from the CIS days, or a new version of an existing case), and a tour
that leaves the catalog dropping out of every read."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import WORKFORCE_ISSUER, LocalStaff

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BHUTAN_A = "aa679000-0000-4000-8000-00000000000a"
BHUTAN_B = "aa679000-0000-4000-8000-00000000000b"
LAOS = "aa679000-0000-4000-8000-00000000000c"
MISSING = "aa679000-0000-4000-8000-0000000000ff"
ADMIN, CONTRACTING, SALES, CONTENT = "it-679-admin", "it-679-contracting", "it-679-sales", "it-679-content"
ROLES = {ADMIN: "admin", CONTRACTING: "contracting", SALES: "sales", CONTENT: "content_creator"}
TABLES = (
    "booking.notification_reads, booking.trip_case_countries, booking.trip_case_version_itinerary, "
    "booking.trip_case_versions, booking.staff_assignments, booking.trip_case_status_history, "
    "booking.trip_case_legs, booking.notifications, booking.trip_cases, booking.trip_plans, booking.staff_users, "
    "booking.audit_logs, booking.customers"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(f"TRUNCATE {TABLES} CASCADE")
    await conn.execute("TRUNCATE catalog.v_tour, catalog.v_tour_day, catalog.v_tour_photo")
    for staff_id, role in ROLES.items():
        # Linked to the local WORKFORCE identity whose sub is the row key (AA-911: WORKFORCE is the only sign-in).
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, name, role, active, cognito_iss, cognito_sub)"
            " VALUES ($1, $2, $3, true, $4, $1)",
            staff_id,
            staff_id.replace("it-679-", "").title(),
            role,
            WORKFORCE_ISSUER,
        )
    await conn.executemany(
        "INSERT INTO catalog.v_tour (source_id, sku, title, country, duration_days, cover_photo_path, photo_count,"
        " highlights, inclusions) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)",
        [
            (BHUTAN_A, "BT-7D", "Bhutan Dragon Trails", "Bhutan", 3, "/public/photos/a", 2, '["Tiger Nest"]', "Guide"),
            (BHUTAN_B, "BT-5D", "Bhutan Valleys", "Bhutan", 5, None, 0, None, None),
            (LAOS, "LA-10D", "Mekong Valley", "Laos", 10, None, 0, None, None),
        ],
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour_day (source_id, day_number, title, body, places) VALUES ($1, $2, $3, $4, $5)",
        [
            (BHUTAN_A, 2, "", "Free day", ["Paro"]),  # no title -> "Day 2"
            (BHUTAN_A, 1, "Arrive in Paro", "Welcome", ["Paro"]),
            (BHUTAN_A, 3, "Tiger's Nest hike", "Hike", None),
            (LAOS, 1, "Luang Prabang", "Alms", ["Luang Prabang"]),
        ],
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour_photo (source_id, path, place_label, position) VALUES ($1, $2, $3, $4)",
        [(BHUTAN_A, "/public/photos/b", "Punakha", 2), (BHUTAN_A, "/public/photos/a", "Paro", 1)],
    )
    try:
        yield conn
    finally:
        await conn.execute("TRUNCATE catalog.v_tour, catalog.v_tour_day, catalog.v_tour_photo")
        await conn.close()


@pytest_asyncio.fixture
async def client(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalStaff]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalStaff()
    app = create_app(settings, db, cis.verifier())
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


def _auth(cis: LocalStaff, sub: str) -> dict[str, str]:
    # The WORKFORCE group carries the same role as the fixture row (the role is re-read from the group each request).
    return {"Authorization": f"Bearer {cis.token(sub, ROLES[sub])}"}


COMMERCIAL = {
    "sellable": True,
    "trip_style": "Private",
    "default_channel": "b2c_tailor_made",
    "price_hint": {"amount": "2450.00", "currency": "USD", "basis": "per_person"},
    "commercial_notes": "DMC net, 2 pax minimum",
}


async def test_list_facets_and_detail(client: tuple[httpx.AsyncClient, LocalStaff], admin: asyncpg.Connection) -> None:
    c, cis = client
    contracting, content = _auth(cis, CONTRACTING), _auth(cis, CONTENT)

    everything = (await c.get("/catalog/trip-plans", headers=content)).json()
    assert everything["total"] == 3 and [i["title"] for i in everything["items"]] == [
        "Bhutan Dragon Trails",
        "Bhutan Valleys",
        "Mekong Valley",
    ]
    assert all(i["sellable"] is False and i["has_commercial_row"] is False for i in everything["items"])

    put = await c.put(f"/catalog/trip-plans/{BHUTAN_A}/commercial", json=COMMERCIAL, headers=contracting)
    assert put.status_code == 200, put.text

    bhutan = (await c.get("/catalog/trip-plans", params={"country": "bhutan"}, headers=contracting)).json()
    assert bhutan["total"] == 2
    sellable = (await c.get("/catalog/trip-plans", params={"sellable": "true"}, headers=contracting)).json()
    assert [i["source_id"] for i in sellable["items"]] == [BHUTAN_A]
    assert sellable["items"][0]["price_hint"] == {"amount": "2450.00", "currency": "USD", "basis": "per_person"}
    not_sellable = (await c.get("/catalog/trip-plans", params={"sellable": "false"}, headers=contracting)).json()
    assert not_sellable["total"] == 2
    by_sku = (await c.get("/catalog/trip-plans", params={"q": "la-10"}, headers=contracting)).json()
    by_id = (await c.get("/catalog/trip-plans", params={"q": LAOS}, headers=contracting)).json()
    assert [i["source_id"] for i in by_sku["items"]] == [LAOS] == [i["source_id"] for i in by_id["items"]]
    styled = (await c.get("/catalog/trip-plans", params={"trip_style": "private"}, headers=contracting)).json()
    assert styled["total"] == 1
    page = await c.get("/catalog/trip-plans", params={"limit": 1, "offset": 2, "sort": "-title"}, headers=content)
    assert page.json()["items"][0]["source_id"] == BHUTAN_A and page.json()["total"] == 3
    assert (await c.get("/catalog/trip-plans", params={"sort": "price"}, headers=content)).status_code == 422

    # Content creators read the catalog, never the commercial price data.
    hidden = (await c.get("/catalog/trip-plans", params={"sellable": "true"}, headers=content)).json()["items"][0]
    assert hidden["sellable"] is True and hidden["price_hint"] is None and hidden["default_channel"] is None

    facets = (await c.get("/catalog/trip-plans/facets", headers=content)).json()
    assert facets["total"] == 3 and facets["sellable"] == 1 and facets["trip_styles"] == ["Private"]
    assert facets["countries"] == [
        {"country": "Bhutan", "tours": 2, "sellable": 1},
        {"country": "Laos", "tours": 1, "sellable": 0},
    ]

    detail = (await c.get(f"/catalog/trip-plans/{BHUTAN_A}", headers=contracting)).json()
    assert [d["day_number"] for d in detail["days"]] == [1, 2, 3] and detail["days"][0]["places"] == ["Paro"]
    assert [p["path"] for p in detail["photos"]] == ["/public/photos/a", "/public/photos/b"]
    assert detail["highlights"] == ["Tiger Nest"] and detail["commercial_notes"] == "DMC net, 2 pax minimum"
    assert detail["commercial"]["created_by"]["id"] == CONTRACTING  # name comes from the latest token
    assert detail["cases_started"] == 0
    content_detail = (await c.get(f"/catalog/trip-plans/{BHUTAN_A}", headers=content)).json()
    assert content_detail["commercial_notes"] is None and content_detail["price_hint"] is None
    untouched = (await c.get(f"/catalog/trip-plans/{LAOS}", headers=content)).json()
    assert untouched["commercial"] is None and untouched["sellable"] is False

    assert (await c.get(f"/catalog/trip-plans/{MISSING}", headers=content)).status_code == 404
    assert (await c.get("/catalog/trip-plans/not-a-uuid", headers=content)).status_code == 422


async def test_commercial_upsert_is_one_row_and_audited(
    client: tuple[httpx.AsyncClient, LocalStaff], admin: asyncpg.Connection
) -> None:
    c, cis = client
    contracting, sales = _auth(cis, CONTRACTING), _auth(cis, SALES)
    url = f"/catalog/trip-plans/{BHUTAN_A}/commercial"

    assert (await c.put(url, json=COMMERCIAL, headers=sales)).status_code == 403
    first = (await c.put(url, json=COMMERCIAL, headers=contracting)).json()
    assert first["sellable"] is True and "sellable" in first["changed"]
    again = (await c.put(url, json=COMMERCIAL, headers=contracting)).json()
    assert again["changed"] == []
    off = (await c.put(url, json={**COMMERCIAL, "sellable": False, "price_hint": None}, headers=contracting)).json()
    assert off["changed"] == ["price_hint_amount", "price_hint_basis", "price_hint_currency", "sellable"]
    assert off["price_hint"] is None

    assert await admin.fetchval("SELECT count(*) FROM booking.trip_plans WHERE source_id = $1", BHUTAN_A) == 1
    logs = await admin.fetch(
        "SELECT action, summary FROM booking.audit_logs WHERE entity_type = 'trip_plan' ORDER BY created_at, id"
    )
    assert [(r["action"], r["summary"]) for r in logs] == [
        ("trip_plan.created", "Marked sellable"),
        ("trip_plan.updated", "Marked not for sale; updated price_hint_amount, price_hint_basis, price_hint_currency"),
    ]
    missing = await c.put(f"/catalog/trip-plans/{MISSING}/commercial", json=COMMERCIAL, headers=contracting)
    assert missing.status_code == 404 and missing.json()["detail"] == "tour_not_in_catalog"
    bad = await c.put(url, json={**COMMERCIAL, "default_channel": "retail"}, headers=contracting)
    assert bad.status_code == 422


async def test_use_for_trip_case(client: tuple[httpx.AsyncClient, LocalStaff], admin: asyncpg.Connection) -> None:
    c, cis = client
    contracting, sales = _auth(cis, CONTRACTING), _auth(cis, SALES)
    use = f"/catalog/trip-plans/{BHUTAN_A}/trip-cases"
    body = {"new_customer": {"name": "Ana Traveller", "email": "ana@example.com"}, "pax_adults": 2}

    not_sellable = await c.post(use, json=body, headers=sales)
    assert not_sellable.status_code == 409 and not_sellable.json()["detail"] == "trip_plan_not_sellable"
    await c.put(f"/catalog/trip-plans/{BHUTAN_A}/commercial", json=COMMERCIAL, headers=contracting)
    assert (await c.post(use, json=body, headers=contracting)).status_code == 403

    runs = [await c.post(use, json=body, headers=sales) for _ in range(2)]
    assert [r.status_code for r in runs] == [201, 201], runs[0].text
    cases = [r.json() for r in runs]
    assert cases[0]["trip_case"]["code"] != cases[1]["trip_case"]["code"]
    for out in cases:
        assert out["mode"] == "new_case"
        case, version = out["trip_case"], out["version"]
        assert case["source_system"] == "cis" and case["source_id"] == BHUTAN_A
        assert case["trip_style"] == "Private" and case["countries"] == ["BT"]
        assert version["version_number"] == 1 and version["title"] == "Bhutan Dragon Trails"
        assert version["channel"] == "b2c_tailor_made" and version["total_price"] is None
        items = version["items"]
        assert [(i["title"], i["day_from"], i["day_to"]) for i in items] == [
            ("Arrive in Paro", 1, 1),
            ("Day 2", 2, 2),
            ("Tiger's Nest hike", 3, 3),
        ]
        assert {i["source_tour_id"] for i in items} == {BHUTAN_A}
    days = await admin.fetchval("SELECT count(*) FROM catalog.v_tour_day WHERE source_id = $1", BHUTAN_A)
    assert len(cases[0]["version"]["items"]) == days

    # A new version of an existing case from the plan (V2; V1 stays).
    code = cases[0]["trip_case"]["code"]
    v2 = await c.post(use, json={"trip_case": code, "notes": "second option"}, headers=sales)
    assert v2.status_code == 201, v2.text
    assert v2.json()["mode"] == "new_version" and v2.json()["version"]["version_number"] == 2
    assert v2.json()["version"]["notes"] == "second option" and len(v2.json()["version"]["items"]) == 3
    both = await c.post(use, json={**body, "trip_case": code}, headers=sales)
    assert both.status_code == 422

    # A sellable plan without canonical days gives one item over its duration.
    await c.put(f"/catalog/trip-plans/{BHUTAN_B}/commercial", json={"sellable": True}, headers=contracting)
    plain = (await c.post(f"/catalog/trip-plans/{BHUTAN_B}/trip-cases", json=body, headers=sales)).json()
    assert [(i["title"], i["day_from"], i["day_to"]) for i in plain["version"]["items"]] == [("Bhutan Valleys", 1, 5)]
    assert plain["version"]["channel"] is None and plain["trip_case"]["trip_style"] is None

    detail = (await c.get(f"/catalog/trip-plans/{BHUTAN_A}", headers=sales)).json()
    assert detail["cases_started"] == 2
    listed = (await c.get("/trip-cases", params={"source": "cis"}, headers=sales)).json()
    assert listed["total"] == 3
    actions = await admin.fetch(
        "SELECT action, summary FROM booking.audit_logs WHERE action = 'trip_case_version.created' ORDER BY created_at"
    )
    assert all(r["summary"].endswith("from trip plan BT-7D") or "BT-5D" in r["summary"] for r in actions)


async def test_tour_leaving_the_catalog_drops_out(
    client: tuple[httpx.AsyncClient, LocalStaff], admin: asyncpg.Connection
) -> None:
    c, cis = client
    contracting = _auth(cis, CONTRACTING)
    await c.put(f"/catalog/trip-plans/{LAOS}/commercial", json=COMMERCIAL, headers=contracting)
    await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = $1", LAOS)  # trashed / unpublished in CIS

    listed = (await c.get("/catalog/trip-plans", headers=contracting)).json()
    assert listed["total"] == 2 and LAOS not in {i["source_id"] for i in listed["items"]}
    assert (await c.get(f"/catalog/trip-plans/{LAOS}", headers=contracting)).status_code == 404
    use = await c.post(
        f"/catalog/trip-plans/{LAOS}/trip-cases",
        json={"new_customer": {"name": "X", "phone": "+8412345"}},
        headers=_auth(cis, SALES),
    )
    assert use.status_code == 404
    # The commercial row stays as history.
    assert await admin.fetchval("SELECT sellable FROM booking.trip_plans WHERE source_id = $1", LAOS) is True


async def test_catalog_unavailable_is_503(
    client: tuple[httpx.AsyncClient, LocalStaff], admin: asyncpg.Connection
) -> None:
    c, cis = client
    await admin.execute("REVOKE SELECT ON catalog.v_tour FROM booking")
    try:
        r = await c.get("/catalog/trip-plans", headers=_auth(cis, CONTENT))
        assert r.status_code == 503 and r.json()["detail"] == "catalog_unavailable"
    finally:
        await admin.execute("GRANT SELECT ON catalog.v_tour TO booking")
