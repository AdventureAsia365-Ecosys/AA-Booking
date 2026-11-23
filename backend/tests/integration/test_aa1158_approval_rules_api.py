"""AA-1158 part 1 (bk-064) over HTTP on a real PostgreSQL, as role ``booking`` — the Done-when flows:

- a supplier made by the content + ops custom role stays ``pending``; its creator cannot activate it, nor can the tech
  lead after editing its pending data; the business owner activates it;
- the business owner creates and activates her own supplier: ``owner_self_approved = true`` (and the audit filter
  lists it);
- the tech lead (an administrator without the marker) approving his own supplier, contract or bill: 403;
- the bill approver recording the payout: 403, except the business owner, whose payout is flagged
  ``same_approver_payer``;
- a ``payment_instructions`` change stays pending until a second person approves it; invoices keep the old details;
- ``sellable = true`` on a plan without an approved price: 409 ``no_approved_price``.

The business owner and the tech lead sign in with WORKFORCE tokens (``booking-admin``); the CIS marker is the CI stub
of ``shared.v_staff_booking_grant``. All names, subs and codes are invented."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.agency_billing import config
from app.auth import custom_roles
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration import ops
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OWNER_SUB, LEAD_SUB, HOLDER_SUB = (f"c1158000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
OWNER, LEAD = f"cog:{OWNER_SUB}", f"cog:{LEAD_SUB}"
MAKER, FIN = "it-1158-api-maker", "it-1158-api-fin"
TOTP = ["pwd", "otp", "mfa"]
TOUR = "c1158000-0000-4000-8000-0000000000aa"
ENTITY = "IT1158API"


async def _purge(admin: asyncpg.Connection) -> None:
    await ops.purge_ops(admin)
    async with admin.transaction():
        await admin.execute("SET LOCAL ROLE bk_owner")
        await admin.execute("DELETE FROM booking.billing_entity WHERE code = $1", ENTITY)
        await admin.execute("DELETE FROM booking.supplier_contracts WHERE title LIKE 'it-1158 %'")
        await admin.execute("DELETE FROM booking.suppliers WHERE company_name LIKE 'it-1158 %'")
        await admin.execute("DELETE FROM booking.trip_plans WHERE source_id = $1", TOUR)
    await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = $1", TOUR)
    await admin.execute("DELETE FROM shared.staff_booking_grant_stub WHERE cognito_sub LIKE 'c1158000-%'")
    custom_roles.forget()


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge(conn)
    await ops.staff(conn, (MAKER, "contracting"), (FIN, "finance"))
    for sub, owner in ((OWNER_SUB, True), (LEAD_SUB, False)):
        await conn.execute(
            "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code, is_owner, is_business_owner)"
            " VALUES ($1, NULL, true, $2)",
            sub,
            owner,
        )
    await conn.execute(
        "INSERT INTO shared.staff_booking_grant_stub (cognito_sub, booking_role_code) VALUES ($1, $2)",
        HOLDER_SUB,
        custom_roles.SEED_CODE,
    )
    try:
        yield conn
    finally:
        await _purge(conn)
        await conn.close()


@pytest_asyncio.fixture
async def http(migrated_booking_dsn: str) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis]]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


def admin_of(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + cis.workforce.token(["booking-admin", "cis-admin"], sub=sub, amr=TOTP)}


def custom(cis: LocalCis) -> dict[str, str]:
    return {"Authorization": "Bearer " + cis.workforce.token(["cis-custom", "booking-custom"], sub=HOLDER_SUB)}


def bridge(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


async def _ok(r: httpx.Response, status: int = 200) -> Any:
    assert r.status_code == status, r.text
    return r.json()


def _refused(r: httpx.Response, detail: str = "maker_checker") -> None:
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == detail, r.text


async def _supplier(c: httpx.AsyncClient, headers: dict[str, str], name: str) -> dict[str, Any]:
    body = {"company_name": f"it-1158 {name} {uuid.uuid4().hex[:6]}", "categories": ["hotel"]}
    return await _ok(await c.post("/suppliers", json=body, headers=headers), 201)


# ── Suppliers and contracts ─────────────────────────────────────────────────


async def test_suppliers_start_pending_and_a_second_person_activates_them(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = http
    owner, lead = admin_of(cis, OWNER_SUB), admin_of(cis, LEAD_SUB)
    assert (await _ok(await c.get("/staff/me/self-approval", headers=owner))) == {
        "is_business_owner": True,
        "approve": True,
        "payout": True,
    }
    assert (await _ok(await c.get("/staff/me/self-approval", headers=lead)))["is_business_owner"] is False

    # The content + ops custom role: pending, and activating it is an approval it does not hold.
    made = await _supplier(c, custom(cis), "Lagoon Boats")
    assert made["status"] == "pending"
    _refused(await c.post(f"/suppliers/{made['id']}/activate", headers=custom(cis)), "forbidden")
    # Whoever changes the pending data is a maker too: the tech lead edits the contact, then may not activate it.
    patch = {"contact_email": "desk@lagoon.example"}
    edited = await _ok(await c.patch(f"/suppliers/{made['id']}", json=patch, headers=lead))
    assert edited["writers"][-1] == LEAD and len(edited["writers"]) == 2
    detail = await _ok(await c.get(f"/suppliers/{made['id']}", headers=lead))
    assert detail["actions"]["activate"]["blockers"] == ["maker_checker"]
    _refused(await c.post(f"/suppliers/{made['id']}/activate", headers=lead))
    on = await _ok(await c.post(f"/suppliers/{made['id']}/activate", headers=owner))
    assert on["status"] == "active" and on["activated_by"] == OWNER and on["owner_self_approved"] is False

    # The business owner's own supplier: activated by her, flagged; listed by the audit filter.
    own = await _supplier(c, owner, "Owner Lodge")
    assert own["actions"]["activate"] == {"allowed": True, "self_approval": True, "blockers": []}
    flagged = await _ok(await c.post(f"/suppliers/{own['id']}/activate", headers=owner))
    assert flagged["owner_self_approved"] is True
    assert await admin.fetchval("SELECT owner_self_approved FROM booking.suppliers WHERE id = $1::uuid", own["id"])
    audit = await _ok(await c.get("/audit-logs", params={"owner_self_approved": "true"}, headers=lead))
    assert own["id"] in {i["entity_id"] for i in audit["items"]}
    tiles = {t["key"]: t for t in (await _ok(await c.get("/my-work/summary", headers=lead)))["tiles"]}
    assert tiles["owner_self_approvals"]["count"] >= 1  # every audit.read holder sees it, not only the owner

    # The tech lead's own supplier: refused (API), and the row stays pending.
    mine = await _supplier(c, lead, "Lead Lodge")
    assert mine["actions"]["activate"]["blockers"] == ["maker_checker"]
    _refused(await c.post(f"/suppliers/{mine['id']}/activate", headers=lead))
    assert await admin.fetchval("SELECT status FROM booking.suppliers WHERE id = $1::uuid", mine["id"]) == "pending"

    # The tech lead's own contract draft: refused; the business owner publishes it.
    await _ok(await c.post(f"/suppliers/{mine['id']}/activate", headers=owner))
    body = {"title": "it-1158 rates", "valid_from": "2027-01-01", "currency": "USD"}
    k = await _ok(await c.post(f"/suppliers/{mine['id']}/contracts", json=body, headers=lead), 201)
    detail = await _ok(await c.get(f"/contracts/{k['id']}", headers=lead))
    assert detail["writers"] == [LEAD] and detail["actions"]["publish"]["blockers"] == ["maker_checker"]
    _refused(await c.patch(f"/contracts/{k['id']}", json={"status": "published"}, headers=lead))
    published = await _ok(await c.patch(f"/contracts/{k['id']}", json={"status": "published"}, headers=owner))
    assert published["published_by"] == OWNER and published["owner_self_approved"] is False


# ── Bills and payouts ───────────────────────────────────────────────────────


async def _approved_bill(admin: asyncpg.Connection, supplier: str, booking: str, n: int, *, by: str) -> str:
    made = await ops.bill(admin, supplier, by=by, amount="100.00", number=f"IT1158-{n}", lines=((booking, "100"),))
    return made["id"]


async def test_the_approver_pays_only_as_the_business_owner_flagged(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = http
    owner, lead = admin_of(cis, OWNER_SUB), admin_of(cis, LEAD_SUB)
    await c.get("/staff/me", headers=owner)  # provision the WORKFORCE rows
    await c.get("/staff/me", headers=lead)
    sup = await ops.supply(admin, "Kestrel")
    case = await ops.sold_case(admin, sup, tag="aa1158")
    bookings = [await ops.confirmed_booking(admin, case, sup["supplier"], net="100.00") for _ in range(3)]

    # The tech lead's own bill: refused.
    own = await _approved_bill(admin, sup["supplier"], bookings[0], 1, by=LEAD)
    _refused(await c.post(f"/supplier-bills/{own}/approve", headers=lead))

    # Approved by the tech lead: he never records its payout; the business owner may.
    b = await _approved_bill(admin, sup["supplier"], bookings[1], 2, by=MAKER)
    await _ok(await c.post(f"/supplier-bills/{b}/approve", headers=lead))
    pay = {"amount": "100.00", "method": "bank_transfer", "paid_on": "2026-11-20", "reference": "IT1158-P2"}
    _refused(await c.post(f"/supplier-bills/{b}/payouts", json=pay, headers={**lead, "Idempotency-Key": "it1158-p2a"}))

    # Approved by the business owner: she records the payout, flagged.
    b3 = await _approved_bill(admin, sup["supplier"], bookings[2], 3, by=MAKER)
    approved = await _ok(await c.post(f"/supplier-bills/{b3}/approve", headers=owner))
    assert approved["actions"]["pay_flagged"] is True
    pay3 = {**pay, "reference": "IT1158-P3"}
    out = await _ok(
        await c.post(f"/supplier-bills/{b3}/payouts", json=pay3, headers={**owner, "Idempotency-Key": "it1158-p3a"}),
        201,
    )
    assert await admin.fetchval(
        "SELECT same_approver_payer FROM booking.supplier_payouts WHERE id = $1::uuid", out["id"]
    )


# ── Billing entity and the sellable gate ────────────────────────────────────


async def test_payment_details_wait_for_a_second_person(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = http
    fin, owner = bridge(cis, FIN), admin_of(cis, OWNER_SUB)
    entity = await admin.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity, payment_instructions) VALUES ($1, 'Lantern Ltd',"
        " 'Old bank 0001') RETURNING id::text",
        ENTITY,
    )
    changed = await _ok(
        await c.patch(f"/billing-entities/{entity}", json={"payment_instructions": "New bank 0002"}, headers=fin)
    )
    assert changed["payment_instructions"] == "Old bank 0001" and changed["pending_change"]["status"] == "pending"
    stored = await admin.fetchval("SELECT payment_instructions FROM booking.billing_entity WHERE id = $1::uuid", entity)
    assert stored == "Old bank 0001"  # invoices still read the old text
    change = changed["pending_change"]["id"]
    _refused(await c.post(f"/billing-entity-changes/{change}/decision", json={"action": "approve"}, headers=fin))
    await _ok(await c.post(f"/billing-entity-changes/{change}/decision", json={"action": "approve"}, headers=owner))
    rows = [r for r in await config.list_entities(admin) if r["id"] == entity]
    assert rows[0]["payment_instructions"] == "New bank 0002" and rows[0]["pending_change"] is None


async def test_sellable_needs_an_approved_price(
    http: tuple[httpx.AsyncClient, LocalCis], admin: asyncpg.Connection
) -> None:
    c, cis = http
    maker = bridge(cis, MAKER)
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days) VALUES ($1, 'it-1158 Delta', 'Vietnam',"
        " 1)",
        TOUR,
    )
    url = f"/catalog/trip-plans/{TOUR}/commercial"
    refused = await c.put(url, json={"sellable": True}, headers=maker)
    assert refused.status_code == 409 and refused.json()["detail"] == "no_approved_price"
    await _ok(await c.put(url, json={"sellable": False}, headers=maker))
    again = await c.put(url, json={"sellable": True}, headers=maker)
    assert again.status_code == 409 and again.json()["detail"] == "no_approved_price"
    assert await admin.fetchval("SELECT sellable FROM booking.trip_plans WHERE source_id = $1", TOUR) is False
