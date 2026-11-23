"""AA-1151 (bk-068): add-on offers over HTTP on a real PostgreSQL, as role ``booking``.

Done-when of the issue: importing a file of 5 activities creates 5 drafts; a second person publishes; the place
add-on view returns only days with an in-force price; the child price follows B4; the 60-day contract warning shows
in admin. Also: who may write and publish (content creator, contracting, sales), the T7 sale rules in a B2B quote, and
the daily expiry job announcing each contract once.

Dates are relative to the database's UTC day (the view starts today). All names and places are invented."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal as D
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.settings import Settings
from app.supply import activity_import, contract_expiry
from tests.integration.conftest import MIGRATIONS
from tests.integration.supply import purge_supply
from tests.staff_tokens import CRM_CLIENT, M2M_ISSUER, LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN, MAKER, CHECKER, EDITOR, SALES = (
    "it-1151-admin",
    "it-1151-maker",
    "it-1151-checker",
    "it-1151-editor",
    "it-1151-sales",
)
POLICY = [p for p in MIGRATIONS if p.name[:3] in ("004", "006", "007")]
TENANT = "e2000000-0000-4000-8000-000000001151"
HANOI = "d0000000-0000-4000-8000-000000000001"
ENTITIES = ["supplier_contract", "addon_offer", "service", "supplier_rate", "b2b_partner"]
TITLES = ["Lotus lake paddle", "Lantern night walk", "Herb garden class", "Bamboo raft drift", "Old town cycle"]


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM booking.b2b_quote_lines")
    await conn.execute("DELETE FROM booking.b2b_quotes")
    await conn.execute("DELETE FROM booking.b2b_partners")
    await conn.execute("DELETE FROM booking.notifications WHERE type = $1", contract_expiry.NOTIFICATION_TYPE)
    await purge_supply(conn)
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type = ANY($1::text[])", ENTITIES)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    for path in POLICY:  # other modules empty pricing_rules; bk-007 approves the seed again
        await conn.execute(path.read_text(encoding="utf-8"))
    await _clean(conn)
    for staff_id, role in (
        (ADMIN, "admin"),
        (MAKER, "contracting"),
        (CHECKER, "admin"),
        (EDITOR, "content_creator"),
        (SALES, "sales"),
    ):
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


async def _today(conn: asyncpg.Connection) -> date:
    return await conn.fetchval("SELECT (now() AT TIME ZONE 'UTC')::date")


async def _supplier(admin: asyncpg.Connection) -> str:
    return await admin.fetchval(
        "INSERT INTO booking.suppliers (company_name, categories, country_code, status, created_by)"
        " VALUES ('Paddle & Lantern Trips', ARRAY['activity'], 'VN', 'active', $1) RETURNING id::text",
        MAKER,
    )


def _sheet(today: date, weekdays: str) -> bytes:
    contract = {
        "Title": "Invented excursions",
        "Valid from": today.isoformat(),
        "Valid to": (today + timedelta(days=40)).isoformat(),
        "Currency": "USD",
    }
    rows = []
    for i, title in enumerate(TITLES):
        values = {
            "Title": title,
            "Places": "Hoi An" if i == 4 else HANOI,  # an exact CIS place name works too
            "Unit": "person",
            "Duration minutes": 120,
            "Max group size": 10,
            "Inclusions": "Guide; Tea",
            "Min pax": 2,
            "Cut-off hours": 24,
            "Weekdays": weekdays,
            "Start times": "09:00",
            "Net adult": 40,
            "Net child": None,
            "Child age max": None,
            "Child %": 25 if i == 0 else None,
            "Season": None,
            "Valid from": None,
            "Valid to": None,
        }
        rows.append(tuple(values[c] for c in activity_import.COLUMNS))
    return activity_import.build_workbook(contract, rows)


def _upload(data: bytes) -> dict[str, str]:
    return {"data_base64": base64.b64encode(data).decode(), "filename": "activities.xlsx"}


async def test_done_when_import_five_publish_view_child_and_expiry(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M], admin: asyncpg.Connection
) -> None:
    c, cis, m2m = client
    maker, checker, editor, sales = (_staff(cis, s) for s in (MAKER, CHECKER, EDITOR, SALES))
    today = await _today(admin)
    run_day = today + timedelta(days=10)
    supplier = await _supplier(admin)
    data = _sheet(today, str(run_day.isoweekday()))

    # Import: dry run writes nothing; sales and content creators cannot import (supply.write).
    for who in (sales, editor):
        assert (
            await c.post(f"/suppliers/{supplier}/activity-sheets", json=_upload(data), headers=who)
        ).status_code == 403
    dry = await _ok(
        await c.post(f"/suppliers/{supplier}/activity-sheets?dry_run=true", json=_upload(data), headers=maker)
    )
    assert dry["dry_run"] is True and [a["title"] for a in dry["activities"]] == TITLES
    assert dry["activities"][4]["places"][0]["name"] == "Hoi An"
    assert await admin.fetchval("SELECT count(*) FROM booking.addon_offers") == 0

    # 5 rows -> 5 services, 5 draft add-ons, one draft contract with 5 adult rates.
    done = await _ok(await c.post(f"/suppliers/{supplier}/activity-sheets", json=_upload(data), headers=maker))
    assert done["imported"] == 5 and done["contract"]["status"] == "draft"
    drafts = await _ok(await c.get("/add-ons?status=draft", headers=sales))
    assert drafts["total"] == 5 and {o["service_title"] for o in drafts["items"]} == set(TITLES)
    assert all(o["missing"] == ["no_price"] for o in drafts["items"])  # the contract is a draft
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.supplier_rates WHERE supplier_contract_id = $1::uuid", done["contract"]["id"]
        )
        == 5
    )

    # The contract: the importer cannot publish it; a second person does.
    k = done["contract"]["id"]
    assert (await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=maker)).status_code == 403
    await _ok(await c.patch(f"/contracts/{k}", json={"status": "published"}, headers=checker))

    # The add-ons: the importer (a writer of every draft) cannot publish; content creators never publish; the checker
    # publishes all five.
    first = next(o for o in drafts["items"] if o["service_title"] == TITLES[0])
    detail = await _ok(await c.get(f"/add-ons/{first['id']}", headers=maker))
    assert detail["actions"]["publish"]["blockers"] == ["maker_checker"]
    own = await c.post(f"/add-ons/{first['id']}/publish", headers=maker)
    assert own.status_code == 403 and own.json()["detail"] == "maker_checker"
    assert (await c.post(f"/add-ons/{first['id']}/publish", headers=editor)).status_code == 403
    mine = await _ok(await c.get("/add-ons?approvable=me", headers=checker))
    assert mine["total"] == 5
    for offer in drafts["items"]:
        published = await _ok(await c.post(f"/add-ons/{offer['id']}/publish", headers=checker))
        assert published["status"] == "published" and published["published_by"] == CHECKER
    assert (await _ok(await c.get("/add-ons?approvable=me", headers=checker)))["total"] == 0

    # B3: the place view lists only running days with an in-force price (contract and rates end on today + 40).
    days = await _ok(
        await c.get(
            f"/add-ons/place-days?destination_id={HANOI}&date_from={today}&date_to={today + timedelta(days=60)}",
            headers=sales,
        )
    )
    expected = [
        (today + timedelta(days=n)).isoformat()
        for n in range(41)
        if (today + timedelta(days=n)).isoweekday() == run_day.isoweekday()
    ]
    assert days["days_ahead"] == 365
    assert sorted({d["service_date"] for d in days["items"]}) == expected
    assert {d["service_title"] for d in days["items"]} == set(TITLES[:4])  # the fifth runs at the other place

    # A published offer is fixed; reopening stops it selling and the reopener cannot republish.
    locked = await c.patch(f"/add-ons/{first['id']}", json={"min_pax": 3}, headers=maker)
    assert locked.status_code == 409 and locked.json()["detail"] == "offer_published"
    await _ok(await c.patch(f"/add-ons/{first['id']}", json={"sort_order": 1, "featured": True}, headers=editor))

    # B4 + T7 in a B2B quote: 2 adults + 1 child on the running day; the offer's 25 % child price wins.
    partner = {"tenant_id": TENANT, "agency_name": "Invented Agency", "m2m_client_id": CRM_CLIENT}
    await _ok(await c.post("/b2b-partners", json=partner, headers=_staff(cis, ADMIN)), 201)
    sv = first["component_id"]
    auth = {"Authorization": f"Bearer {m2m.token()}", "X-AA-Tenant-Id": TENANT}

    def ask(ref: str, day: date, adult: int, child: int = 0) -> dict[str, Any]:
        lines = [{"line_ref": ref, "component_id": sv, "service_date": day.isoformat(),
                  "pax": {"adult": adult, "child": child}}]  # fmt: skip
        return {"proposal_ref": f"PRO-1151-{ref}", "proposal_version": 1, "currency": "USD", "lines": lines}

    quote = await _ok(await c.post("/b2b/v1/quotes", json=ask("ok", run_day, 2, 1), headers=auth), 201)
    line = await admin.fetchrow(
        "SELECT net_cost_usd, price_note FROM booking.b2b_quote_lines WHERE quote_id = $1::uuid", quote["quote_id"]
    )
    assert line["net_cost_usd"] == D("90.0000")  # 2 x 40 + 25 % of 40
    assert json.loads(line["price_note"])["child"] == {"basis": "offer_pct", "pct": "25"}
    for bad, reason in (
        (ask("weekday", run_day + timedelta(days=1), 2), "not_operating"),
        (ask("min", run_day, 1), "below_min_pax"),
    ):
        refused = await c.post("/b2b/v1/quotes", json=bad, headers=auth)
        assert refused.status_code == 422 and reason in refused.json()["message"], refused.text

    # The contract ends on today + 40: inside the 60-day warning (policy contract_expiry_alert_days).
    expiring = await _ok(await c.get("/contracts/expiring", headers=sales))
    assert expiring["window_days"] == 60
    assert [(x["id"], x["days_left"], x["announced"]) for x in expiring["items"]] == [(k, 40, False)]
    assert (await c.get("/contracts/expiring", headers=editor)).status_code == 403


async def test_content_creators_write_drafts_and_sales_reads(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M], admin: asyncpg.Connection
) -> None:
    c, cis, _ = client
    editor, sales, checker = _staff(cis, EDITOR), _staff(cis, SALES), _staff(cis, CHECKER)
    supplier = await _supplier(admin)
    service, code = await admin.fetchrow(
        "INSERT INTO booking.services (supplier_id, service_type, unit, title, created_by)"
        " VALUES ($1::uuid, 'activity', 'person', 'Silk village visit', $2) RETURNING id::text, code",
        supplier,
        MAKER,
    )
    body = {"service_id": code, "cutoff_hours": 12, "operating_weekdays": [6, 7],
            "places": [{"destination_id": HANOI}]}  # fmt: skip
    assert (await c.post("/add-ons", json=body, headers=sales)).status_code == 403
    made = await _ok(await c.post("/add-ons", json=body, headers=editor), 201)
    assert made["status"] == "draft" and made["writers"] == [EDITOR] and made["service_id"] == service
    assert made["places"][0]["name"] == "Hanoi"
    assert (await c.post("/add-ons", json=body, headers=editor)).status_code == 409  # one offer per service
    bad = await c.put(f"/add-ons/{made['id']}/places", json={"places": [{"destination_id": "0" * 36}]}, headers=editor)
    assert bad.status_code == 422
    changed = await _ok(await c.patch(f"/add-ons/{made['id']}", json={"child_pct": "40", "min_pax": 2}, headers=editor))
    assert changed["changed"] == ["child_pct", "min_pax"] and changed["child_pct"] == "40.00"
    # No price yet: the checker's publish is refused with the reason, nothing changes.
    blocked = await c.post(f"/add-ons/{made['id']}/publish", headers=checker)
    assert blocked.status_code == 409 and blocked.json()["detail"] == "no_price"
    retired = await _ok(await c.post(f"/add-ons/{made['id']}/retire", headers=editor))
    assert retired["status"] == "retired"
    reopened = await _ok(await c.post(f"/add-ons/{made['id']}/reopen", headers=editor))
    assert reopened["status"] == "draft" and reopened["writers"] == [EDITOR]
    options = await _ok(await c.get("/add-ons/services?q=silk", headers=editor))
    assert options["items"][0]["offer_id"] == made["id"] and "net_amount" not in options["items"][0]
    actions = [a["action"] for a in (await _ok(await c.get(f"/add-ons/{made['id']}", headers=sales)))["audit"]]
    assert actions[:2] == ["addon_offer.reopened", "addon_offer.retired"] and "addon_offer.created" in actions


async def test_the_expiry_job_announces_each_contract_once(
    admin: asyncpg.Connection, migrated_booking_dsn: str
) -> None:
    today = await _today(admin)
    supplier = await _supplier(admin)
    ids = []
    for days in (30, 90):  # inside and outside the 60-day window
        k = await admin.fetchval(
            "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, valid_to, currency, created_by)"
            " VALUES ($1::uuid, $2, $3, $4, 'USD', $5) RETURNING id::text",
            supplier,
            f"Invented {days}-day contract",
            today,
            today + timedelta(days=days),
            MAKER,
        )
        await admin.execute(
            "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
            " WHERE id = $1::uuid",
            k,
            CHECKER,
        )
        ids.append(k)
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        async with conn.transaction():
            dry = await contract_expiry.run_watch(conn, today=today, dry_run=True)
        assert dry["announced_count"] == 1 and dry["expiring_count"] == 1
        assert await admin.fetchval("SELECT count(*) FROM booking.notifications WHERE type = 'contract.expiring'") == 0
        async with conn.transaction():
            first = await contract_expiry.run_watch(conn, today=today)
        async with conn.transaction():
            again = await contract_expiry.run_watch(conn, today=today)
    finally:
        await conn.close()
    assert first["announced"][0]["contract_id"] == ids[0] and again["announced_count"] == 0
    row = await admin.fetchrow(
        "SELECT entity_id::text AS entity_id, title, data ->> 'days_left' AS days_left FROM booking.notifications"
        " WHERE type = 'contract.expiring'"
    )
    assert row["entity_id"] == ids[0] and row["days_left"] == "30" and "ends on" in row["title"]
