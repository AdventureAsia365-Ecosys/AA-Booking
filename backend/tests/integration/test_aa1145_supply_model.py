"""AA-1145 (bk-060): the supply model over HTTP on a real PostgreSQL, as role ``booking``.

Done-when of the issue: a test contract with a 50 % child price, tiered cancellation terms and a stop-sale on 24/12
prices correctly (the child pays 50 % of the adult net, the 24/12 line is on request), contracting sets a stop-sale
and sales gets 403. Also: terms and restrictions are draft-only (fixed once a second person publishes the contract),
the stop-sale review and end take a second person, B7 commissionable rates keep both source numbers, and the bk-060
CHECKs and guards refuse what the API refuses.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
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
from tests.staff_tokens import CRM_CLIENT, M2M_ISSUER, LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN, MAKER, CHECKER, SALES = "it-1145-admin", "it-1145-maker", "it-1145-checker", "it-1145-sales"
POLICY = [p for p in MIGRATIONS if p.name[:3] in ("004", "006", "007")]
TENANT = "e2000000-0000-4000-8000-000000001145"
ENTITIES = ["supplier_contract", "supply_restriction", "supplier_rate", "b2b_partner"]


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM booking.b2b_quote_lines")
    await conn.execute("DELETE FROM booking.b2b_quotes")
    await conn.execute("DELETE FROM booking.b2b_partners")
    await purge_supply(conn)
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type = ANY($1::text[])", ENTITIES)


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


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


@pytest_asyncio.fixture
async def draft(admin: asyncpg.Connection) -> dict[str, str]:
    """A draft USD contract of an invented activity supplier with one per-person boat trip and a hotel room."""
    sid = await _insert(
        admin, "suppliers", company_name="Lantern Bay Trips", categories=["activity", "hotel"], country_code="VN",
        created_by=MAKER,
    )  # fmt: skip
    contract = await _insert(
        admin, "supplier_contracts", supplier_id=sid, title="2027 activities", valid_from=date(2027, 1, 1),
        valid_to=date(2027, 12, 31), currency="USD", created_by=MAKER,
    )  # fmt: skip
    service = await _insert(
        admin, "services", supplier_id=sid, service_type="activity", unit="person", title="Lantern boat trip",
        created_by=MAKER,
    )  # fmt: skip
    hotel = await _insert(admin, "hotels", supplier_id=sid, name="Harbour Light Inn", created_by=MAKER)
    room = await _insert(admin, "hotel_room_types", hotel_id=hotel, name="Garden", max_occupancy=2, created_by=MAKER)
    return {"supplier": sid, "contract": contract, "service": service, "room": room}


TIERS = [{"min_days": 30, "fee_pct": "0"}, {"min_days": 15, "fee_pct": "50"}, {"min_days": 0, "fee_pct": "100"}]


async def test_done_when_child_50_pct_tiers_and_stop_sale_on_24_12(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M], admin: asyncpg.Connection, draft: dict[str, str]
) -> None:
    c, cis, m2m = client
    maker, checker, sales, boss = _staff(cis, MAKER), _staff(cis, CHECKER), _staff(cis, SALES), _staff(cis, ADMIN)
    k, sv = draft["contract"], f"sv:{draft['service']}"

    # Structured terms on the draft (supply.write; sales 403): tiered cancellation, deposit + balance, child 50 %.
    terms = {"cancellation_tiers": TIERS, "deposit_pct": "30", "deposit_due_days": 7, "balance_due_days_before": 21,
             "child_pct": "50"}  # fmt: skip
    assert (await c.put(f"/contracts/{k}/terms", json=terms, headers=sales)).status_code == 403
    bad = await c.put(f"/contracts/{k}/terms", json={**terms, "credit_days_after": 30}, headers=maker)
    assert bad.status_code == 422 and bad.json()["detail"] == "balance_or_credit", bad.text
    saved = await _ok(await c.put(f"/contracts/{k}/terms", json=terms, headers=maker))
    assert saved["terms"]["child_pct"] == "50.00" and saved["terms"]["deposit_pct"] == "30.00"
    assert [t["min_days"] for t in saved["terms"]["cancellation_tiers"]] == [30, 15, 0]

    # The adult rate (40 USD a person) and a free-sale term for the whole year (max 8 travellers a booking).
    rate = {"service_id": draft["service"], "net_amount": "40.00"}
    await _ok(await c.post(f"/contracts/{k}/rates", json=rate, headers=maker), 201)
    term = {"component_id": sv, "inventory_kind": "free_sale", "date_from": "2027-01-01", "date_to": "2027-12-31",
            "max_pax": 8}  # fmt: skip
    assert (await c.post(f"/contracts/{k}/restrictions", json=term, headers=sales)).status_code == 403
    await _ok(await c.post(f"/contracts/{k}/restrictions", json=term, headers=maker), 201)
    # A stop-sale needs a published contract.
    stop = {"component_id": sv, "date_from": "2027-12-24", "date_to": "2027-12-24", "note": "Supplier e-mail: closed"}
    early = await c.post(f"/contracts/{k}/stop-sales", json=stop, headers=maker)
    assert early.status_code == 409 and early.json()["detail"] == "contract_not_published"

    # The maker cannot publish its own terms; a second person does.
    mine = await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=maker)
    assert mine.status_code == 403 and mine.json()["detail"] == "maker_checker"
    await _ok(await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=checker))
    # Published: terms and contract restrictions are fixed (API 409, the DB guard too).
    assert (await c.put(f"/contracts/{k}/terms", json=terms, headers=maker)).status_code == 409
    assert (await c.post(f"/contracts/{k}/restrictions", json=term, headers=maker)).status_code == 409
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await admin.execute("UPDATE booking.supplier_contract_terms SET child_pct = 10")

    # Stop-sale on 24/12: sales 403, contracting sets it, in effect at once (pending).
    assert (await c.post(f"/contracts/{k}/stop-sales", json=stop, headers=sales)).status_code == 403
    ss = await _ok(await c.post(f"/contracts/{k}/stop-sales", json=stop, headers=maker), 201)
    assert ss["status"] == "pending" and ss["in_effect"] is True

    check = await _ok(
        await c.get(f"/supply/availability?component_id={sv}&date_from=2027-12-23&date_to=2027-12-25&pax=3",
                    headers=sales)
    )  # fmt: skip
    assert [(d["date"], d["status"], d["reason"]) for d in check["days"]] == [
        ("2027-12-23", "available", "free_sale"),
        ("2027-12-24", "on_request", "stop_sale"),
        ("2027-12-25", "available", "free_sale"),
    ]

    # The B2B quote: 2 adults + 1 child on 23/12 and on 24/12. Child = 50 % of 40 (contract), so 100 USD net a line.
    partner = {"tenant_id": TENANT, "agency_name": "Invented Agency", "m2m_client_id": CRM_CLIENT}
    await _ok(await c.post("/b2b-partners", json=partner, headers=boss), 201)
    lines = [
        {"line_ref": "d23", "component_id": sv, "service_date": "2027-12-23", "pax": {"adult": 2, "child": 1}},
        {"line_ref": "d24", "component_id": sv, "service_date": "2027-12-24", "pax": {"adult": 2, "child": 1}},
    ]
    request = {"proposal_ref": "PRO-1145", "proposal_version": 1, "currency": "USD", "lines": lines}
    auth = {"Authorization": f"Bearer {m2m.token()}", "X-AA-Tenant-Id": TENANT}
    quote = await _ok(await c.post("/b2b/v1/quotes", json=request, headers=auth), 201)
    by_ref = {line["line_ref"]: line for line in quote["lines"]}
    assert by_ref["d24"]["availability"] == "on_request"
    stored = {
        r["line_ref"]: r
        for r in await admin.fetch(
            "SELECT line_ref, net_cost_usd, price_note FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid",
            quote["quote_id"],
        )
    }
    assert stored["d23"]["net_cost_usd"] == D("100.0000") == stored["d24"]["net_cost_usd"]
    note24, note23 = json.loads(stored["d24"]["price_note"]), json.loads(stored["d23"]["price_note"])
    assert note24["supply"] == {"stop_sale": ["2027-12-24"]}
    assert note24["child"] == {"basis": "contract_pct", "pct": "50"} == note23["child"]
    assert "supply" not in note23

    # Review: the maker cannot approve its own stop-sale; the checker approves.
    own = await c.post(f"/supply/stop-sales/{ss['id']}/decision", json={"action": "approve"}, headers=maker)
    assert own.status_code == 403 and own.json()["detail"] == "maker_checker"
    approved = await _ok(await c.post(f"/supply/stop-sales/{ss['id']}/decision", json={"action": "approve"},
                                      headers=checker))  # fmt: skip
    assert approved["status"] == "approved" and approved["decided_by"] == CHECKER
    # Lifting it takes a second person: request (maker), the maker cannot end it, the checker does.
    await _ok(await c.post(f"/supply/stop-sales/{ss['id']}/end-request", json={"reason": "Supplier reopened"},
                           headers=maker))  # fmt: skip
    still = await _ok(await c.get(f"/supply/restrictions/{ss['id']}", headers=sales))
    assert still["in_effect"] is True
    assert (await c.post(f"/supply/stop-sales/{ss['id']}/end", headers=maker)).status_code == 403
    assert (await c.post(f"/supply/stop-sales/{ss['id']}/end", headers=sales)).status_code == 403
    ended = await _ok(await c.post(f"/supply/stop-sales/{ss['id']}/end", headers=checker))
    assert ended["in_effect"] is False and ended["ended_by"] == CHECKER
    after = await _ok(
        await c.get(f"/supply/availability?component_id={sv}&date_from=2027-12-24&date_to=2027-12-24", headers=sales)
    )
    assert after["days"][0]["reason"] == "free_sale"

    # A stop-sale is never deleted and keeps its facts (DB guard).
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await admin.execute("DELETE FROM booking.supply_restrictions WHERE id = $1::uuid", ss["id"])
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
        await admin.execute(
            "UPDATE booking.supply_restrictions SET date_to = '2027-12-31' WHERE id = $1::uuid", ss["id"]
        )
    actions = {
        r["action"]
        for r in await admin.fetch("SELECT action FROM booking.audit_logs WHERE entity_type = 'supply_restriction'")
    }
    assert {"supply_restriction.stop_sale_set", "supply_restriction.stop_sale_approved",
            "supply_restriction.stop_sale_ended"} <= actions  # fmt: skip


async def test_withdraw_reject_allotment_and_commissionable_rates(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M], admin: asyncpg.Connection, draft: dict[str, str]
) -> None:
    c, cis, _ = client
    maker, checker, sales = _staff(cis, MAKER), _staff(cis, CHECKER), _staff(cis, SALES)
    k, rt = draft["contract"], f"rt:{draft['room']}"

    # B7: a commissionable room rate keeps both source numbers; the net is derived (70 at 20 % = 56).
    room_rate = {"hotel_room_type_id": draft["room"], "published_amount": "70", "commission_pct": "20"}
    made = await _ok(await c.post(f"/contracts/{k}/rates", json=room_rate, headers=maker), 201)
    assert (made["net_amount"], made["published_amount"], made["commission_pct"]) == ("56.00", "70.00", "20.00")
    mismatch = await c.post(f"/contracts/{k}/rates", json={**room_rate, "net_amount": "50"}, headers=maker)
    assert mismatch.status_code == 422 and mismatch.json()["detail"] == "net_mismatch"
    # A child rate only on a per-person rate; an age band only on a child / infant rate.
    child_room = await c.post(f"/contracts/{k}/rates", json={**room_rate, "pax_type": "child"}, headers=maker)
    assert child_room.status_code == 422 and child_room.json()["detail"] == "pax_type_per_person"
    child = {"service_id": draft["service"], "net_amount": "15", "pax_type": "child", "age_min": 4, "age_max": 11}
    made_child = await _ok(await c.post(f"/contracts/{k}/rates", json=child, headers=maker), 201)
    assert (made_child["pax_type"], made_child["age_min"], made_child["age_max"]) == ("child", 4, 11)

    # Allotment: 2 rooms a night, release 7 days; numbers are checked by kind.
    bad = {"component_id": rt, "inventory_kind": "allotment", "date_from": "2027-03-01", "date_to": "2027-03-31"}
    refused = await c.post(f"/contracts/{k}/restrictions", json=bad, headers=maker)
    assert refused.status_code == 422 and refused.json()["detail"] == "allotment_numbers"
    allot = await _ok(
        await c.post(f"/contracts/{k}/restrictions", json={**bad, "units": 2, "release_days": 7}, headers=maker), 201
    )
    outside = await c.post(
        f"/contracts/{k}/restrictions", json={**bad, "date_to": "2028-01-31", "units": 2, "release_days": 7},
        headers=maker,
    )  # fmt: skip
    assert outside.status_code == 422 and outside.json()["detail"] == "restriction_outside_contract"
    await _ok(await c.patch(f"/supply/restrictions/{allot['id']}", json={"units": 3}, headers=maker))
    await _ok(await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=checker))
    listing = await _ok(await c.get(f"/supply/restrictions?contract_id={k}&active=true", headers=sales))
    assert [(r["inventory_kind"], r["units"], r["in_effect"]) for r in listing["items"]] == [("allotment", 3, True)]
    window = await _ok(
        await c.get(f"/supply/availability?component_id={rt}&date_from=2027-03-10&date_to=2027-03-10&units=3",
                    headers=sales)
    )  # fmt: skip
    assert window["days"][0]["reason"] == "allotment"
    full = await _ok(
        await c.get(f"/supply/availability?component_id={rt}&date_from=2027-03-10&date_to=2027-03-10&units=4",
                    headers=sales)
    )  # fmt: skip
    assert (full["days"][0]["status"], full["days"][0]["reason"]) == ("on_request", "allotment_full")

    # Withdraw (maker only, with a reason) and reject (second person, with a reason): no longer in effect.
    stop = {"component_id": rt, "date_from": "2027-03-10", "date_to": "2027-03-12", "note": "Pool works"}
    one = await _ok(await c.post(f"/contracts/{k}/stop-sales", json=stop, headers=maker), 201)
    other = await c.post(f"/supply/stop-sales/{one['id']}/decision", json={"action": "withdraw", "reason": "x"},
                         headers=checker)  # fmt: skip
    assert other.status_code == 403 and other.json()["detail"] == "not_maker"
    gone = await _ok(await c.post(f"/supply/stop-sales/{one['id']}/decision",
                                  json={"action": "withdraw", "reason": "Entered twice"}, headers=maker))  # fmt: skip
    assert gone["status"] == "withdrawn" and gone["in_effect"] is False
    two = await _ok(await c.post(f"/contracts/{k}/stop-sales", json=stop, headers=maker), 201)
    no_reason = await c.post(f"/supply/stop-sales/{two['id']}/decision", json={"action": "reject"}, headers=checker)
    assert no_reason.status_code == 422
    rejected = await _ok(await c.post(f"/supply/stop-sales/{two['id']}/decision",
                                      json={"action": "reject", "reason": "Supplier denied it"},
                                      headers=checker))  # fmt: skip
    assert rejected["status"] == "rejected" and rejected["in_effect"] is False
    reopened = await _ok(
        await c.get(f"/supply/availability?component_id={rt}&date_from=2027-03-11&date_to=2027-03-11", headers=sales)
    )
    assert reopened["days"][0]["reason"] == "allotment"
    # The DB keeps the people apart even past the API.
    with pytest.raises(asyncpg.CheckViolationError):
        await admin.execute(
            "INSERT INTO booking.supply_restrictions (supplier_contract_id, hotel_room_type_id, inventory_kind,"
            " date_from, date_to, note, status, created_by, decided_by, decided_at)"
            " VALUES ($1::uuid, $2::uuid, 'stop_sale', '2027-04-01', '2027-04-02', 'n', 'approved', $3, $3, now())",
            k,
            draft["room"],
            MAKER,
        )
