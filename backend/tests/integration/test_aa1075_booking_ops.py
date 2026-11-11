"""AA-1075 (bk-047) end to end as role ``booking`` on a real PostgreSQL (schema checks: ``test_bk047_schema.py``).

- staff enter a booking for an agency: quote, hold and confirm in one call, the trip case ``entered_via = staff``,
  ``booking.created_by_aa`` before ``line.confirmed`` on the agency's feed; a price below the floor waits for a second
  person (never the one who entered it) and books on approval;
- the generated vouchers (agency and staff, JSON and HTML, no traveller name, no price) and the departure manifest;
- a partial cancellation through the agency API (dry run, then real: seats back on sale, the confirmation's pax,
  ``booking.line_reduced``); the staff cancel with a lower fee asked, approved by another person
  (``booking.fee_changed``, the profit view's revenue); a supplier cancel fee in the cost view;
- a tour's own deposit terms on ``GET /terms?source_id=`` and a child price from the rule on a quote.

Dates are relative to today; agency, hotel and service names are invented.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from tests.integration.conftest import apply_migrations, as_booking_owner
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    ADMIN,
    CHECKER,
    DAY1,
    MAKER,
    SALES,
    SOURCE_ID,
    TENANT,
    TODAY,
    _departure,
    _m2m,
    _ok,
    _plan_line,
    _problem,
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


NEW_EVENTS = ["booking.created_by_aa", "booking.line_reduced", "booking.fee_changed"]


@pytest_asyncio.fixture
async def bk047(migrated_admin_dsn: str) -> AsyncIterator[None]:
    """Tests of bk-022 / bk-045 / bk-046 re-run those files (older outbox trigger, CHECKs, views): put bk-047 back
    first; afterwards delete the outbox rows of the bk-047 event types, so a later re-run of an older file's CHECK
    finds none (the flow commits)."""
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        await apply_migrations(conn, only=[47])
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        async with as_booking_owner(conn):
            await conn.execute("DELETE FROM booking.outbox WHERE event_type = ANY($1::text[])", NEW_EVENTS)
    finally:
        await conn.close()


def _entry(partner: str, line: dict, **extra: object) -> dict:
    return {"partner_id": partner, "agency_ref": "AG-MAIL-7", "currency": "USD", "lines": [line], **extra}


def _key(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


async def _events(c: httpx.AsyncClient, m2m: LocalM2M) -> list[tuple[str, str | None]]:
    page = await _ok(await c.get("/b2b/v1/events", headers=_m2m(m2m)))
    return [(e["type"], e["line_ref"]) for e in page["items"]]


async def test_staff_entry_vouchers_manifest_reduce_and_cancel(
    bk047: None, client: Client, admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    c, cis, m2m, _ = client
    sales, maker, boss = _staff(cis, SALES), _staff(cis, MAKER), _staff(cis, ADMIN)
    dep = await _departure(c, cis, DAY1, 10)
    line = {**_plan_line("tour", plan["plan"], dep["id"], adult=3), "pax": {"adult": 3, "child": 1, "infant": 1}}

    # The agency picker and the permission: contracting cannot enter a booking.
    agencies = await _ok(await c.get("/b2b-desk/agencies", headers=sales))
    assert [a["partner_id"] for a in agencies["items"]] == [plan["partner"]]
    denied = await c.post("/b2b-desk/entries", json=_entry(plan["partner"], line), headers=_key(maker, "desk-key-0"))
    assert denied.status_code == 403

    # Booked at once: one call, the trip case entered by staff, the agency's feed imports it.
    entry = await _ok(
        await c.post("/b2b-desk/entries", json=_entry(plan["partner"], line), headers=_key(sales, "desk-key-1")), 201
    )
    assert entry["status"] == "booked" and entry["trip_case"]["code"].startswith("TC-")
    again = await _ok(
        await c.post("/b2b-desk/entries", json=_entry(plan["partner"], line), headers=_key(sales, "desk-key-1")), 201
    )
    assert again["id"] == entry["id"]
    case_id = entry["trip_case"]["id"]
    row = await admin.fetchrow("SELECT entered_via, entered_by FROM booking.trip_cases WHERE id = $1::uuid", case_id)
    assert dict(row) == {"entered_via": "staff", "entered_by": SALES}
    assert await _events(c, m2m) == [("booking.created_by_aa", None), ("line.confirmed", "tour")]
    seats = await admin.fetchval("SELECT seats FROM booking.b2b_hold_lines WHERE line_ref = 'tour'")
    assert seats == 4  # adults + children

    # A price below the floor waits for a second person and books on approval.
    floor_case = await _ok(
        await c.post(
            "/b2b-desk/entries",
            json={**_entry(plan["partner"], _plan_line("tour", plan["plan"], dep["id"])), "agency_ref": "AG-MAIL-8",
                  "override_total": "1.00", "override_reason": "Repeat group, matched an offer"},
            headers=_key(boss, "desk-key-2"),
        ),
        201,
    )  # fmt: skip
    assert floor_case["status"] == "awaiting_approval" and floor_case["trip_case"] is None
    assert floor_case["floor_total"] is not None and Decimal(floor_case["floor_total"]) > 1
    assert (await c.post(f"/b2b-desk/entries/{floor_case['id']}/approve", headers=sales)).status_code == 403
    own = await c.post(f"/b2b-desk/entries/{floor_case['id']}/approve", headers=boss)
    assert own.status_code == 403 and own.json()["detail"] == "maker_checker"
    approved = await _ok(await c.post(f"/b2b-desk/entries/{floor_case['id']}/approve", headers=maker))
    assert approved["status"] == "booked" and approved["total"] == "1.00"
    listed = await _ok(await c.get("/b2b-desk/entries", headers=sales))
    assert listed["total"] == 2 and listed["awaiting_approval"] == 0
    assert listed["items"][0]["floor_total"] is not None  # sales hold pricing.read

    # A confirmed supplier booking of the case: the voucher (no name, no price) for the agency and staff.
    supplier = await admin.fetchval(
        "SELECT supplier_id::text FROM booking.services WHERE id = $1::uuid", plan["service"]
    )
    await admin.execute(
        "INSERT INTO booking.supplier_bookings (trip_case_id, supplier_id, item_description, service_date, status,"
        " requested_at, confirmed_at, confirmation_ref, service_time, pickup_point, dropoff_point, voucher_note,"
        " b2b_hold_id, b2b_line_ref, created_by)"
        " SELECT $1::uuid, $2::uuid, 'Airport pick-up', $3::date, 'pending', NULL, NULL, NULL, NULL, NULL, NULL, NULL,"
        " h.id, 'tour', $4 FROM booking.b2b_holds h WHERE h.trip_case_id = $1::uuid",
        case_id,
        supplier,
        TODAY + timedelta(days=30),
        SALES,
    )
    await admin.execute(
        "UPDATE booking.supplier_bookings SET status = 'requested', requested_at = now() WHERE trip_case_id = $1::uuid",
        case_id,
    )
    await admin.execute(
        "UPDATE booking.supplier_bookings SET status = 'confirmed', confirmed_at = now(), confirmation_ref = 'RR-55',"
        " service_time = '07:45', pickup_point = 'Lobby, Willowmere Hotel', dropoff_point = 'Terminal 2',"
        " voucher_note = 'Driver holds an AA sign' WHERE trip_case_id = $1::uuid",
        case_id,
    )
    mine = await _ok(await c.get(f"/b2b/v1/bookings/{case_id}/vouchers", headers=_m2m(m2m)))
    (voucher,) = mine["vouchers"]
    assert voucher["service_time"] == "07:45" and voucher["pickup_point"] == "Lobby, Willowmere Hotel"
    assert voucher["confirmation_ref"] == "RR-55" and voucher["travellers"] == {"adult": 3, "child": 1, "infant": 1}
    assert voucher["supplier"] == {"name": "Red River DMC"}  # no contact field (contract field policy)
    page = await c.get(f"/b2b-desk/bookings/{entry['trip_case']['code']}/vouchers", headers=sales)
    assert page.status_code == 200 and "RR-55" in page.text and "Lead traveller" in page.text
    assert page.headers["content-security-policy"].startswith("default-src 'none'")

    # The departure manifest: counts only, both bookings, the supplier booking.
    manifest = await _ok(await c.get(f"/b2b-desk/departures/{dep['id']}/manifest", headers=sales))
    assert manifest["totals"] == {"adult": 5, "child": 1, "infant": 1, "seats": 6, "bookings": 2}
    assert {b["agency_ref"] for b in manifest["bookings"]} == {"AG-MAIL-7", "AG-MAIL-8"}
    assert [s["confirmation_ref"] for s in manifest["supplier_bookings"]] == ["RR-55"]
    html_page = await c.get(f"/b2b-desk/departures/{dep['id']}/manifest?format=html", headers=sales)
    assert html_page.status_code == 200 and "Departure manifest" in html_page.text

    # Partial cancellation through the agency API: dry run, then one adult off; the seat goes back on sale.
    path = f"/b2b/v1/bookings/{case_id}/lines/tour/reduce"
    body = {"adult": 1, "reason_code": "client_cancelled"}
    preview = await _ok(await c.post(path + "?dry_run=true", json=body, headers=_m2m(m2m, "reduce-key-1")))
    assert preview["dry_run"] is True and preview["seats_after"] == 3
    reduced = await _ok(await c.post(path, json=body, headers=_m2m(m2m, "reduce-key-1")))
    assert reduced["removed"] == {"adult": 1, "child": 0, "infant": 0} and reduced["reduced_at"] is not None
    replay = await _ok(await c.post(path, json=body, headers=_m2m(m2m, "reduce-key-1")))
    assert replay["fee"] == reduced["fee"]
    assert await admin.fetchval("SELECT seats FROM booking.b2b_hold_lines WHERE hold_id = (SELECT id FROM"
                                " booking.b2b_holds WHERE trip_case_id = $1::uuid)", case_id) == 3  # fmt: skip
    _problem(
        await c.post(path, json={"adult": 2, "reason_code": "other"}, headers=_m2m(m2m, "reduce-key-2")), 422, "invalid"
    )
    confirmation = await _ok(await c.get(f"/b2b/v1/bookings/{case_id}/confirmation", headers=_m2m(m2m)))
    assert confirmation["lines"][0]["pax"] == {"adult": 2, "child": 1, "infant": 1}
    assert ("booking.line_reduced", "tour") in await _events(c, m2m)

    # Staff cancel for the agency with a lower fee asked; approved by another person.
    cancel_path = f"/b2b-desk/bookings/{entry['trip_case']['code']}/cancel"
    cancel_body = {"reason_code": "agency_cancelled", "fee_override_pct": "0", "waive_reason": "Supplier rebooked"}
    assert (await c.post(cancel_path, json=cancel_body, headers=_key(maker, "cancel-key-1"))).status_code == 403
    cancelled = await _ok(await c.post(cancel_path, json=cancel_body, headers=_key(sales, "cancel-key-1")))
    x = cancelled["cancellation"]
    assert x["entered_via"] == "staff" and x["override"]["status"] == "pending" and x["override"]["fee_pct"] == "0.00"
    fee_before = Decimal(x["fee"]["amount"])
    approve = f"/b2b-desk/bookings/{case_id}/cancellation/override/approve"
    seen = {"fee_override_pct": "0.00"}
    assert (await c.post(approve, json=seen, headers=sales)).status_code == 403
    # Maker-checker: the pending value is never swapped, and the approver approves only the value they saw.
    override = f"/b2b-desk/bookings/{case_id}/cancellation/override"
    other = {"fee_override_pct": "5", "waive_reason": "Changed my mind"}
    for res, code in (
        (await c.post(override, json=other, headers=sales), "override_pending"),
        (await c.post(approve, json={"fee_override_pct": "5"}, headers=maker), "override_changed"),
    ):
        assert (res.status_code, res.json()["detail"]) == (409, code), res.text
    done = await _ok(await c.post(approve, json=seen, headers=maker))
    assert done["fee"]["amount"] == "0.00" and done["override"]["status"] == "approved"
    events = await _events(c, m2m)
    assert "booking.cancelled" in [t for t, _ in events]
    assert ("booking.fee_changed", None) in events if fee_before > 0 else True
    revenue = await admin.fetchval("SELECT revenue FROM booking.v_case_profit WHERE trip_case_id = $1::uuid", case_id)
    assert revenue == Decimal(reduced["fee"]["amount"])  # the cancel fee waived, the reduction fee kept
    # The confirmed supplier booking stays to cancel with the supplier; its fee is recorded later.
    sb = await admin.fetchval("SELECT id::text FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid", case_id)
    await _ok(
        await c.post(f"/supplier-bookings/{sb}/cancel", json={"reason": "Agency cancelled", "supplier_cancel_fee":
                     "12.50", "supplier_cancel_fee_currency": "USD"}, headers=sales)
    )  # fmt: skip
    cost = await admin.fetchval("SELECT cost_usd FROM booking.v_cost_variance WHERE supplier_booking_id = $1::uuid", sb)
    assert cost == Decimal("12.50")


async def test_tour_terms_and_child_price(
    bk047: None, client: Client, admin: asyncpg.Connection, plan: dict[str, str]
) -> None:
    c, cis, m2m, _ = client
    boss, sales = _staff(cis, ADMIN), _staff(cis, SALES)

    # The tour's own deposit and balance days win over the default for every agency, the agency's row over both.
    assert (await c.put(f"/tour-terms/{SOURCE_ID}", json={"deposit_pct": "0.5", "balance_days_before_departure": 45},
                        headers=sales)).status_code == 403  # fmt: skip
    await _ok(
        await c.put(f"/tour-terms/{SOURCE_ID}", json={"deposit_pct": "0.5", "balance_days_before_departure": 45},
                    headers=boss)
    )  # fmt: skip
    terms = await _ok(await c.get(f"/b2b/v1/terms?source_id={SOURCE_ID}", headers=_m2m(m2m)))
    assert terms["payment"]["deposit_pct"] == "50.00" and terms["payment"]["scope"] == "tour"
    await _ok(
        await c.put(f"/tour-terms/{SOURCE_ID}", json={"tenant_id": TENANT, "deposit_pct": "0.2",
                    "balance_days_before_departure": 20}, headers=boss)
    )  # fmt: skip
    terms = await _ok(await c.get(f"/b2b/v1/terms?source_id={SOURCE_ID}", headers=_m2m(m2m)))
    assert (terms["payment"]["deposit_pct"], terms["payment"]["scope"]) == ("20.00", "agency_tour")
    plain = await _ok(await c.get("/b2b/v1/terms", headers=_m2m(m2m)))
    assert plain["payment"]["scope"] in ("agency", "default")
    rows = await _ok(await c.get(f"/tour-terms/{SOURCE_ID}", headers=boss))
    assert len(rows["items"]) == 2
    assert (await c.delete(f"/tour-terms/{SOURCE_ID}?tenant_id={TENANT}", headers=boss)).status_code == 204

    # A child at 50 % of the adult price: a b2b rule dated today with child_pct, approved by a second person.
    adults = await _ok(await _quote(c, m2m, "PRO-C1", [_plan_line("tour", plan["plan"], None, adult=3)]), 201)
    rule = await admin.fetchval(
        "INSERT INTO booking.pricing_rules (channel, target_margin, floor_margin, peak_uplift_pts, card_fee_pct,"
        " valid_from, created_by, child_pct, infant_flat_usd)"
        " SELECT channel, target_margin, floor_margin, peak_uplift_pts, card_fee_pct, $1, $2, 0.5, 10"
        " FROM booking.pricing_rules WHERE channel = 'b2b' ORDER BY valid_from DESC LIMIT 1 RETURNING id::text",
        TODAY,
        MAKER,
    )
    await admin.execute(
        "INSERT INTO booking.policy_approvals (entity_type, pricing_rule_id, approved_by)"
        " VALUES ('pricing_rule', $1::uuid, $2)",
        rule,
        CHECKER,
    )
    try:
        child = {**_plan_line("tour", plan["plan"], None, adult=2), "pax": {"adult": 2, "child": 1}}
        mixed = await _ok(await _quote(c, m2m, "PRO-C2", [child]), 201)
        three = Decimal(adults["total_b2b_price"]["amount"])
        assert Decimal(mixed["total_b2b_price"]["amount"]) < three  # the child pays half
        infant = {**_plan_line("tour", plan["plan"], None, adult=2), "pax": {"adult": 2, "child": 1, "infant": 1}}
        with_infant = await _ok(await _quote(c, m2m, "PRO-C3", [infant]), 201)
        assert Decimal(with_infant["total_b2b_price"]["amount"]) > Decimal(mixed["total_b2b_price"]["amount"])
        # The agency reads which ages count as child and infant on the product (1.7): defaults, then the plan's own.
        read = _m2m(m2m)

        async def bands() -> dict[str, int]:
            page = await _ok(await c.get("/b2b/v1/products", headers=read))
            return next(p["age_bands"] for p in page["items"] if p["tour"]["source_id"] == SOURCE_ID)

        assert await bands() == {"child_age_max": 11, "infant_age_max": 1}
        body = {"child_age_max": 9, "infant_age_max": 2}
        await _ok(await c.put(f"/catalog/trip-plans/{SOURCE_ID}/age-bands", json=body, headers=boss))
        assert await bands() == body
    finally:
        await admin.execute("DELETE FROM booking.policy_approvals WHERE pricing_rule_id = $1::uuid", rule)
        await admin.execute("DELETE FROM booking.pricing_rules WHERE id = $1::uuid", rule)
