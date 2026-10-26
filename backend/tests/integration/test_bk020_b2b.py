"""AA-973 (bk-020): departures, holds and confirm end to end as role ``booking`` on a real PostgreSQL (schema checks:
``test_bk020_schema.py``).

- staff departures (create, duplicate, capacity floor, cancel) → B2B ``GET /departures`` with held / sold counts;
- quote availability per line (available / on_request / sold_out) → hold (Idempotency-Key, one set per proposal
  version, price_changed, sold_out) → confirm → B2B trip case + approved version, no customer (memo seams 1, 2);
- Linear R: capacity 0 → ``sold_out``; confirm after expiry → refused (contract: 410 ``hold_expired``);
- Linear E: two concurrent holds on the last seat → exactly one wins; a confirm that began before expiry and waited
  while another hold took the freed seat is refused (no oversell);
- the expiry job writes expired holds and frees nothing twice;
- AA-950: a quote leaves out the live hold of its own proposal version (the set it replaces), never anyone else's.

Dates are relative to today (lessons.md S245: no hard-coded future dates): departures start in 30 / 37 days.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.b2b import expiry
from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration.conftest import MIGRATIONS
from tests.integration.supply import purge_supply
from tests.staff_tokens import CRM_CLIENT, M2M_ISSUER, LocalCis, LocalM2M

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN, MAKER, CHECKER, SALES = "it-973-admin", "it-973-maker", "it-973-checker", "it-973-sales"
POLICY = [p for p in MIGRATIONS if p.name[:3] in ("004", "006", "007")]
SOURCE_ID = "e1000000-0000-4000-8000-000000000973"
TENANT = "e2000000-0000-4000-8000-000000000973"
TODAY = date.today()
DAY1_DATE = TODAY + timedelta(days=30)  # > 48 h away: a hold gets its full 48 h
DAY1, DAY2 = DAY1_DATE.isoformat(), (DAY1_DATE + timedelta(days=7)).isoformat()
FROM = TODAY.isoformat()
VALID_FROM, VALID_TO = TODAY - timedelta(days=30), TODAY + timedelta(days=365)
PUBLISH = (
    "UPDATE booking.supplier_contracts SET status = 'published', published_by = $2, published_at = now()"
    " WHERE id = $1::uuid"
)
LOCKED = ("booking.version_item_costs", "booking.trip_case_version_itinerary", "booking.trip_case_versions")


async def _clean(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        owner = await conn.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
        if owner != await conn.fetchval("SELECT current_user"):
            await conn.execute(f'SET LOCAL ROLE "{owner}"')
        await conn.execute("DELETE FROM booking.b2b_cancellations")  # AA-974 (bk-021), references holds and cases
        await conn.execute("DELETE FROM booking.b2b_hold_lines")
        await conn.execute("DELETE FROM booking.b2b_holds")
        # AA-974: plan-specific cancellation policies reference trip plans (approvals cascade); the seed stays.
        await conn.execute("DELETE FROM booking.cancellation_policies WHERE trip_plan_id IS NOT NULL")
        for table in LOCKED:  # approved B2B versions are locked by trigger
            await conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        cases = "SELECT id FROM booking.trip_cases WHERE channel = 'b2b'"
        versions = f"SELECT id FROM booking.trip_case_versions WHERE trip_case_id IN ({cases})"  # noqa: S608
        items = f"SELECT id FROM booking.trip_case_version_itinerary WHERE trip_case_version_id IN ({versions})"  # noqa: S608
        await conn.execute(f"DELETE FROM booking.version_item_costs WHERE version_item_id IN ({items})")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.trip_case_version_itinerary WHERE id IN ({items})")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.trip_case_versions WHERE id IN ({versions})")  # noqa: S608
        for table in LOCKED:
            await conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
        await conn.execute("DELETE FROM booking.trip_cases WHERE channel = 'b2b'")
        await conn.execute("DELETE FROM booking.b2b_quote_lines")
        await conn.execute("DELETE FROM booking.b2b_quotes")
        await conn.execute("DELETE FROM booking.departure_incidents")  # AA-975 (bk-022), references departures
        await conn.execute("DELETE FROM booking.departures")
        await conn.execute("DELETE FROM booking.b2b_partners")
    await conn.execute("DELETE FROM booking.trip_plan_components")
    await conn.execute("DELETE FROM booking.trip_plans WHERE source_id = $1", SOURCE_ID)
    await conn.execute("DELETE FROM catalog.v_tour WHERE source_id = $1", SOURCE_ID)
    await purge_supply(conn)
    await conn.execute("DELETE FROM booking.supplier_contracts")
    await conn.execute("DELETE FROM booking.suppliers")
    await conn.execute("DELETE FROM booking.fx_rates")
    await conn.execute("DELETE FROM booking.job WHERE kind = $1", expiry.KIND)


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
) -> AsyncIterator[tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]]:
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
            yield c, cis, m2m, db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def plan(
    admin: asyncpg.Connection, client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
) -> dict[str, str]:
    """A sellable plan (day 1: a room at 1,000,000 VND a night + a per-person transfer at 250,000 VND, 25,000 VND/USD)
    and an active partner bound to the CRM client."""
    c, cis, _, _ = client
    sid = await _insert(
        admin, "suppliers", company_name="Red River DMC", categories=["hotel", "transport"], country_code="VN",
        created_by=MAKER,
    )  # fmt: skip
    contract = await _insert(
        admin, "supplier_contracts", supplier_id=sid, title="Contract 973", valid_from=VALID_FROM,
        valid_to=VALID_TO, currency="VND", created_by=MAKER,
    )  # fmt: skip
    hotel = await _insert(admin, "hotels", supplier_id=sid, name="Hanoi Emerald", created_by=MAKER)
    room = await _insert(admin, "hotel_room_types", hotel_id=hotel, name="Deluxe", max_occupancy=2, created_by=MAKER)
    service = await _insert(
        admin, "services", supplier_id=sid, service_type="transfer", title="Airport pick-up", created_by=MAKER
    )
    common = {"supplier_contract_id": contract, "valid_from": VALID_FROM, "valid_to": VALID_TO}
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
        admin, "fx_rates", currency="VND", rate_per_usd=25_000, buffer_pct=0, as_of=TODAY - timedelta(days=1),
        source="synthetic", created_by=MAKER,
    )  # fmt: skip
    await admin.execute(
        "INSERT INTO booking.policy_approvals (entity_type, fx_rate_id, approved_by) VALUES ('fx_rate', $1::uuid, $2)",
        fx,
        CHECKER,
    )
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days) VALUES ($1, 'Hanoi 1D', 'Vietnam', 1)",
        SOURCE_ID,
    )
    maker = _staff(cis, MAKER)
    commercial = {"sellable": True}
    await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/commercial", json=commercial, headers=maker))
    items = {"items": [{"day_no": 1, "hotel_room_type_id": room}, {"day_no": 1, "service_id": service}]}
    saved = await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/components", json=items, headers=maker))
    partner = await _insert(admin, "b2b_partners", tenant_id=TENANT, agency_name="Agency 973", m2m_client_id=CRM_CLIENT)
    return {"plan": saved["trip_plan_id"], "room": room, "service": service, "partner": partner}


def _staff(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


def _m2m(m2m: LocalM2M, key: str | None = None, **kwargs: Any) -> dict[str, str]:
    h = {"Authorization": f"Bearer {m2m.token(**kwargs)}", "X-AA-Tenant-Id": TENANT}
    if key is not None:
        h["Idempotency-Key"] = key
    return h


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def _problem(response: httpx.Response, status: int, error: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == error, body
    return body


async def _departure(c: httpx.AsyncClient, cis: LocalCis, start: str, capacity: int, **extra: Any) -> dict[str, Any]:
    body = {"start_date": start, "capacity": capacity, **extra}
    return await _ok(
        await c.post(f"/catalog/trip-plans/{SOURCE_ID}/departures", json=body, headers=_staff(cis, MAKER)), 201
    )


def _plan_line(ref: str, plan_id: str, dep: str | None, start: str = DAY1, adult: int = 2) -> dict[str, Any]:
    line = {"line_ref": ref, "component_id": f"tp:{plan_id}", "service_date": start, "pax": {"adult": adult}}
    return {**line, "departure_id": dep} if dep else line


async def _quote(c: httpx.AsyncClient, m2m: LocalM2M, ref: str, lines: list[dict[str, Any]], version: int = 1) -> Any:
    body = {"proposal_ref": ref, "proposal_version": version, "currency": "USD", "lines": lines}
    return await c.post("/b2b/v1/quotes", json=body, headers=_m2m(m2m))


def _hold_body(quote: dict[str, Any], ref: str, version: int = 1) -> dict[str, Any]:
    return {
        "quote_id": quote["quote_id"],
        "price_hash": quote["price_hash"],
        "proposal_ref": ref,
        "proposal_version": version,
    }


async def _seats(c: httpx.AsyncClient, m2m: LocalM2M) -> dict[str, tuple[int, int, int]]:
    page = await _ok(
        await c.get(f"/b2b/v1/departures?source_system=cis&source_id={SOURCE_ID}&from={FROM}", headers=_m2m(m2m))
    )
    return {d["start_date"]: (d["held"], d["sold"], d["available"]) for d in page["items"]}


# ── Flow ─────────────────────────────────────────────────────────────────────


async def test_departures_quote_hold_confirm(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database], admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    c, cis, m2m, _ = client
    sales, maker, boss = _staff(cis, SALES), _staff(cis, MAKER), _staff(cis, ADMIN)

    # Staff departures: trip_plan.write only, one per date, listed with seats.
    body = {"start_date": DAY1, "capacity": 4}
    assert (await c.post(f"/catalog/trip-plans/{SOURCE_ID}/departures", json=body, headers=sales)).status_code == 403
    dep1 = await _departure(c, cis, DAY1, 4)
    dup = await c.post(f"/catalog/trip-plans/{SOURCE_ID}/departures", json=body, headers=maker)
    assert dup.status_code == 409
    started = await c.post(
        f"/catalog/trip-plans/{SOURCE_ID}/departures",
        json={"start_date": datetime.now(UTC).date().isoformat(), "capacity": 4},
        headers=maker,
    )
    assert started.status_code == 422 and "invalid_start_date" in started.text, started.text
    dep2 = await _departure(c, cis, DAY2, 10, auto_confirm=False)
    staff_list = await _ok(await c.get(f"/catalog/trip-plans/{SOURCE_ID}/departures", headers=sales))
    assert [(d["start_date"], d["available"]) for d in staff_list["items"]] == [(DAY1, 4), (DAY2, 10)]
    page = await _ok(
        await c.get(f"/b2b/v1/departures?source_system=cis&source_id={SOURCE_ID}&from={FROM}", headers=_m2m(m2m))
    )
    first = page["items"][0]
    assert first == {
        "departure_id": dep1["id"],
        "tour": {"source_system": "cis", "source_id": SOURCE_ID},
        "start_date": DAY1,
        "capacity": 4,
        "held": 0,
        "sold": 0,
        "available": 4,
        "status": "scheduled",
        "auto_confirm": True,
    }
    products = await _ok(await c.get("/b2b/v1/products", headers=_m2m(m2m, scope="booking.b2b/read")))
    assert products["items"][0]["has_fixed_departures"] is True

    # Quote availability: departure with seats + auto-confirm = available; no departure = on_request;
    # departure without auto-confirm = on_request; a departure on another date = 422.
    room = {"line_ref": "room", "component_id": f"rt:{plan['room']}", "service_date": DAY1, "pax": {"adult": 2}}
    lines = [_plan_line("tour", plan["plan"], dep1["id"]), room]
    quote = await _ok(await _quote(c, m2m, "PRO-1", lines), 201)
    assert [(ln["availability"], ln["departure_id"]) for ln in quote["lines"]] == [
        ("available", dep1["id"]),
        ("on_request", None),
    ]
    later = await _ok(await _quote(c, m2m, "PRO-9", [_plan_line("tour", plan["plan"], dep2["id"], DAY2)]), 201)
    assert later["lines"][0]["availability"] == "on_request"
    _problem(await _quote(c, m2m, "PRO-9", [_plan_line("tour", plan["plan"], dep1["id"], DAY2)]), 422, "invalid")
    room_dep = {**room, "departure_id": dep1["id"]}
    _problem(await _quote(c, m2m, "PRO-9", [room_dep]), 422, "invalid")

    # Hold: key required, scope hold, price_hash must be the quote's.
    hold_body = _hold_body(quote, "PRO-1")
    _problem(await c.post("/b2b/v1/holds", json=hold_body, headers=_m2m(m2m)), 422, "invalid")
    _problem(
        await c.post("/b2b/v1/holds", json=hold_body, headers=_m2m(m2m, "key-pro1-a", scope="booking.b2b/quote")),
        403,
        "forbidden",
    )
    wrong = {**hold_body, "price_hash": "0" * 64}
    changed = _problem(await c.post("/b2b/v1/holds", json=wrong, headers=_m2m(m2m, "key-pro1-x")), 409, "price_changed")
    assert changed["price_hash"] == quote["price_hash"]
    hold = await _ok(await c.post("/b2b/v1/holds", json=hold_body, headers=_m2m(m2m, "key-pro1-a")), 201)
    assert hold["status"] == "active" and hold["extended_count"] == 0 and hold["quote_id"] == quote["quote_id"]
    assert hold["lines"] == [
        {"line_ref": "tour", "line_status": "available"},
        {"line_ref": "room", "line_status": "on_request"},
    ]
    expires = datetime.fromisoformat(hold["expires_at"])
    assert timedelta(hours=47) < expires - datetime.now(UTC) <= timedelta(hours=48)

    # Idempotent: same key → same hold; another key for the same version and quote → same hold; another quote → 422;
    # the same key for another request → 422.
    replay = await _ok(await c.post("/b2b/v1/holds", json=hold_body, headers=_m2m(m2m, "key-pro1-a")), 201)
    same_version = await _ok(await c.post("/b2b/v1/holds", json=hold_body, headers=_m2m(m2m, "key-pro1-b")), 201)
    assert replay["hold_id"] == same_version["hold_id"] == hold["hold_id"]
    requote = await _ok(await _quote(c, m2m, "PRO-1", lines), 201)
    _problem(
        await c.post("/b2b/v1/holds", json=_hold_body(requote, "PRO-1"), headers=_m2m(m2m, "key-pro1-c")),
        422,
        "invalid",
    )
    _problem(
        await c.post("/b2b/v1/holds", json=_hold_body(later, "PRO-9"), headers=_m2m(m2m, "key-pro1-a")), 422, "invalid"
    )
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_holds") == 1
    assert (await _seats(c, m2m))[DAY1] == (2, 0, 2)

    # Capacity: 3 more on the departure does not fit (2 left) → quote says sold_out, hold refuses with sold_out.
    big = await _ok(await _quote(c, m2m, "PRO-2", [_plan_line("tour", plan["plan"], dep1["id"], adult=3)]), 201)
    assert big["lines"][0]["availability"] == "sold_out"
    _problem(
        await c.post("/b2b/v1/holds", json=_hold_body(big, "PRO-2"), headers=_m2m(m2m, "key-pro2-a")), 422, "sold_out"
    )

    # Confirm: book scope, hash, then the B2B trip case + approved version; replay gives the same booking.
    confirm = {"price_hash": quote["price_hash"], "deposit_ref": "pay_973"}
    url = f"/b2b/v1/holds/{hold['hold_id']}/confirm"
    _problem(
        await c.post(url, json=confirm, headers=_m2m(m2m, "conf-key-1", scope="booking.b2b/hold")), 403, "forbidden"
    )
    bad_hash = {**confirm, "price_hash": "1" * 64}
    _problem(await c.post(url, json=bad_hash, headers=_m2m(m2m, "conf-key-1")), 409, "price_changed")
    booking = await _ok(await c.post(url, json=confirm, headers=_m2m(m2m, "conf-key-1")), 201)
    assert booking["hold_id"] == hold["hold_id"] and booking["trip_case_code"].startswith("TC-")
    assert booking["lines"] == [
        {"line_ref": "tour", "status": "confirmed"},
        {"line_ref": "room", "status": "on_request"},
    ]
    again = await _ok(await c.post(url, json=confirm, headers=_m2m(m2m, "conf-key-2")), 201)
    assert again == booking
    got = await _ok(await c.get(f"/b2b/v1/holds/{hold['hold_id']}", headers=_m2m(m2m)))
    assert got["status"] == "confirmed" and got["lines"] == hold["lines"]
    assert (await _seats(c, m2m))[DAY1] == (0, 2, 2)
    _problem(await c.delete(f"/b2b/v1/holds/{hold['hold_id']}", headers=_m2m(m2m)), 422, "invalid")

    case = await admin.fetchrow(
        "SELECT id::text AS id, code, channel, customer_id, party_tenant_id::text AS party, source_system, source_id,"
        " status, start_date, end_date, pax, currency FROM booking.trip_cases WHERE id = $1::uuid",
        booking["booking_id"],
    )
    assert dict(case) | {"id": None} == {
        "id": None,
        "code": booking["trip_case_code"],
        "channel": "b2b",
        "customer_id": None,
        "party": TENANT,
        "source_system": "crm",
        "source_id": "PRO-1",
        "status": "confirmed",
        "start_date": DAY1_DATE,
        "end_date": DAY1_DATE,
        "pax": 2,
        "currency": "USD",
    }
    version = await admin.fetchrow(
        "SELECT id::text AS id, is_approved, channel, total_price, currency, fx_rate_locked, is_sent_to_customer"
        " FROM booking.trip_case_versions WHERE trip_case_id = $1::uuid",
        case["id"],
    )
    assert version["is_approved"] and version["channel"] == "b2b" and not version["is_sent_to_customer"]
    assert version["total_price"] == D(quote["total_b2b_price"]["amount"]) and version["fx_rate_locked"] == 1
    costs = await admin.fetch(
        "SELECT i.position, i.hotel_room_type_id::text AS room, c.net_amount, c.net_currency"
        " FROM booking.trip_case_version_itinerary i JOIN booking.version_item_costs c ON c.version_item_id = i.id"
        " WHERE i.trip_case_version_id = $1::uuid ORDER BY i.position",
        version["id"],
    )
    # plan: 1 room night for 2 (40 USD) + transfer x 2 (20 USD) = 60; room line: 1 night = 40.
    assert [(r["position"], r["room"], r["net_amount"], r["net_currency"]) for r in costs] == [
        (1, None, D("60.00"), "USD"),
        (2, plan["room"], D("40.00"), "USD"),
    ]
    history = await admin.fetch(
        "SELECT from_status, to_status, changed_by, note FROM booking.trip_case_status_history"
        " WHERE trip_case_id = $1::uuid",
        case["id"],
    )
    note = f"B2B confirm by m2m:{CRM_CLIENT} of hold {hold['hold_id']}"
    assert [tuple(r) for r in history] == [(None, "confirmed", None, note)]

    # Staff see the B2B case (no customer) in the list and the detail.
    listed = await _ok(await c.get("/trip-cases?source=crm", headers=boss))
    assert [(i["code"], i["channel"], i["customer_name"]) for i in listed["items"]] == [(case["code"], "b2b", None)]
    detail = await _ok(await c.get(f"/trip-cases/{case['code']}", headers=boss))
    assert detail["party_tenant_id"] == TENANT and detail["versions"][0]["channel"] == "b2b"
    # ... but do not move it: a staff cancel would leave the seats sold (cancel is the partner API, AA-974).
    cancel = {"to_status": "cancelled", "reason": "agency asked by phone"}
    refused = await c.post(f"/trip-cases/{case['code']}/status", json=cancel, headers=boss)
    assert refused.status_code == 409 and refused.json()["detail"] == "b2b_case_managed_by_partner_api", refused.text
    assert await admin.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case["id"]) == "confirmed"

    # Staff: capacity never below held + sold; cancel is final; a cancelled departure is sold out.
    patch = f"/catalog/departures/{dep1['id']}"
    assert (await c.patch(patch, json={"capacity": 1}, headers=maker)).status_code == 409
    cancelled = await _ok(await c.patch(patch, json={"status": "cancelled"}, headers=maker))
    assert cancelled["status"] == "cancelled" and cancelled["available"] == 0
    assert (await c.patch(patch, json={"status": "scheduled"}, headers=maker)).status_code == 409
    after = await _ok(await _quote(c, m2m, "PRO-3", [_plan_line("tour", plan["plan"], dep1["id"], adult=1)]), 201)
    assert after["lines"][0]["availability"] == "sold_out"
    actions = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_type IN ('departure', 'b2b_hold')"
        " AND entity_id = ANY($1::uuid[]) ORDER BY created_at",
        [dep1["id"], dep2["id"], hold["hold_id"]],
    )
    assert [a["action"] for a in actions] == [
        "departure.created",
        "departure.created",
        "b2b_hold.placed",
        "b2b_hold.confirmed",
        "departure.updated",
    ]


async def test_capacity_zero_release_and_expiry(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database], admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    c, cis, m2m, db = client
    # Linear R: capacity 0 → sold_out (quote shows it, hold refuses it).
    empty = await _departure(c, cis, DAY2, 0)
    q0 = await _ok(await _quote(c, m2m, "PRO-0", [_plan_line("tour", plan["plan"], empty["id"], DAY2)]), 201)
    assert q0["lines"][0]["availability"] == "sold_out"
    _problem(
        await c.post("/b2b/v1/holds", json=_hold_body(q0, "PRO-0"), headers=_m2m(m2m, "key-zero-1")), 422, "sold_out"
    )

    dep = await _departure(c, cis, DAY1, 2)
    quote = await _ok(await _quote(c, m2m, "PRO-5", [_plan_line("tour", plan["plan"], dep["id"])]), 201)
    hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(quote, "PRO-5"), headers=_m2m(m2m, "key-rel-1")), 201
    )
    assert (await _seats(c, m2m))[DAY1] == (2, 0, 0)

    # Release frees the seats, twice is a no-op; a released version can be held again with a new key.
    assert (await c.delete(f"/b2b/v1/holds/{hold['hold_id']}", headers=_m2m(m2m))).status_code == 204
    assert (await c.delete(f"/b2b/v1/holds/{hold['hold_id']}", headers=_m2m(m2m))).status_code == 204
    assert (await _seats(c, m2m))[DAY1] == (0, 0, 2)
    rehold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(quote, "PRO-5"), headers=_m2m(m2m, "key-rel-2")), 201
    )
    assert rehold["hold_id"] != hold["hold_id"]
    # Another partner's caller never sees the hold.
    _problem(await c.get(f"/b2b/v1/holds/{uuid.uuid4()}", headers=_m2m(m2m)), 404, "not_found")

    # Expiry: past expires_at the hold reads expired and frees its seats at once, before the job runs.
    await admin.execute(
        "UPDATE booking.b2b_holds SET created_at = now() - interval '3 days', expires_at = now() - interval '1 minute'"
        " WHERE id = $1::uuid",
        rehold["hold_id"],
    )
    got = await _ok(await c.get(f"/b2b/v1/holds/{rehold['hold_id']}", headers=_m2m(m2m)))
    assert got["status"] == "expired"
    assert (await _seats(c, m2m))[DAY1] == (0, 0, 2)
    confirm = {"price_hash": quote["price_hash"]}
    _problem(
        await c.post(f"/b2b/v1/holds/{rehold['hold_id']}/confirm", json=confirm, headers=_m2m(m2m, "conf-exp-1")),
        410,
        "hold_expired",
    )
    # The schedule hook queues one run only while something is due; the handler writes the status.
    pool = await db.pool()
    minute = datetime.now(UTC).replace(second=0, microsecond=0)
    hook = expiry.ExpirySchedule(now=lambda: minute)
    job_id = await hook(pool)
    assert job_id is not None and await hook(pool) is None  # same minute: one job
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE kind = $1", expiry.KIND) == 1
    assert await expiry.expire_due(pool) == 1
    row = await admin.fetchrow(
        "SELECT status, expired_at FROM booking.b2b_holds WHERE id = $1::uuid", rehold["hold_id"]
    )
    assert row["status"] == "expired" and row["expired_at"] is not None
    assert await expiry.expire_due(pool) == 0
    later = expiry.ExpirySchedule(now=lambda: minute + timedelta(minutes=5))
    assert await later(pool) is None  # nothing due: no job row


async def test_a_quote_leaves_out_the_live_hold_of_its_own_proposal_version(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database], admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    """AA-950: a proposal version has one hold set, and a new quote for that version replaces it (the CRM releases the
    old set before it holds the new quote). The quote leaves the version's own live hold out of the seat count, so a
    client who holds a departure's last seats and changes another pick is not told those seats are sold out. Any
    other proposal (or version) still sees the departure full, and the hold itself still counts every hold."""
    c, cis, m2m, _ = client
    dep = await _departure(c, cis, DAY1, 2)
    line = [_plan_line("tour", plan["plan"], dep["id"])]
    first = await _ok(await _quote(c, m2m, "OWN-1", line), 201)
    assert first["lines"][0]["availability"] == "available"
    hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(first, "OWN-1"), headers=_m2m(m2m, "own-key-1")), 201
    )
    assert (await _seats(c, m2m))[DAY1] == (2, 0, 0)  # the departure is full

    # The same version quoted again (the client changed another pick): its own seats are not counted.
    again = await _ok(await _quote(c, m2m, "OWN-1", line), 201)
    assert again["lines"][0]["availability"] == "available"
    # Another proposal, and another version of the same proposal, still find it full.
    other = await _ok(await _quote(c, m2m, "OWN-2", line), 201)
    assert other["lines"][0]["availability"] == "sold_out"
    v2 = await _ok(await _quote(c, m2m, "OWN-1", line, version=2), 201)
    assert v2["lines"][0]["availability"] == "sold_out"

    # The hold counts every hold: the new quote is refused while the old set stands, then placed once it is released.
    _problem(
        await c.post("/b2b/v1/holds", json=_hold_body(again, "OWN-1"), headers=_m2m(m2m, "own-key-2")), 422, "invalid"
    )
    assert (await c.delete(f"/b2b/v1/holds/{hold['hold_id']}", headers=_m2m(m2m))).status_code == 204
    rehold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(again, "OWN-1"), headers=_m2m(m2m, "own-key-3")), 201
    )
    assert rehold["status"] == "active" and rehold["hold_id"] != hold["hold_id"]
    assert (await _seats(c, m2m))[DAY1] == (2, 0, 0)
    _problem(
        await c.post("/b2b/v1/holds", json=_hold_body(other, "OWN-2"), headers=_m2m(m2m, "own-key-4")), 422, "sold_out"
    )
    # An expired hold of the version is no longer its own live set: it frees its seats for everyone anyway.
    await admin.execute(
        "UPDATE booking.b2b_holds SET created_at = now() - interval '3 days', expires_at = now() - interval '1 minute'"
        " WHERE id = $1::uuid",
        rehold["hold_id"],
    )
    assert (await _ok(await _quote(c, m2m, "OWN-2", line), 201))["lines"][0]["availability"] == "available"


async def test_two_concurrent_holds_on_the_last_seat(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database], admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    """Linear E: both requests wait on the departure row (held here by another session), then race; exactly one wins."""
    c, cis, m2m, _ = client
    dep = await _departure(c, cis, DAY1, 1)
    quotes = [
        await _ok(await _quote(c, m2m, f"RACE-{n}", [_plan_line("tour", plan["plan"], dep["id"], adult=1)]), 201)
        for n in (1, 2)
    ]
    blocker = admin
    tx = blocker.transaction()
    await tx.start()
    await blocker.execute("SELECT 1 FROM booking.departures WHERE id = $1::uuid FOR UPDATE", dep["id"])
    tasks = [
        asyncio.create_task(
            c.post("/b2b/v1/holds", json=_hold_body(q, f"RACE-{n}"), headers=_m2m(m2m, f"race-key-{n}"))
        )
        for n, q in zip((1, 2), quotes, strict=True)
    ]
    await asyncio.sleep(0.5)
    waiting = await admin.fetchval("SELECT count(*) FROM pg_locks WHERE NOT granted")
    await tx.rollback()
    results = await asyncio.gather(*tasks)
    assert waiting == 2  # both holds were queued on the same departure lock
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 422]
    loser = next(r for r in results if r.status_code == 422)
    assert loser.json()["error"] == "sold_out"
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_holds WHERE status = 'active'") == 1
    assert (await _seats(c, m2m))[DAY1] == (1, 0, 0)


async def test_confirm_that_waited_past_expiry_does_not_oversell(
    client: tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database], admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    """Review S248: a confirm whose transaction began before ``expires_at`` waits (here: on the hold row, held by
    another session); meanwhile the hold expires and another proposal takes the freed last seat and commits. The
    confirm must then answer 410 ``hold_expired``, not turn the expired hold into a sale (sold + held > capacity)."""
    c, cis, m2m, _ = client
    dep = await _departure(c, cis, DAY1, 1)
    first, second = [
        await _ok(await _quote(c, m2m, ref, [_plan_line("tour", plan["plan"], dep["id"], adult=1)]), 201)
        for ref in ("LATE-1", "LATE-2")
    ]
    hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(first, "LATE-1"), headers=_m2m(m2m, "late-key-1")), 201
    )
    await admin.execute(
        "UPDATE booking.b2b_holds SET expires_at = now() + interval '2 seconds' WHERE id = $1::uuid", hold["hold_id"]
    )
    expires_at = await admin.fetchval("SELECT expires_at FROM booking.b2b_holds WHERE id = $1::uuid", hold["hold_id"])

    tx = admin.transaction()
    await tx.start()
    await admin.execute("SELECT 1 FROM booking.b2b_holds WHERE id = $1::uuid FOR UPDATE", hold["hold_id"])
    confirm = asyncio.create_task(
        c.post(
            f"/b2b/v1/holds/{hold['hold_id']}/confirm",
            json={"price_hash": first["price_hash"]},
            headers=_m2m(m2m, "late-conf-1"),
        )
    )
    try:
        await asyncio.sleep(0.3)
        assert datetime.now(UTC) < expires_at, "the confirm must start before the hold expires"
        waiting = await admin.fetchval("SELECT count(*) FROM pg_locks WHERE NOT granted")
        await asyncio.sleep(max((expires_at - datetime.now(UTC)).total_seconds(), 0) + 0.3)
        # The hold has expired: the other proposal takes the last seat and commits while the confirm still waits.
        other = await c.post("/b2b/v1/holds", json=_hold_body(second, "LATE-2"), headers=_m2m(m2m, "late-key-2"))
    finally:
        await tx.rollback()
    assert waiting == 1  # the confirm was queued on the hold row
    assert other.status_code == 201, other.text
    _problem(await confirm, 410, "hold_expired")
    held, sold, available = (await _seats(c, m2m))[DAY1]
    assert (held, sold, available) == (1, 0, 0) and held + sold <= 1
    assert await admin.fetchval("SELECT count(*) FROM booking.trip_cases WHERE channel = 'b2b'") == 0
