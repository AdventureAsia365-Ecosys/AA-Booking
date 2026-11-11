"""AA-1073 rest (bk-048) end to end as role ``booking`` on a real PostgreSQL (schema checks: ``test_bk048_schema.py``).

- the departure board: the month window, the view's seats and occupancy, the filters (status, at risk, country, trip
  plan) and the drill-down (seats by agency with the trip case);
- the ops sheet: meeting point and time (``departure.updated`` to the agency, ``GET /b2b/v1/departures`` 1.8 fields),
  the staff note never sent, staff assigned and removed; sales can, a content creator cannot;
- departure fees: proposed by one person, refused to the same person and to a role without ``b2b_desk.approve``,
  approved by another (``departure.fee_charged`` with the fee, the settlement draft bills it once as
  ``departure_fee``);
- price adjustments: proposed, approved by another person, applied to the agency's quote on that departure after the
  tier discount with the line's ``price_note``; an early-booking rule takes margin points off a quote far enough ahead.

Dates are relative to today; agency names are invented. Outbox rows of the bk-048 types are deleted afterwards (tests
of bk-022 / bk-046 / bk-047 re-run those files, which put back an older CHECK).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.agency_billing import drafts
from app.db import Database
from tests.integration.conftest import apply_migrations, as_booking_owner
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    ADMIN,
    DAY1,
    DAY1_DATE,
    MAKER,
    SALES,
    SOURCE_ID,
    TENANT,
    TODAY,
    _hold_body,
    _m2m,
    _ok,
    _plan_line,
    _quote,
    _staff,
    admin,
    client,
    plan,
)
from tests.staff_tokens import LocalCis, LocalM2M

FIXTURES = (admin, client, plan)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
DEPS = f"/catalog/trip-plans/{SOURCE_ID}/departures"
NEW_EVENTS = ["departure.updated", "departure.fee_charged"]
WRITER = "it-1073r-writer"


@pytest_asyncio.fixture
async def bk048(migrated_admin_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[None]:
    """Put bk-048 back first (older files re-run by other tests replace its CHECKs, trigger and grants); afterwards
    delete the outbox rows of its event types and the agency invoices the settlement run made."""
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(conn, only=[48])
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, 'content_creator', true, $1)"
            " ON CONFLICT (cis_user_id) DO NOTHING",
            WRITER,
        )
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        async with as_booking_owner(conn):
            await conn.execute("DELETE FROM booking.outbox WHERE event_type = ANY($1::text[])", NEW_EVENTS)
            await conn.execute(
                "DELETE FROM booking.agency_invoice_line WHERE invoice_id IN"
                " (SELECT id FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid)",
                TENANT,
            )
            await conn.execute("DELETE FROM booking.agency_invoice WHERE party_tenant_id = $1::uuid", TENANT)
            await conn.execute("UPDATE booking.pricing_rules SET early_days = NULL, early_discount_pts = NULL")
    finally:
        await conn.close()


async def _events(c: httpx.AsyncClient, m2m: LocalM2M) -> list[dict]:
    return (await _ok(await c.get("/b2b/v1/events", headers=_m2m(m2m))))["items"]


async def _book(c: httpx.AsyncClient, m2m: LocalM2M, plan_id: str, dep: str, ref: str, adult: int = 2) -> dict:
    quote = await _ok(await _quote(c, m2m, ref, [_plan_line("tour", plan_id, dep, adult=adult)]), 201)
    hold = await _ok(await c.post("/b2b/v1/holds", json=_hold_body(quote, ref), headers=_m2m(m2m, f"h-{ref}")), 201)
    confirm = {"price_hash": quote["price_hash"], "deposit_ref": f"pay_{ref}"}
    return await _ok(
        await c.post(f"/b2b/v1/holds/{hold['hold_id']}/confirm", json=confirm, headers=_m2m(m2m, f"c-{ref}")), 201
    )


async def test_board_ops_and_fees(bk048: None, client: Client, admin: asyncpg.Connection, plan: dict[str, str]) -> None:
    c, cis, m2m, db = client
    maker, sales, boss, writer = _staff(cis, MAKER), _staff(cis, SALES), _staff(cis, ADMIN), _staff(cis, WRITER)
    dep = await _ok(
        await c.post(DEPS, json={"start_date": DAY1, "capacity": 10, "min_to_operate": 6}, headers=maker), 201
    )
    other_day = (DAY1_DATE + timedelta(days=3)).isoformat()
    await _ok(
        await c.post(DEPS, json={"start_date": other_day, "capacity": 4, "status": "guaranteed"}, headers=maker), 201
    )
    booked = await _book(c, m2m, plan["plan"], dep["id"], "PRO-BOARD")

    # Board: the window of the two departures, seats from the view, filters.
    window = f"from={TODAY.isoformat()}&to={(DAY1_DATE + timedelta(days=10)).isoformat()}"
    assert (await c.get(f"/b2b-desk/departure-board?{window}", headers=writer)).status_code == 403
    page = await _ok(await c.get(f"/b2b-desk/departure-board?{window}", headers=sales))
    assert page["total"] == 2 and page["countries"] == ["Vietnam"]
    row = page["items"][0]
    assert (row["id"], row["sold"], row["free"], row["occupancy_pct"], row["agencies_count"]) == (
        dep["id"], 2, 8, "20.0", 1,
    )  # fmt: skip
    assert row["tour_title"] == "Hanoi 1D" and row["price"]["adjustment"] is None
    only = await _ok(await c.get(f"/b2b-desk/departure-board?{window}&status=guaranteed", headers=sales))
    assert [i["start_date"] for i in only["items"]] == [other_day]
    assert (await _ok(await c.get(f"/b2b-desk/departure-board?{window}&at_risk=true", headers=sales)))["total"] == 0
    assert (await _ok(await c.get(f"/b2b-desk/departure-board?{window}&country=Laos", headers=sales)))["total"] == 0
    by_plan = await _ok(await c.get(f"/b2b-desk/departure-board?{window}&source_id={SOURCE_ID}", headers=sales))
    assert by_plan["total"] == 2

    # Drill-down: the agency, its seats and its case.
    detail = await _ok(await c.get(f"/b2b-desk/departure-board/{dep['id']}", headers=sales))
    assert [(a["agency_name"], a["sold"], [x["code"] for x in a["cases"]]) for a in detail["agencies"]] == [
        ("Agency 973", 2, [booked["trip_case_code"]])
    ]
    assert detail["fee_suggestions"] == [{"reason": "min_shortfall", "partner_id": None, "seats": 4}]
    seen = {e["event_id"] for e in await _events(c, m2m)}

    # Ops sheet: meeting point and time reach the agency; the note does not; a content creator cannot write.
    body = {"meeting_point": "Old Quarter lantern gate", "meeting_time": "07:30", "ops_note": "Coach waits at gate B"}
    assert (await c.patch(f"/b2b-desk/departures/{dep['id']}/ops", json=body, headers=writer)).status_code == 403
    ops = await _ok(await c.patch(f"/b2b-desk/departures/{dep['id']}/ops", json=body, headers=sales))
    assert (ops["meeting_point"], ops["meeting_time"]) == ("Old Quarter lantern gate", "07:30:00")
    await _ok(await c.patch(f"/b2b-desk/departures/{dep['id']}/ops", json={"ops_note": "Changed"}, headers=sales))
    new = [e for e in await _events(c, m2m) if e["event_id"] not in seen]
    assert [(e["type"], e["departure_id"], e["fee"]) for e in new] == [("departure.updated", dep["id"], None)]
    seen |= {e["event_id"] for e in new}
    b2b = await _ok(
        await c.get(f"/b2b/v1/departures?source_system=cis&source_id={SOURCE_ID}&from={TODAY}", headers=_m2m(m2m))
    )
    mine = next(d for d in b2b["items"] if d["departure_id"] == dep["id"])
    assert (mine["meeting_point"], mine["meeting_time"]) == ("Old Quarter lantern gate", "07:30")
    assert "ops_note" not in mine
    leader = await _ok(
        await c.post(f"/b2b-desk/departures/{dep['id']}/staff", json={"role": "tour_leader", "staff_user_id": SALES},
                     headers=sales), 201
    )  # fmt: skip
    again = await c.post(
        f"/b2b-desk/departures/{dep['id']}/staff", json={"role": "tour_leader", "staff_user_id": SALES}, headers=sales
    )
    assert again.status_code == 409
    sheet = (await _ok(await c.get(f"/b2b-desk/departure-board/{dep['id']}", headers=sales)))["ops"]
    assert [(s["role"], s["staff"]["id"]) for s in sheet["staff"]] == [("tour_leader", SALES)]
    assert any(o["id"] == SALES for o in sheet["staff_options"])
    await _ok(await c.delete(f"/b2b-desk/departure-staff/{leader['id']}", headers=sales))

    # Fees: maker sales, a second person with b2b_desk.approve decides.
    fee_body = {"partner_id": plan["partner"], "reason": "min_shortfall", "seats": 4, "price_per_seat": "25.50",
                "currency": "USD"}  # fmt: skip
    fee = await _ok(await c.post(f"/b2b-desk/departures/{dep['id']}/fees", json=fee_body, headers=sales), 201)
    assert (fee["status"], fee["amount"]) == ("pending", "102.00")
    assert (await c.post(f"/b2b-desk/departure-fees/{fee['id']}/approve", headers=sales)).status_code == 403
    listed = await _ok(await c.get("/b2b-desk/departure-fees?status=pending", headers=boss))
    assert listed["pending"] == 1 and listed["items"][0]["can_decide"] is True
    approved = await _ok(await c.post(f"/b2b-desk/departure-fees/{fee['id']}/approve", headers=maker))
    assert approved["status"] == "approved" and approved["decided_by"]["id"] == MAKER
    assert (await c.post(f"/b2b-desk/departure-fees/{fee['id']}/reject", headers=boss)).status_code == 409
    new = [e for e in await _events(c, m2m) if e["event_id"] not in seen]
    assert [(e["type"], e["fee"]) for e in new] == [
        ("departure.fee_charged", {"fee_id": fee["id"], "amount": "102.00", "currency": "USD"})
    ]

    # The settlement draft bills it once on the agency's draft of the month.
    pool = await db.pool()
    first = await drafts.run_settlement(pool, period=TODAY.strftime("%Y-%m"), tenant_id=TENANT, actor="job:test")
    lines = await admin.fetch(
        "SELECT l.line_role, l.source_fee_id::text AS fee, l.quantity, l.unit_price, l.amount FROM"
        " booking.agency_invoice_line l JOIN booking.agency_invoice i ON i.id = l.invoice_id"
        " WHERE i.party_tenant_id = $1::uuid AND l.line_role = 'departure_fee'",
        TENANT,
    )
    assert first["lines_added"] >= 1
    assert [(r["fee"], r["quantity"], r["unit_price"], r["amount"]) for r in lines] == [
        (fee["id"], Decimal("4.00"), Decimal("25.50"), Decimal("102.00"))
    ]
    await drafts.run_settlement(pool, period=TODAY.strftime("%Y-%m"), tenant_id=TENANT, actor="job:test")
    count = await admin.fetchval(
        "SELECT count(*) FROM booking.agency_invoice_line WHERE source_fee_id = $1::uuid", fee["id"]
    )
    assert count == 1
    billed = (await _ok(await c.get(f"/b2b-desk/departure-fees?departure_id={dep['id']}", headers=boss)))["items"]
    assert billed[0]["billed"] is True


async def test_price_adjustment_and_early_booking_on_quotes(
    bk048: None, client: Client, admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    c, cis, m2m, _ = client
    maker, sales, boss = _staff(cis, MAKER), _staff(cis, SALES), _staff(cis, ADMIN)
    dep = await _ok(await c.post(DEPS, json={"start_date": DAY1, "capacity": 10}, headers=maker), 201)
    line = [_plan_line("tour", plan["plan"], dep["id"], adult=2)]
    before = await _ok(await _quote(c, m2m, "PRO-ADJ-0", line), 201)
    base = Decimal(before["lines"][0]["b2b_price"]["amount"])

    # Maker contracting proposes 10 % off; sales cannot (no pricing.write); the maker cannot approve; admin approves.
    body = {"adjust_pct": "-0.1", "reason": "Low season push"}
    assert (
        await c.post(f"/b2b-desk/departures/{dep['id']}/price-adjustments", json=body, headers=sales)
    ).status_code == 403
    adj = await _ok(await c.post(f"/b2b-desk/departures/{dep['id']}/price-adjustments", json=body, headers=maker), 201)
    assert adj["state"] == "pending"
    own = await c.post(f"/b2b-desk/price-adjustments/{adj['id']}/approve", headers=maker)
    assert own.status_code == 403 and own.json()["detail"] == "maker_checker"
    assert (await _ok(await c.post(f"/b2b-desk/price-adjustments/{adj['id']}/approve", headers=boss)))["state"] == (
        "in_force"
    )
    after = await _ok(await _quote(c, m2m, "PRO-ADJ-1", line), 201)
    assert Decimal(after["lines"][0]["b2b_price"]["amount"]) == (base * Decimal("0.9")).quantize(Decimal("0.01"))
    assert after["price_hash"] != before["price_hash"]
    note = await admin.fetchval(
        "SELECT l.price_note FROM booking.b2b_quote_lines l WHERE l.quote_id = $1::uuid", after["quote_id"]
    )
    # 10 % off the seed rule's price sells under its floor margin: still sold, the note says so.
    assert json.loads(note) == {"adjustment": {"id": adj["id"], "pct": "-0.1"}, "below_floor": True}
    board = await _ok(await c.get(f"/b2b-desk/departure-board/{dep['id']}", headers=sales))
    assert board["departure"]["price"]["adjustment"]["id"] == adj["id"]

    # Withdrawn: the next quote is the rule's price again. Then an early-booking rule (30 days: DAY1 is 30 days away).
    ended = await _ok(await c.post(f"/b2b-desk/price-adjustments/{adj['id']}/end", headers=maker))
    assert ended["end_reason"] == "withdrawn"
    plain = await _ok(await _quote(c, m2m, "PRO-ADJ-2", line), 201)
    assert Decimal(plain["lines"][0]["b2b_price"]["amount"]) == base
    async with as_booking_owner(admin):
        await admin.execute(
            "UPDATE booking.pricing_rules SET early_days = 30, early_discount_pts = 0.0200 WHERE channel = 'b2b'"
        )
    early = await _ok(await _quote(c, m2m, "PRO-ADJ-3", line), 201)
    assert Decimal(early["lines"][0]["b2b_price"]["amount"]) < base
    note = await admin.fetchval(
        "SELECT l.price_note FROM booking.b2b_quote_lines l WHERE l.quote_id = $1::uuid", early["quote_id"]
    )
    assert json.loads(note)["early_booking"] == {"days": 30, "pts": "0.02"}
    board = await _ok(await c.get(f"/b2b-desk/departure-board/{dep['id']}", headers=sales))
    assert board["departure"]["price"]["early_booking"]["days"] == 30
