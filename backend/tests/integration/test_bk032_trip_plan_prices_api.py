"""AA-996 (bk-032) over HTTP on a real PostgreSQL, as role ``booking``: the supplier of a trip plan (set, kept when
left out, cleared, active only, hidden from content creators), the price matrix (rows by hand and rows priced from a
net cost with the approved rule and FX rate, overrides below the floor need a reason, overlaps refused, one audit row
per change, nothing written when nothing changed), the from price, and the list rows (sellable first by default,
destinations, cover photo fallback, from price)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

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

PLAN = "aa996032-0000-4000-8000-00000000000a"
OTHER = "aa996032-0000-4000-8000-00000000000b"
ADMIN, MAKER, CHECKER, SALES, CONTENT = (
    "it-996p-admin", "it-996p-contracting", "it-996p-checker", "it-996p-sales", "it-996p-content",
)  # fmt: skip
POLICY = [p for p in MIGRATIONS if p.name[:3] in ("004", "006", "007")]
NEXT_YEAR = date.today().year + 1
SUPPLIER_NAME = "it-996p Red River DMC"


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute("SET ROLE bk_owner")
    try:
        await conn.execute(
            "DELETE FROM booking.trip_plan_prices WHERE trip_plan_id IN"
            " (SELECT id FROM booking.trip_plans WHERE source_id = ANY($1))",
            [PLAN, OTHER],
        )
        await conn.execute("DELETE FROM booking.trip_plans WHERE source_id = ANY($1)", [PLAN, OTHER])
        await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type = 'trip_plan'")
        await conn.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-996p %'")
        await conn.execute("DELETE FROM booking.policy_approvals WHERE fx_rate_id IS NOT NULL")
        await conn.execute("DELETE FROM booking.fx_rates")
    finally:
        await conn.execute("RESET ROLE")
    await conn.execute("DELETE FROM catalog.v_tour WHERE source_id = ANY($1)", [PLAN, OTHER])
    await conn.execute("DELETE FROM catalog.v_tour_day WHERE source_id = ANY($1)", [PLAN, OTHER])
    await conn.execute("DELETE FROM catalog.v_tour_photo WHERE source_id = ANY($1)", [PLAN, OTHER])


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    for path in POLICY:  # other modules empty pricing_rules; bk-007 approves the seed again
        await conn.execute(path.read_text(encoding="utf-8"))
    await purge_supply(conn)
    await _clean(conn)
    for staff_id, role in (
        (ADMIN, "admin"),
        (MAKER, "contracting"),
        (CHECKER, "admin"),
        (SALES, "sales"),
        (CONTENT, "content_creator"),
    ):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, $2, true, $1)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
            staff_id,
            role,
        )
    # Two catalog tours; titles put the unsellable one first in A-Z order. The plan has no cover, only photos.
    await conn.executemany(
        "INSERT INTO catalog.v_tour (source_id, sku, title, country, duration_days) VALUES ($1, $2, $3, 'Vietnam', $4)",
        [(PLAN, "VN-5D", "Zz Northern Loop", 5), (OTHER, None, "Aa Delta Days", 3)],
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour_day (source_id, day_number, title, places) VALUES ($1, $2, $3, $4)",
        [
            (PLAN, 2, "Ha Giang", ["Ha Giang", " Hanoi "]),
            (PLAN, 1, "Hanoi", ["Hanoi", ""]),
            (PLAN, 3, "Dong Van", ["Dong Van", "Ha Giang"]),
        ],
    )
    await conn.executemany(
        "INSERT INTO catalog.v_tour_photo (source_id, path, place_label, position) VALUES ($1, $2, $3, $4)",
        [(PLAN, "/public/photos/second", "Hanoi", 2), (PLAN, "/public/photos/first", "Ha Giang", 1)],
    )
    fx = await conn.fetchval(
        "INSERT INTO booking.fx_rates (currency, rate_per_usd, buffer_pct, as_of, source, created_by)"
        " VALUES ('VND', 25000, 0, DATE '2026-01-01', 'synthetic', $1) RETURNING id",
        MAKER,
    )
    await conn.execute(
        "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1, $2)",
        fx,
        CHECKER,
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


async def _supplier(conn: asyncpg.Connection, suffix: str, status: str = "active") -> str:
    return await conn.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, status) VALUES ($1, ARRAY['transport'],"
        " 'VN', $2) RETURNING id::text",
        f"{SUPPLIER_NAME} {suffix}",
        status,
    )


COMMERCIAL = {"sellable": True, "trip_style": "Private", "default_channel": "b2c_tailor_made"}


def _row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "channel": "b2c_tailor_made",
        "season": "Low",
        "valid_from": f"{NEXT_YEAR}-01-01",
        "valid_to": f"{NEXT_YEAR}-04-30",
        "pax_min": 2,
        "pax_max": 2,
        "currency": "USD",
        "amount": "1450.00",
    }
    row.update(over)
    return row


async def test_supplier_on_the_commercial_row(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    contracting, content = _auth(cis, MAKER), _auth(cis, CONTENT)
    active, inactive = await _supplier(admin, "A"), await _supplier(admin, "B", "inactive")
    url = f"/catalog/trip-plans/{PLAN}/commercial"

    put = await c.put(url, json={**COMMERCIAL, "supplier_id": active}, headers=contracting)
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["supplier"]["id"] == active and body["supplier"]["company_name"] == f"{SUPPLIER_NAME} A"
    assert body["supplier"]["code"].startswith("SUP-") and "supplier_id" in body["changed"]

    # Left out (the sellable toggle, an older UI): the supplier is kept.
    kept = await c.put(url, json={**COMMERCIAL, "sellable": False}, headers=contracting)
    assert kept.json()["supplier"]["id"] == active and kept.json()["changed"] == ["sellable"]
    # An inactive or unknown supplier is refused when set; null clears it.
    refused = await c.put(url, json={**COMMERCIAL, "supplier_id": inactive}, headers=contracting)
    assert refused.status_code == 422 and refused.json()["detail"] == "supplier_inactive"
    unknown = await c.put(url, json={**COMMERCIAL, "supplier_id": PLAN}, headers=contracting)
    assert unknown.status_code == 422 and unknown.json()["detail"] == "supplier_not_found"

    # An unchanged supplier that was deactivated since stays (and the save goes through).
    await admin.execute("UPDATE booking.suppliers SET status = 'inactive' WHERE id = $1::uuid", active)
    again = await c.put(
        url, json={**COMMERCIAL, "supplier_id": active, "trip_style": "Small group"}, headers=contracting
    )
    assert again.status_code == 200 and again.json()["supplier"]["status"] == "inactive"

    hidden = (await c.get(f"/catalog/trip-plans/{PLAN}", headers=content)).json()
    assert hidden["supplier"] is None and hidden["supplier_id"] is None
    cleared = await c.put(url, json={**COMMERCIAL, "supplier_id": None}, headers=contracting)
    assert cleared.json()["supplier"] is None and "supplier_id" in cleared.json()["changed"]
    actions = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_type = 'trip_plan' ORDER BY created_at, id"
    )
    assert [a["action"] for a in actions] == ["trip_plan.created", *["trip_plan.updated"] * 3]


async def test_price_matrix_by_hand_and_from_price(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    contracting, sales, content = _auth(cis, MAKER), _auth(cis, SALES), _auth(cis, CONTENT)
    url = f"/catalog/trip-plans/{PLAN}/prices"

    empty = await c.get(url, headers=sales)
    assert empty.status_code == 200 and empty.json() == {"source_id": PLAN, "items": [], "from_price": None}
    no_plan = await c.put(url, json={"items": [_row()]}, headers=contracting)
    assert no_plan.status_code == 404 and no_plan.json()["detail"] == "trip_plan_not_found"
    assert (
        await c.put(f"/catalog/trip-plans/{PLAN}/commercial", json=COMMERCIAL, headers=contracting)
    ).status_code == 200

    rows = [
        _row(),
        _row(pax_min=3, pax_max=4, amount="1250"),
        _row(season="High", valid_from=f"{NEXT_YEAR}-10-01", valid_to=f"{NEXT_YEAR}-12-31", amount="1650"),
        _row(currency="EUR", amount="1300"),
        _row(channel="b2b", amount="1100", pax_min=1, pax_max=None),
    ]
    put = await c.put(url, json={"items": rows}, headers=contracting)
    assert put.status_code == 200, put.text
    out = put.json()
    assert [r["position"] for r in out["items"]] == [1, 2, 3, 4, 5]
    assert out["items"][1]["amount"] == "1250.00" and out["items"][4]["pax_max"] is None
    assert all(r["priced"] is False and r["effective_margin"] is None for r in out["items"])
    # B2C, covers 2 pax, still valid, USD first: the 2-pax low season row (EUR 1300 and b2b 1100 do not count).
    assert out["from_price"] == {"amount": "1450.00", "currency": "USD", "basis": "per_person", "season": "Low"}
    assert (await c.get(url, headers=sales)).json() == out

    # The same set again writes nothing.
    assert (await c.put(url, json={"items": rows}, headers=contracting)).status_code == 200
    audits = await admin.fetchval("SELECT count(*) FROM booking.audit_logs WHERE action = 'trip_plan.prices_replaced'")
    assert audits == 1

    # Content creators never read prices; sales cannot write them.
    assert (await c.get(url, headers=content)).status_code == 403
    assert (await c.put(url, json={"items": rows}, headers=sales)).status_code == 403

    # Overlap in one channel and currency is refused, with both rows named.
    overlap = await c.put(url, json={"items": [_row(), _row(pax_min=1, pax_max=2, amount="1500")]}, headers=contracting)
    assert overlap.status_code == 422 and overlap.json()["detail"] == "overlapping_prices"
    assert overlap.json()["rows"] == [1, 2]
    bad = await c.put(url, json={"items": [_row(valid_to=f"{NEXT_YEAR - 1}-01-01")]}, headers=contracting)
    assert bad.status_code == 422 and bad.json()["detail"] == "invalid_validity"
    missing = await c.put(url, json={"items": [_row(amount=None)]}, headers=contracting)
    assert missing.status_code == 422 and missing.json()["detail"] == "price_missing"
    vnd = await c.put(url, json={"items": [_row(currency="VND")]}, headers=contracting)
    assert vnd.status_code == 422 and vnd.json()["detail"] == "invalid_currency"

    # The list shows the from price; a content creator sees none.
    listed = (await c.get("/catalog/trip-plans", params={"q": "VN-5D"}, headers=sales)).json()["items"][0]
    assert listed["from_price"]["amount"] == "1450.00"
    assert (await c.get("/catalog/trip-plans", params={"q": "VN-5D"}, headers=content)).json()["items"][0][
        "from_price"
    ] is None

    # An expired 2-pax row no longer counts; emptying the matrix is allowed.
    past = [_row(valid_from="2020-01-01", valid_to="2020-12-31")]
    assert (await c.put(url, json={"items": past}, headers=contracting)).json()["from_price"] is None
    cleared = await c.put(url, json={"items": []}, headers=contracting)
    assert cleared.status_code == 200 and cleared.json()["items"] == []


async def test_rows_priced_from_net_cost(client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection) -> None:
    """b2c_tailor_made seed rule (bk-004): 30 % margin, 3 % card fee, floor 20 %, peak +3 pts; USD rounds up to 5."""
    c, cis = client
    contracting = _auth(cis, MAKER)
    url = f"/catalog/trip-plans/{PLAN}/prices"
    assert (
        await c.put(f"/catalog/trip-plans/{PLAN}/commercial", json=COMMERCIAL, headers=contracting)
    ).status_code == 200

    net = {"amount": None, "net_amount": "670", "net_currency": "USD"}
    vnd = {"amount": None, "net_amount": "25000000", "net_currency": "VND", "pax_min": 3, "pax_max": 4}
    peak = {**net, "peak": True, "season": "Tet", "valid_from": f"{NEXT_YEAR}-05-01", "valid_to": f"{NEXT_YEAR}-05-10"}
    put = await c.put(url, json={"items": [_row(**net), _row(**vnd), _row(**peak)]}, headers=contracting)
    assert put.status_code == 200, put.text
    first, second, third = put.json()["items"]
    assert first["amount"] == "1000.00" and first["priced"] is True  # 670 / 0.67
    assert first["effective_margin"] == "0.3000" and first["priced_on"] == date.today().isoformat()
    assert first["net_amount"] == "670.00" and first["net_currency"] == "USD"
    assert second["amount"] == "1495.00"  # 25,000,000 VND = 1,000 USD; / 0.67 = 1492.54 -> 1495
    assert third["amount"] == "1050.00" and third["peak"] is True  # peak: 670 / 0.64 = 1046.88 -> 1050

    # A sales override above the floor is kept as the price; below the floor it needs a reason.
    above = await c.put(url, json={"items": [_row(**{**net, "amount": "950"})]}, headers=contracting)
    assert above.status_code == 200 and above.json()["items"][0]["amount"] == "950.00"
    assert above.json()["items"][0]["effective_margin"] == "0.2647"  # 1 - 0.03 - 670 / 950
    assert above.json()["items"][0]["overridden"] is True and first["overridden"] is False
    low = {**net, "amount": "800"}  # 1 - 0.03 - 670 / 800 = 0.1325 < 0.20
    refused = await c.put(url, json={"items": [_row(**low)]}, headers=contracting)
    assert refused.status_code == 422 and refused.json()["detail"] == "override_reason_required"
    assert refused.json()["rows"] == [1]
    ok = await c.put(url, json={"items": [_row(**low)], "below_floor_reason": "Launch offer"}, headers=contracting)
    assert ok.status_code == 200 and ok.json()["items"][0]["amount"] == "800.00"
    audit = await admin.fetchrow(
        "SELECT summary, after->>'below_floor_reason' AS reason FROM booking.audit_logs"
        " WHERE action = 'trip_plan.prices_replaced' ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    assert audit["reason"] == "Launch offer" and "below floor" in audit["summary"]

    # No approved rate for the cost currency: a clear 422, nothing saved.
    thb = await c.put(
        url, json={"items": [_row(amount=None, net_amount="1000", net_currency="THB")]}, headers=contracting
    )
    assert thb.status_code == 422 and thb.json()["detail"] == "fx_rate_missing"
    assert (await c.get(url, headers=contracting)).json()["items"][0]["amount"] == "800.00"
    half = await c.put(url, json={"items": [_row(amount=None, net_amount="10")]}, headers=contracting)
    assert half.status_code == 422 and half.json()["detail"] == "invalid_net_cost"

    # Rows the table cannot hold are a clear 422, never a 500: a net cost pricing to 0, a typo override.
    zero_cost = {"items": [_row(amount=None, net_amount="0", net_currency="USD")]}
    zero = await c.put(url, json=zero_cost, headers=contracting)
    assert zero.status_code == 422 and zero.json()["detail"] == "price_missing"
    typo = {"items": [_row(amount="15", net_amount="2000", net_currency="USD")], "below_floor_reason": "Typo"}
    typo_put = await c.put(url, json=typo, headers=contracting)
    assert typo_put.status_code == 422 and typo_put.json()["detail"] == "override_too_low"
    deep = {"items": [_row(amount="1100", net_amount="2000", net_currency="USD")], "below_floor_reason": "Write-off"}
    deep_put = await c.put(url, json=deep, headers=contracting)
    assert deep_put.status_code == 200 and deep_put.json()["items"][0]["effective_margin"] == "-0.8482"


async def test_list_rows_sellable_first_with_destinations_and_cover(
    client: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = client
    contracting = _auth(cis, MAKER)
    assert (
        await c.put(f"/catalog/trip-plans/{PLAN}/commercial", json=COMMERCIAL, headers=contracting)
    ).status_code == 200

    page = (await c.get("/catalog/trip-plans", params={"country": "Vietnam"}, headers=contracting)).json()
    titles = [i["title"] for i in page["items"]]
    assert titles.index("Zz Northern Loop") < titles.index("Aa Delta Days")  # sellable first, despite A-Z
    plan = next(i for i in page["items"] if i["source_id"] == PLAN)
    assert plan["destinations"] == ["Hanoi", "Ha Giang", "Dong Van"]  # first appearance, trimmed, no blanks
    assert plan["cover_photo_path"] == "/public/photos/first"  # no CIS cover: the first tour photo
    other = next(i for i in page["items"] if i["source_id"] == OTHER)
    assert other["destinations"] == [] and other["cover_photo_path"] is None and other["supplier"] is None

    by_title = await c.get("/catalog/trip-plans", params={"country": "Vietnam", "sort": "title"}, headers=contracting)
    titles = [i["title"] for i in by_title.json()["items"]]
    assert titles.index("Aa Delta Days") < titles.index("Zz Northern Loop")
    not_first = await c.get(
        "/catalog/trip-plans", params={"country": "Vietnam", "sort": "-sellable"}, headers=contracting
    )
    titles = [i["title"] for i in not_first.json()["items"]]
    assert titles.index("Aa Delta Days") < titles.index("Zz Northern Loop")
    # The detail keeps the CIS cover field as it is (null here); only list rows fall back.
    detail = (await c.get(f"/catalog/trip-plans/{PLAN}", headers=contracting)).json()
    assert detail["cover_photo_path"] is None and detail["from_price"] is None
