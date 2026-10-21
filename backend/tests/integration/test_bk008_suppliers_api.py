"""AA-822 suppliers and supplier contracts over HTTP on a real PostgreSQL (bk-008), as role ``booking``.

RBAC (contracting/admin write, sales read), the CIS place check against ``catalog.v_destination`` (stub), the
duplicate guard, draft -> published (second person) -> expired, the deactivate rule, and one audit row per write.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import WORKFORCE_ISSUER, LocalStaff

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER, CHECKER, SALES = "it-822-api-maker", "it-822-api-checker", "it-822-api-sales"
ROLES = {MAKER: "contracting", CHECKER: "admin", SALES: "sales"}
HANOI = "d0000000-0000-4000-8000-000000000001"
LUANG_PRABANG = "d0000000-0000-4000-8000-000000000002"
ATLANTIS = "d0000000-0000-4000-8000-000000000004"  # a catalog place whose country is not in booking.countries
NOWHERE = "d0000000-0000-4000-8000-0000000000ff"


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type IN ('supplier', 'supplier_contract')")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _clean(conn)
    for staff_id, role in ROLES.items():
        # Linked to the local WORKFORCE identity whose sub is the row key (AA-911: WORKFORCE is the only sign-in).
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name, cognito_iss, cognito_sub)"
            " VALUES ($1, $2, true, $1, $3, $1)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true,"
            " cognito_iss = EXCLUDED.cognito_iss, cognito_sub = EXCLUDED.cognito_sub",
            staff_id,
            role,
            WORKFORCE_ISSUER,
        )
    try:
        yield conn
    finally:
        await _clean(conn)
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


async def test_supplier_crud_rbac_and_place_check(
    client: tuple[httpx.AsyncClient, LocalStaff], admin: asyncpg.Connection
) -> None:
    c, cis = client
    maker, checker, sales = _auth(cis, MAKER), _auth(cis, CHECKER), _auth(cis, SALES)
    body = {
        "company_name": "  Hanoi Emerald Hotel ",
        "categories": ["meal", "hotel", "hotel"],
        "contact_email": "Sales@Emerald.example",
        "website": "https://emerald.example",
        "destination_id": HANOI,
    }

    assert (await c.post("/suppliers", json=body, headers=sales)).status_code == 403
    created = await c.post("/suppliers", json=body, headers=maker)
    assert created.status_code == 201, created.text
    sup = created.json()
    assert sup["code"].startswith("SUP-") and sup["company_name"] == "Hanoi Emerald Hotel"
    assert sup["categories"] == ["hotel", "meal"]  # ordered, de-duplicated
    assert sup["contact_email"] == "sales@emerald.example"
    assert sup["country_code"] == "VN" and sup["country_name"] == "Vietnam"  # derived from the CIS place
    assert sup["destination"] == {"id": HANOI, "name": "Hanoi", "country": "Vietnam"}
    assert sup["status"] == "active" and sup["contract_counts"]["total"] == 0

    dup = await c.post("/suppliers", json={**body, "company_name": "hanoi emerald hotel"}, headers=checker)
    assert dup.status_code == 409 and dup.json()["detail"] == "supplier_exists"
    unknown = await c.post("/suppliers", json={**body, "company_name": "X", "destination_id": NOWHERE}, headers=maker)
    assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown_destination"
    mismatch = await c.post("/suppliers", json={**body, "company_name": "Y", "country_code": "LA"}, headers=maker)
    assert mismatch.status_code == 422 and mismatch.json()["detail"] == "destination_country_mismatch"
    bad_country = await c.post(
        "/suppliers", json={"company_name": "Z", "categories": ["guide"], "country_code": "ZZ"}, headers=maker
    )
    assert bad_country.status_code == 422 and bad_country.json()["detail"] == "unknown_country"
    bad_category = await c.post("/suppliers", json={"company_name": "Z", "categories": ["spa"]}, headers=maker)
    assert bad_category.status_code == 422
    # A place whose country Booking does not list: accepted, country stays empty.
    other = await c.post(
        "/suppliers",
        json={"company_name": "Lost Guides", "categories": ["guide"], "destination_id": ATLANTIS},
        headers=maker,
    )
    assert other.status_code == 201 and other.json()["country_code"] is None
    laos = await c.post(
        "/suppliers",
        json={"company_name": "Mekong Boats", "categories": ["transport"], "country_code": "la"},
        headers=checker,
    )
    assert laos.status_code == 201 and laos.json()["country_code"] == "LA"

    by_code = await c.get(f"/suppliers/{sup['code'].lower()}", headers=sales)
    assert by_code.status_code == 200 and by_code.json()["id"] == sup["id"]
    assert (await c.get("/suppliers/SUP-999999", headers=sales)).status_code == 404

    patched = await c.patch(
        f"/suppliers/{sup['id']}",
        json={"contact_name": "Linh", "website": "", "destination_id": None, "country_code": "VN"},
        headers=maker,
    )
    assert patched.status_code == 200, patched.text
    assert sorted(patched.json()["changed"]) == ["contact_name", "destination_id", "website"]
    assert patched.json()["website"] is None and patched.json()["destination_id"] is None
    noop = await c.patch(f"/suppliers/{sup['id']}", json={"contact_name": " Linh "}, headers=maker)
    assert noop.status_code == 200 and noop.json()["changed"] == []
    assert (await c.patch(f"/suppliers/{sup['id']}", json={"code": "SUP-1"}, headers=maker)).status_code == 422
    assert (await c.patch(f"/suppliers/{sup['id']}", json={"contact_name": "x"}, headers=sales)).status_code == 403
    move = await c.patch(f"/suppliers/{sup['id']}", json={"destination_id": LUANG_PRABANG}, headers=maker)
    assert move.status_code == 422 and move.json()["detail"] == "destination_country_mismatch"

    listing = await c.get("/suppliers", headers=sales)
    assert listing.status_code == 200 and listing.json()["total"] == 3
    assert [s["company_name"] for s in listing.json()["items"]] == [
        "Hanoi Emerald Hotel",
        "Lost Guides",
        "Mekong Boats",
    ]
    assert (await c.get("/suppliers?category=transport", headers=sales)).json()["total"] == 1
    assert (await c.get("/suppliers?country=vn", headers=sales)).json()["total"] == 1
    assert (await c.get("/suppliers?q=emerald", headers=sales)).json()["total"] == 1
    page = (await c.get("/suppliers?sort=-company_name&limit=1&offset=1", headers=sales)).json()
    assert [s["company_name"] for s in page["items"]] == ["Lost Guides"] and page["total"] == 3
    assert (await c.get("/suppliers?sort=bogus", headers=sales)).status_code == 422
    summary = (await c.get("/suppliers/summary", headers=sales)).json()
    assert summary["total"] == 3 and summary["active"] == 3 and summary["countries"] == 2

    places = await c.get("/catalog/destinations?q=han", headers=sales)
    assert places.status_code == 200
    assert [p["name"] for p in places.json()["items"]] == ["Hanoi"]
    assert places.json()["items"][0]["country_code"] == "VN"
    assert len((await c.get("/catalog/destinations?country=VN", headers=sales)).json()["items"]) == 2

    actions = [
        r["action"]
        for r in await admin.fetch(
            "SELECT action FROM booking.audit_logs WHERE entity_type = 'supplier' AND entity_id = $1::uuid"
            " ORDER BY created_at",
            sup["id"],
        )
    ]
    assert actions == ["supplier.created", "supplier.updated"]


async def test_contract_lifecycle_and_deactivate_rule(
    client: tuple[httpx.AsyncClient, LocalStaff], admin: asyncpg.Connection
) -> None:
    c, cis = client
    maker, checker, sales = _auth(cis, MAKER), _auth(cis, CHECKER), _auth(cis, SALES)
    today = datetime.now(UTC).date()
    sup = (
        await c.post("/suppliers", json={"company_name": "Paro Lodge", "categories": ["hotel"]}, headers=maker)
    ).json()
    contract = {
        "title": "2026-27 rates",
        "valid_from": str(today - timedelta(days=10)),
        "valid_to": str(today + timedelta(days=20)),
        "currency": "btn",
        "payment_terms": "30% deposit, balance 30 days before arrival",
    }

    assert (await c.post(f"/suppliers/{sup['id']}/contracts", json=contract, headers=sales)).status_code == 403
    bad = await c.post(
        f"/suppliers/{sup['id']}/contracts",
        json={**contract, "valid_to": str(today - timedelta(days=30))},
        headers=maker,
    )
    assert bad.status_code == 422
    created = await c.post(f"/suppliers/{sup['id']}/contracts", json=contract, headers=maker)
    assert created.status_code == 201, created.text
    k = created.json()
    assert k["status"] == "draft" and k["phase"] == "draft" and k["currency"] == "BTN"
    assert k["created_by"] == MAKER and k["created_by_name"] == "Staff One"  # refreshed from the token

    edited = await c.patch(f"/contracts/{k['id']}", json={"cancellation_policy": "Free until 30 days"}, headers=maker)
    assert edited.status_code == 200 and edited.json()["changed"] == ["cancellation_policy"]
    both = await c.patch(f"/contracts/{k['id']}", json={"status": "published", "notes": "x"}, headers=checker)
    assert both.status_code == 422 and both.json()["detail"] == "status_with_changes"
    early = await c.patch(f"/contracts/{k['id']}", json={"status": "expired"}, headers=checker)
    assert early.status_code == 409 and early.json()["detail"] == "contract_not_published"
    own = await c.patch(f"/contracts/{k['id']}", json={"status": "published"}, headers=maker)
    assert own.status_code == 403 and own.json()["detail"] == "maker_checker"
    published = await c.patch(f"/contracts/{k['id']}", json={"status": "published"}, headers=checker)
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published" and published.json()["phase"] == "in_force"
    assert published.json()["published_by"] == CHECKER
    locked = await c.patch(f"/contracts/{k['id']}", json={"payment_terms": "60 days"}, headers=checker)
    assert locked.status_code == 409 and locked.json()["detail"] == "contract_locked"

    detail = (await c.get(f"/suppliers/{sup['id']}", headers=sales)).json()
    assert detail["contracts_in_force"] == 1 and detail["contract_counts"] == {"total": 1, "drafts": 0, "in_force": 1}
    assert detail["recent_activity"][0]["action"] == "supplier_contract.published"
    assert (await c.get(f"/suppliers/{sup['code']}/contracts", headers=sales)).json()["items"][0]["id"] == k["id"]
    assert (await c.get(f"/contracts/{k['id']}", headers=sales)).status_code == 200
    assert (await c.get("/contracts/not-a-uuid", headers=sales)).status_code == 404

    blocked = await c.post(f"/suppliers/{sup['id']}/deactivate", json={"reason": "closed"}, headers=maker)
    assert blocked.status_code == 409 and blocked.json()["detail"] == "supplier_has_contracts"
    assert [x["id"] for x in blocked.json()["contracts"]] == [k["id"]]

    expired = await c.patch(f"/contracts/{k['id']}", json={"status": "expired"}, headers=maker)
    assert expired.status_code == 200 and expired.json()["phase"] == "expired"
    off = await c.post(f"/suppliers/{sup['id']}/deactivate", json={"reason": "closed"}, headers=maker)
    assert off.status_code == 200 and off.json()["status"] == "inactive"
    assert (await c.post(f"/suppliers/{sup['id']}/deactivate", headers=maker)).json()["changed"] == []
    no_new = await c.post(f"/suppliers/{sup['id']}/contracts", json=contract, headers=maker)
    assert no_new.status_code == 409 and no_new.json()["detail"] == "supplier_inactive"
    assert (await c.get("/suppliers?status=inactive", headers=sales)).json()["total"] == 1
    on = await c.post(f"/suppliers/{sup['id']}/activate", headers=checker)
    assert on.status_code == 200 and on.json()["status"] == "active"

    actions = [
        r["action"]
        for r in await admin.fetch(
            "SELECT action FROM booking.audit_logs WHERE entity_type IN ('supplier', 'supplier_contract')"
            " ORDER BY created_at, action"
        )
    ]
    assert actions == [
        "supplier.created",
        "supplier_contract.created",
        "supplier_contract.updated",
        "supplier_contract.published",
        "supplier_contract.expired",
        "supplier.deactivated",
        "supplier.activated",
    ]
    reason = await admin.fetchval(
        "SELECT summary FROM booking.audit_logs WHERE action = 'supplier.deactivated' AND entity_id = $1::uuid",
        sup["id"],
    )
    assert reason == "Deactivated: closed"


async def test_creator_publishes_after_a_checker_edit(client: tuple[httpx.AsyncClient, LocalStaff]) -> None:
    """The maker is the last writer of the terms: when B corrects A's draft, B cannot publish it, A can."""
    c, cis = client
    maker, checker = _auth(cis, MAKER), _auth(cis, CHECKER)
    sup = (
        await c.post("/suppliers", json={"company_name": "Gobi Jeeps", "categories": ["transport"]}, headers=maker)
    ).json()
    body = {"title": "Next season", "valid_from": str(datetime.now(UTC).date() + timedelta(days=60)), "currency": "MNT"}
    k = (await c.post(f"/suppliers/{sup['id']}/contracts", json=body, headers=maker)).json()
    fixed = await c.patch(f"/contracts/{k['id']}", json={"payment_terms": "50% on booking"}, headers=checker)
    assert fixed.status_code == 200 and fixed.json()["updated_by"] == CHECKER
    assert (await c.patch(f"/contracts/{k['id']}", json={"status": "published"}, headers=checker)).status_code == 403
    ok = await c.patch(f"/contracts/{k['id']}", json={"status": "published"}, headers=maker)
    assert ok.status_code == 200 and ok.json()["phase"] == "upcoming" and ok.json()["published_by"] == MAKER
    blocked = await c.post(f"/suppliers/{sup['id']}/deactivate", headers=maker)  # an upcoming contract blocks too
    assert blocked.status_code == 409 and blocked.json()["detail"] == "supplier_has_contracts"


async def test_clearing_the_country_keeps_the_place_country(
    client: tuple[httpx.AsyncClient, LocalStaff], admin: asyncpg.Connection
) -> None:
    """With a CIS place set, the country follows the place: clearing it is a no-op, not a NULL next to the place."""
    c, cis = client
    maker = _auth(cis, MAKER)
    body = {"company_name": "Old Quarter Guides", "categories": ["guide"], "destination_id": HANOI}
    sup = (await c.post("/suppliers", json=body, headers=maker)).json()
    assert sup["country_code"] == "VN"
    cleared = await c.patch(f"/suppliers/{sup['code']}", json={"country_code": None}, headers=maker)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["country_code"] == "VN" and cleared.json()["changed"] == []
    assert await admin.fetchval("SELECT country_code FROM booking.suppliers WHERE id = $1::uuid", sup["id"]) == "VN"
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.audit_logs WHERE action = 'supplier.updated' AND entity_id = $1::uuid",
            sup["id"],
        )
        == 0
    )
