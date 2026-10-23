"""AA-972 (bk-019): the staff partner and component endpoints and the booking.b2b.v1 flow end to end as role
``booking`` on a real PostgreSQL (schema checks: ``test_bk019_schema.py``): products → quote priced from published
supplier rates with the approved b2b rule and a locked FX rate → stored rows → same hash for the same request →
suspended partner refused (Linear R: wrong client or scope → 403).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal as D
from pathlib import Path
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
from tests.staff_tokens import CRM_CLIENT, M2M_ISSUER, OTHER_M2M_CLIENT, LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN, MAKER, CHECKER, SALES = "it-972-admin", "it-972-maker", "it-972-checker", "it-972-sales"
MIGRATION = Path(__file__).resolve().parents[3] / "migrations" / "019_b2b_partners_quotes.sql"
POLICY = [p for p in MIGRATIONS if p.name[:3] in ("004", "006", "007")]
SOURCE_ID = "e1000000-0000-4000-8000-000000000972"
TENANT = "e2000000-0000-4000-8000-000000000972"
PUBLISH = (
    "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
    " WHERE id = $1::uuid"
)


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM booking.b2b_quote_lines")
    await conn.execute("DELETE FROM booking.b2b_quotes")
    await conn.execute("DELETE FROM booking.b2b_partners")
    await conn.execute("DELETE FROM booking.trip_plan_components")
    await conn.execute("DELETE FROM booking.trip_plans WHERE source_id = $1", SOURCE_ID)
    await conn.execute("DELETE FROM catalog.v_tour WHERE source_id = $1", SOURCE_ID)
    await purge_supply(conn)
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    await conn.execute("DELETE FROM booking.fx_rates")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type IN ('b2b_partner', 'trip_plan')")


async def _insert(conn: asyncpg.Connection, table: str, **cols: object) -> str:
    names = ", ".join(cols)
    marks = ", ".join(f"${i + 1}" for i in range(len(cols)))
    return await conn.fetchval(
        f"INSERT INTO booking.{table} ({names}) VALUES ({marks}) RETURNING id::text",  # noqa: S608 — test columns
        *cols.values(),
    )


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    for path in POLICY:  # other modules empty pricing_rules; bk-007 approves the seed again
        await conn.execute(path.read_text(encoding="utf-8"))
    await _clean(conn)
    for staff_id, role in ((ADMIN, "admin"), (MAKER, "contracting"), (CHECKER, "admin"), (SALES, "sales")):
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
async def supply(admin: asyncpg.Connection) -> dict[str, str]:
    """A published VND contract: a room at 1,000,000 VND a night and a per-person transfer at 250,000 VND."""
    sid = await _insert(
        admin, "suppliers", company_name="Red River DMC", categories=["hotel", "transport"], country_code="VN",
        created_by=MAKER,
    )  # fmt: skip
    contract = await _insert(
        admin, "supplier_contracts", supplier_id=sid, title="2027", valid_from=date(2027, 1, 1),
        valid_to=date(2027, 12, 31), currency="VND", created_by=MAKER,
    )  # fmt: skip
    hotel = await _insert(admin, "hotels", supplier_id=sid, name="Hanoi Emerald", created_by=MAKER)
    room = await _insert(admin, "hotel_room_types", hotel_id=hotel, name="Deluxe", max_occupancy=2, created_by=MAKER)
    service = await _insert(
        admin, "services", supplier_id=sid, service_type="transfer", title="Airport pick-up", created_by=MAKER
    )
    common = {"supplier_contract_id": contract, "valid_from": date(2027, 1, 1), "valid_to": date(2027, 12, 31)}
    await _insert(
        admin, "supplier_rates", **common, hotel_room_type_id=room, unit="room_night", net_amount=1_000_000,
        currency="VND", created_by=MAKER,
    )  # fmt: skip
    await _insert(
        admin, "supplier_rates", **common, service_id=service, unit="person", net_amount=250_000, currency="VND",
        created_by=MAKER,
    )  # fmt: skip
    await admin.execute(PUBLISH, contract, CHECKER)
    fx = await _insert(
        admin, "fx_rates", currency="VND", rate_per_usd=25_000, buffer_pct=0, as_of=date(2026, 1, 1),
        source="synthetic", created_by=MAKER,
    )  # fmt: skip
    await admin.execute(
        "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1::uuid, $2)",
        fx,
        CHECKER,
    )
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days) VALUES ($1, 'Hanoi 2D', 'Vietnam', 2)",
        SOURCE_ID,
    )
    return {"room": room, "service": service}


@pytest_asyncio.fixture
async def client(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis, LocalM2M]]:
    cis, m2m = LocalCis(), LocalM2M()
    settings = Settings(
        database_url=migrated_booking_dsn,
        db_ssl="disable",
        environment="test",
        git_sha="ci",
        cognito_m2m_issuer=M2M_ISSUER,
        cognito_m2m_crm_client_id=CRM_CLIENT,
    )
    db = Database(settings)
    app = create_app(settings, db, cis.verifier(), m2m_verifier=m2m.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis, m2m
    finally:
        await db.close()


def _staff(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


def _m2m(m2m: LocalM2M, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {m2m.token(**kwargs)}", "X-AA-Tenant-Id": TENANT}


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


# ── Flow ─────────────────────────────────────────────────────────────────────


async def test_partner_components_products_and_quote(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M], admin: asyncpg.Connection, supply: dict[str, str]
) -> None:
    c, cis, m2m = client
    boss, sales, maker = _staff(cis, ADMIN), _staff(cis, SALES), _staff(cis, MAKER)

    # Partner admin: admin only, unique tenant, audited.
    body = {"tenant_id": TENANT, "agency_name": "Agency One", "m2m_client_id": CRM_CLIENT}
    assert (await c.post("/b2b-partners", json=body, headers=sales)).status_code == 403
    partner = await _ok(await c.post("/b2b-partners", json=body, headers=boss), 201)
    assert partner["status"] == "active" and partner["billing_currency"] == "USD"
    assert (await c.post("/b2b-partners", json=body, headers=boss)).status_code == 409
    assert len((await _ok(await c.get("/b2b-partners", headers=boss)))["items"]) == 1

    # Plan commercial row + components (trip_plan.write).
    commercial = {"sellable": True, "price_hint": {"amount": "150.00", "currency": "USD", "basis": "per_person"}}
    await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/commercial", json=commercial, headers=maker))
    items = {
        "items": [
            {"day_no": 1, "hotel_room_type_id": supply["room"]},
            {"day_no": 1, "service_id": supply["service"]},
        ]
    }
    assert (await c.put(f"/catalog/trip-plans/{SOURCE_ID}/components", json=items, headers=sales)).status_code == 403
    saved = await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/components", json=items, headers=maker))
    assert {i["kind"] for i in saved["items"]} == {"hotel_room", "transfer"}
    plan_id = saved["trip_plan_id"]
    bad = {"items": [{"day_no": 1, "service_id": str(uuid.uuid4())}]}
    assert (await c.put(f"/catalog/trip-plans/{SOURCE_ID}/components", json=bad, headers=maker)).status_code == 422

    # Products (scope read).
    page = await _ok(await c.get("/b2b/v1/products", headers=_m2m(m2m, scope="booking.b2b/read")))
    [product] = page["items"]
    assert product["tour"] == {"source_system": "cis", "source_id": SOURCE_ID} and product["trip_plan_id"] == plan_id
    ids = {comp["component_id"] for comp in product["components"]}
    assert ids == {f"tp:{plan_id}", f"rt:{supply['room']}", f"sv:{supply['service']}"}

    # Quote: room 2 nights for 2 adults (1 room) + transfer x 2 pax + the whole plan for 2.
    lines = [
        {"line_ref": "room", "component_id": f"rt:{supply['room']}", "service_date": "2027-03-01", "nights": 2,
         "pax": {"adult": 2, "infant": 1}},
        {"line_ref": "car", "component_id": f"sv:{supply['service']}", "service_date": "2027-03-01",
         "pax": {"adult": 2}},
        {"line_ref": "plan", "component_id": f"tp:{plan_id}", "service_date": "2027-03-01", "pax": {"adult": 2}},
    ]  # fmt: skip
    request = {"proposal_ref": "PRO-1", "proposal_version": 1, "currency": "USD", "lines": lines}
    assert (
        await c.post("/b2b/v1/quotes", json=request, headers=_m2m(m2m, scope="booking.b2b/read"))
    ).status_code == 403
    quote = await _ok(await c.post("/b2b/v1/quotes", json=request, headers=_m2m(m2m)), 201)
    # room: 2,000,000 VND = 80 USD → /0.82 = 97.5609… → 97.57; car: 500,000 VND = 20 USD → 24.40;
    # plan: 1 night (40) + car (20) = 60 USD → 73.1707… → 73.18. Total 195.15.
    assert [line["b2b_price"]["amount"] for line in quote["lines"]] == ["97.57", "24.40", "73.18"]
    assert quote["total_b2b_price"] == {"amount": "195.15", "currency": "USD"}
    stored = await admin.fetchrow(
        "SELECT partner_id::text AS partner_id, m2m_client_id, total_b2b_price, price_hash, margin_applied"
        " FROM booking.b2b_quotes WHERE id = $1::uuid",
        quote["quote_id"],
    )
    assert stored["partner_id"] == partner["id"] and stored["m2m_client_id"] == CRM_CLIENT
    assert stored["total_b2b_price"] == D("195.15") and stored["price_hash"] == quote["price_hash"]
    assert stored["margin_applied"] == D("0.1500")
    nets = await admin.fetch(
        "SELECT line_ref, net_cost_usd, cardinality(supplier_rate_ids) AS rates FROM booking.b2b_quote_lines"
        " WHERE quote_id = $1::uuid ORDER BY line_no",
        quote["quote_id"],
    )
    assert [(r["line_ref"], r["net_cost_usd"], r["rates"]) for r in nets] == [
        ("room", D("80.0000"), 2),
        ("car", D("20.0000"), 1),
        ("plan", D("60.0000"), 2),
    ]

    # Same request → same hash, new quote id.
    again = await _ok(await c.post("/b2b/v1/quotes", json={**request, "proposal_version": 2}, headers=_m2m(m2m)), 201)
    assert again["price_hash"] == quote["price_hash"] and again["quote_id"] != quote["quote_id"]

    # FX and parity.
    rates = await _ok(await c.get("/b2b/v1/fx-rates?date=2026-10-23", headers=_m2m(m2m)))
    assert rates["rates"] == [
        {"currency": "VND", "rate_per_usd": "25000.00000000", "as_of": "2026-01-01", "source": "synthetic"}
    ]
    parity = await _ok(
        await c.get(f"/b2b/v1/price-parity?source_system=cis&source_id={SOURCE_ID}&currency=VND", headers=_m2m(m2m))
    )
    assert parity["from_price"] == {"amount": "3750000", "currency": "VND"}

    # Wrong client for the partner, then a suspended partner.
    other = _m2m(m2m, client_id=OTHER_M2M_CLIENT)
    assert (await c.get("/b2b/v1/products", headers=other)).status_code == 403
    await _ok(await c.patch(f"/b2b-partners/{partner['id']}", json={"status": "suspended"}, headers=boss))
    refused = await c.get("/b2b/v1/products", headers=_m2m(m2m))
    assert refused.status_code == 403 and refused.json()["error"] == "unknown_partner"
    actions = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_type = 'b2b_partner' ORDER BY created_at"
    )
    assert [a["action"] for a in actions] == ["b2b_partner.created", "b2b_partner.suspended"]


async def test_app_role_cannot_rewrite_a_quote(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M],
    admin: asyncpg.Connection,
    migrated_booking_dsn: str,
    supply: dict[str, str],
) -> None:
    c, cis, m2m = client
    body = {"tenant_id": TENANT, "agency_name": "Agency One", "m2m_client_id": CRM_CLIENT}
    await _ok(await c.post("/b2b-partners", json=body, headers=_staff(cis, ADMIN)), 201)
    line = {
        "line_ref": "car",
        "component_id": f"sv:{supply['service']}",
        "service_date": "2027-03-01",
        "pax": {"adult": 1},
    }
    request = {"proposal_ref": "PRO-9", "proposal_version": 1, "currency": "USD", "lines": [line]}
    quote = await _ok(await c.post("/b2b/v1/quotes", json=request, headers=_m2m(m2m)), 201)
    app = await asyncpg.connect(migrated_booking_dsn)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute(
                "UPDATE booking.b2b_quotes SET total_b2b_price = 0 WHERE id = $1::uuid", quote["quote_id"]
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("DELETE FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid", quote["quote_id"])
    finally:
        await app.close()
