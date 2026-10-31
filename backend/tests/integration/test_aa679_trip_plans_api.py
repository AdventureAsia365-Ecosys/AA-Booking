"""AA-679 trip plans over HTTP on a real PostgreSQL (bk-009 + a stub of the CIS catalog views, role ``booking``):
list / facets / detail joined live to catalog.v_tour, the commercial upsert (one row per tour, audited once per
change), "Use for trip case" (new case with V1 from the CIS days, or a new version of an existing case), and a tour
that leaves the catalog dropping out of every read."""

from __future__ import annotations

import json
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

BHUTAN_A = "aa679000-0000-4000-8000-00000000000a"
BHUTAN_B = "aa679000-0000-4000-8000-00000000000b"
LAOS = "aa679000-0000-4000-8000-00000000000c"
MISSING = "aa679000-0000-4000-8000-0000000000ff"
ADMIN, CONTRACTING, SALES, CONTENT = "it-679-admin", "it-679-contracting", "it-679-sales", "it-679-content"
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
    await conn.execute("TRUNCATE catalog.v_tour, catalog.stub_parsed_day, catalog.v_tour_photo")
    for staff_id, role in (
        (ADMIN, "admin"),
        (CONTRACTING, "contracting"),
        (SALES, "sales"),
        (CONTENT, "content_creator"),
    ):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, name, role, active) VALUES ($1, $2, $3, true)",
            staff_id,
            staff_id.replace("it-679-", "").title(),
            role,
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
        await conn.execute("TRUNCATE catalog.v_tour, catalog.stub_parsed_day, catalog.v_tour_photo")
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
    # cis_role only matters on first provisioning; the fixture rows already carry the Booking role.
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


COMMERCIAL = {
    "sellable": True,
    "trip_style": "Private",
    "default_channel": "b2c_tailor_made",
    "price_hint": {"amount": "2450.00", "currency": "USD", "basis": "per_person"},
    "commercial_notes": "DMC net, 2 pax minimum",
}


async def test_list_facets_and_detail(client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection) -> None:
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
    # A tour without a country counts in the KPI totals (same total as the list) but is no country option.
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, title) VALUES ('aa679000-0000-4000-8000-0000000000aa', 'No country')"
    )
    with_none = (await c.get("/catalog/trip-plans/facets", headers=content)).json()
    listed = (await c.get("/catalog/trip-plans", params={"limit": 1}, headers=content)).json()
    assert with_none["total"] == listed["total"] == 4 and len(with_none["countries"]) == 2
    await admin.execute("DELETE FROM catalog.v_tour WHERE title = 'No country'")

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
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
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


async def test_use_for_trip_case(client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection) -> None:
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


async def test_items_read_back_a_cis_rewrite_and_staff_refresh_them(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    """AA-1013: a version made before CIS rewrote the tour keeps its titles; each item says what CIS says today, and a
    staff save of the CIS titles clears the flags. Nothing changes by itself; the trail does not store the read-back."""
    c, cis = client
    contracting, sales = _auth(cis, CONTRACTING), _auth(cis, SALES)
    await c.put(f"/catalog/trip-plans/{BHUTAN_A}/commercial", json=COMMERCIAL, headers=contracting)
    body = {"new_customer": {"name": "Ana Traveller", "email": "ana@example.com"}, "pax_adults": 2}
    made = (await c.post(f"/catalog/trip-plans/{BHUTAN_A}/trip-cases", json=body, headers=sales)).json()
    code = made["trip_case"]["code"]
    assert [i["cis"]["changed"] for i in made["version"]["items"]] == [False, False, False]
    assert made["version"]["items"][0]["cis"]["tour_title"] == "Bhutan Dragon Trails"

    # CIS rewrites days 1 and 3 (a later block of the same day wins, as in CIS 289).
    await admin.executemany(
        "INSERT INTO catalog.v_tour_day (source_id, day_number, title, body, places) VALUES ($1, $2, $3, $4, $5)",
        [(BHUTAN_A, 1, "Paro arrival", "Welcome", ["Paro"]), (BHUTAN_A, 3, "Tiger's  Nest hike", "Hike", None)],
    )
    url = f"/trip-cases/{code}/versions/1/items"
    got = (await c.get(url, headers=sales)).json()
    assert [i["title"] for i in got["items"]] == ["Arrive in Paro", "Day 2", "Tiger's Nest hike"]
    assert [(i["cis"]["day_number"], i["cis"]["title"], i["cis"]["changed"]) for i in got["items"]] == [
        (1, "Paro arrival", True),
        (2, None, False),  # CIS has no title for day 2: nothing to compare
        (3, "Tiger's  Nest hike", False),  # whitespace only
    ]

    # Refresh = staff save the CIS titles (the builder's "Refresh from CIS"), ids kept.
    items = [
        {
            "id": i["id"],
            "title": i["cis"]["title"] if i["cis"]["changed"] else i["title"],
            "day_from": i["day_from"],
            "day_to": i["day_to"],
            "source_tour_id": i["source_tour_id"],
        }
        for i in got["items"]
    ]
    saved = await c.put(url, json={"items": items}, headers=sales)
    assert saved.status_code == 200, saved.text
    assert [(i["title"], i["cis"]["changed"]) for i in saved.json()["items"]] == [
        ("Paro arrival", False),
        ("Day 2", False),
        ("Tiger's Nest hike", False),
    ]
    trail = await admin.fetchval(
        "SELECT after::text FROM booking.audit_logs WHERE action = 'trip_case_version.items_replaced'"
    )
    assert "Paro arrival" in trail and '"cis"' not in trail

    # A catalog that cannot be read leaves the items without a read-back; the version still loads.
    await admin.execute("REVOKE SELECT ON catalog.v_tour FROM booking")
    try:
        soft = await c.get(url, headers=sales)
        assert soft.status_code == 200 and [i["cis"] for i in soft.json()["items"]] == [None, None, None]
    finally:
        await admin.execute("GRANT SELECT ON catalog.v_tour TO booking")


async def test_one_day_tripplanner_legs_are_not_read_as_cis_day_copies(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    """AA-1013 review: a one-day TripPlanner leg is titled from the TripPlanner snapshot (or "AA tour <key>"), not
    with a CIS day title, so a version prefilled from legs never says CIS has newer text: a lone one-day leg of a
    3-day CIS tour, and a one-day CIS tour whose leg has no named snapshot day."""
    c, cis = client
    snapshot = {"days": [{"day": 1, "source_tour_id": BHUTAN_A, "name": "Paro valley evening"}], "custom_days": []}
    customer = await admin.fetchval(
        "INSERT INTO booking.customers (name, email) VALUES ('Lead', 'lead-1013@example.com') RETURNING id"
    )
    case = await admin.fetchrow(
        "INSERT INTO booking.trip_cases (customer_id, source_system, inquiry_snapshot, currency)"
        " VALUES ($1, 'tripplanner', $2::jsonb, 'USD') RETURNING id::text AS id, code",
        customer,
        json.dumps(snapshot),
    )
    await admin.executemany(
        "INSERT INTO booking.trip_case_legs (trip_case_id, position, source_tour_id, day_from, day_to)"
        " VALUES ($1::uuid, $2, $3, $4, $5)",
        [(case["id"], 1, BHUTAN_A, 1, 1), (case["id"], 2, LAOS, 2, 2)],
    )
    sales = _auth(cis, SALES)
    made = await c.post(f"/trip-cases/{case['code']}/versions", json={}, headers=sales)
    assert made.status_code == 201, made.text
    got = (await c.get(f"/trip-cases/{case['code']}/versions/1/items", headers=sales)).json()["items"]
    assert [i["title"] for i in got] == ["Paro valley evening", f"AA tour {LAOS}"]
    # CIS day 1 of each tour says something else ("Arrive in Paro", "Luang Prabang"); neither leg is a day copy.
    assert [(i["cis"]["tour_title"], i["cis"]["day_number"], i["cis"]["changed"]) for i in got] == [
        ("Bhutan Dragon Trails", None, False),
        ("Mekong Valley", None, False),
    ]


async def test_tour_leaving_the_catalog_drops_out(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
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
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    await admin.execute("REVOKE SELECT ON catalog.v_tour FROM booking")
    try:
        r = await c.get("/catalog/trip-plans", headers=_auth(cis, CONTENT))
        assert r.status_code == 503 and r.json()["detail"] == "catalog_unavailable"
    finally:
        await admin.execute("GRANT SELECT ON catalog.v_tour TO booking")
