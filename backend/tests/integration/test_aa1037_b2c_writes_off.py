"""AA-1037 (root ADR 0019 d8) on a real PostgreSQL, over HTTP as role ``booking``, with the B2C switch off (the
default): no B2C write path is left, and retention still works.

- every staff write on a b2c trip case, its customer, its invoice and its payment answers 410 ``b2b_only`` and leaves
  every B2C row as it was (a fingerprint of the B2C tables before and after);
- the billing reads stay, with no write affordance;
- a write on a B2B case still works;
- retention still covers the B2C rows: the DM-7 clearing of trip-check personal data and the DM-6.6 inbox payload
  redaction both run on them while the switch is off.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.catalog import liveness
from app.db import Database
from app.main import create_app
from app.ops import lifecycle
from app.retention.policy import INBOX_KIND
from app.settings import Settings
from tests import inquiry_samples as samples
from tests.integration import ops
from tests.integration.test_bk012_retention import StubS3, result, run
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN, OTHER = "it-682-aa1037-admin", "it-682-aa1037-other"
INBOX_EVENT = "it-aa1037-inbox"  # the idempotency_key of the test inbox event

# Every B2C table a staff write could touch, fingerprinted row by row (ordered by a unique key).
FINGERPRINT = {
    "booking.trip_cases": "id",
    "booking.customers": "id",
    "booking.invoices": "id",
    "booking.invoice_items": "id",
    "booking.payment_schedule": "id",
    "booking.payments": "id",
    "booking.trip_case_versions": "id",
    "booking.trip_case_version_itinerary": "id",
    "booking.trip_case_status_history": "id",
    "booking.staff_assignments": "id",
    "booking.supplier_bookings": "id",
    "booking.trip_case_checks": "id",
    "booking.ops_files": "id",
    "booking.notifications": "id",
    "booking.audit_logs": "id",
}


@pytest_asyncio.fixture(autouse=True)
async def _no_pilot_view(migrated_admin_dsn: str) -> AsyncIterator[None]:
    """AA-969 d16: the retention clean-ups fail closed without the pilot marker view: an empty one says "no pilot"."""
    conn = await asyncpg.connect(migrated_admin_dsn)
    try:
        await ops.no_pilot_view(conn)
        liveness.reset_cache()
        yield
    finally:
        await conn.execute(ops.TENANT_ACCOUNT_DROP)
        liveness.reset_cache()
        await conn.close()


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await conn.execute("DELETE FROM booking.inbox_events WHERE idempotency_key = $1", INBOX_EVENT)
    await ops.staff(conn, (ADMIN, "admin"), (OTHER, "admin"))
    try:
        yield conn
    finally:
        await conn.execute("UPDATE booking.trip_cases SET inbox_event_id = NULL WHERE notes LIKE 'it-682%'")
        await conn.execute("DELETE FROM booking.inbox_events WHERE idempotency_key = $1", INBOX_EVENT)
        await ops.purge_ops(conn)
        await conn.close()


Client = tuple[httpx.AsyncClient, LocalCis]


@pytest_asyncio.fixture
async def client(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[Client]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    assert settings.b2c_intake_enabled is False  # the default: B2C frozen
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


@pytest_asyncio.fixture
async def pool(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


def _as(cis: LocalCis, sub: str = ADMIN) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='admin')}", "Idempotency-Key": "it-aa1037-key"}


async def _fingerprint(admin: asyncpg.Connection) -> dict[str, str | None]:
    out = {}
    for table, key in FINGERPRINT.items():
        out[table] = await admin.fetchval(
            f"SELECT md5(string_agg(t::text, '|' ORDER BY t.{key})) FROM {table} t"  # noqa: S608 — fixed names
        )
    return out


async def _b2c_world(admin: asyncpg.Connection) -> dict[str, Any]:
    """A sold b2c case with a sent invoice, a recorded payment, a supplier booking and its checks."""
    sup = await ops.supply(admin, name="Aa1037")
    case = await ops.sold_case(admin, sup, tag="aa1037")
    inv = await ops.draft_invoice(admin, case, by=ADMIN, tag="aa1037-inv")
    await ops.send_invoice(admin, inv["id"], by=ADMIN)
    payment = await admin.fetchrow(
        "SELECT id::text AS id, ref FROM booking.payments WHERE id = $1::uuid",
        await ops.record_payment(admin, inv["id"], "100", by=ADMIN, tag="aa1037-pay"),
    )
    booking = await admin.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, version_item_id, supplier_id, item_description)"
        " VALUES ($1::uuid, $2::uuid, $3::uuid, 'Room') RETURNING id::text",
        case["id"],
        case["room_item"],
        sup["supplier"],
    )
    check = await admin.fetchval(
        "SELECT id::text FROM booking.trip_case_checks WHERE trip_case_id = $1::uuid ORDER BY code LIMIT 1",
        case["id"],
    )
    customer = await admin.fetchval(
        "SELECT c.code FROM booking.customers c JOIN booking.trip_cases tc ON tc.customer_id = c.id"
        " WHERE tc.id = $1::uuid",
        case["id"],
    )
    customer_id = await admin.fetchval(
        "SELECT customer_id::text FROM booking.trip_cases WHERE id = $1::uuid", case["id"]
    )
    return {
        "case": case["code"],
        "invoice": inv["invoice_number"],
        "payment": payment["ref"],
        "booking": booking,
        "check": check,
        "customer": customer,
        "customer_id": customer_id,
        "supplier": sup["supplier"],
    }


async def test_every_b2c_write_is_refused_and_leaves_no_row(admin: asyncpg.Connection, client: Client) -> None:
    c, cis = client
    w = await _b2c_world(admin)
    h, case, inv, pay = _as(cis), w["case"], w["invoice"], w["payment"]
    adjustment = {"description": "Late check-out", "unit_price": "10", "reason": "asked"}
    calls: list[tuple[str, str, Any]] = [
        # Customer invoices and payments (booking.invoices / payments are B2C-only).
        ("POST", f"/trip-cases/{case}/invoices", {}),
        ("PATCH", f"/invoices/{inv}", {"notes": "x"}),
        ("DELETE", f"/invoices/{inv}", None),
        ("POST", f"/invoices/{inv}/send", None),
        ("POST", f"/invoices/{inv}/cancel", {"reason": "duplicate"}),
        ("POST", f"/invoices/{inv}/amend", {"adjustments": [adjustment]}),
        ("POST", f"/invoices/{inv}/remind", None),
        ("POST", f"/invoices/{inv}/payments", {"amount": "50", "method": "cash", "received_on": str(date.today())}),
        ("PATCH", f"/payments/{pay}", {"note": "x"}),
        ("POST", f"/payments/{pay}/verify", None),
        ("POST", f"/payments/{pay}/void", {"reason": "typo"}),
        # The customer.
        ("PATCH", f"/customers/{w['customer']}", {"name": "Someone Else"}),
        ("DELETE", f"/customers/{w['customer']}", None),
        # New B2C cases.
        ("POST", "/trip-cases", {"customer_id": w["customer_id"]}),
        # The b2c case itself.
        ("PATCH", f"/trip-cases/{case}", {"notes": "changed"}),
        ("POST", f"/trip-cases/{case}/status", {"to_status": "cancelled", "reason": "no reply"}),
        ("POST", f"/trip-cases/{case}/assign", {"cis_user_id": OTHER}),
        ("POST", f"/trip-cases/{case}/versions", None),
        ("PATCH", f"/trip-cases/{case}/versions/1", {"title": "Plan B"}),
        ("DELETE", f"/trip-cases/{case}/versions/1", None),
        ("PUT", f"/trip-cases/{case}/versions/1/items", {"items": []}),
        ("POST", f"/trip-cases/{case}/versions/1/price", {"channel": "b2c_tailor_made"}),
        ("POST", f"/trip-cases/{case}/versions/1/send", {"subject": "Your trip", "message": "Hello"}),
        ("POST", f"/trip-cases/{case}/versions/1/approve", None),
        ("POST", f"/trip-cases/{case}/supplier-bookings/generate", None),
        ("POST", f"/trip-cases/{case}/supplier-bookings", {"supplier_id": w["supplier"]}),
        ("PATCH", f"/supplier-bookings/{w['booking']}", {"notes": "x"}),
        ("POST", f"/supplier-bookings/{w['booking']}/request", None),
        ("POST", f"/supplier-bookings/{w['booking']}/cancel", {"reason": "moved"}),
        ("POST", f"/trip-cases/{case}/checks", {"title": "Visa copies"}),
        ("PATCH", f"/trip-checks/{w['check']}", {"note": "x"}),
    ]
    before = await _fingerprint(admin)
    for method, path, body in calls:
        r = (
            await c.request(method, path, json=body, headers=h)
            if body is not None
            else await c.request(method, path, headers=h)
        )
        assert r.status_code == 410, (method, path, r.text)
        assert r.json()["detail"] == "b2b_only", (method, path)
    assert await _fingerprint(admin) == before

    # The reads stay, and offer no write.
    invoice = await c.get(f"/invoices/{inv}", headers=h)
    assert invoice.status_code == 200 and not any(invoice.json()["actions"].values())
    tab = (await c.get(f"/trip-cases/{case}/billing", headers=h)).json()
    assert tab["invoice"]["invoice_number"] == inv and not any(tab["invoice"]["actions"].values())
    payment = (await c.get(f"/payments/{pay}", headers=h)).json()
    assert not any(payment["actions"].values())
    assert (await c.get(f"/trip-cases/{case}", headers=h)).status_code == 200
    assert (await c.get(f"/customers/{w['customer']}", headers=h)).status_code == 200


async def test_a_b2b_case_stays_writable(admin: asyncpg.Connection, client: Client) -> None:
    c, cis = client
    case = await ops.b2b_case(admin, tag="aa1037-b2b")
    r = await c.patch(f"/trip-cases/{case['code']}", json={"notes": "it-682-aa1037 agency note"}, headers=_as(cis))
    assert r.status_code == 200, r.text
    assert await admin.fetchval("SELECT notes FROM booking.trip_cases WHERE id = $1::uuid", case["id"]) == (
        "it-682-aa1037 agency note"
    )


async def test_retention_still_covers_b2c_rows(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    # DM-7: the trip-check personal data of a b2c trip that ended 90+ days ago is cleared by the frozen lifecycle run.
    today = await admin.fetchval("SELECT (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date")
    old = await ops.b2c_case(admin, tag="aa1037-old", status="confirmed", start=today - timedelta(days=100))
    await admin.execute(
        "UPDATE booking.trip_case_checks SET emergency_contact_name = 'Ola Example', note = 'after six'"
        " WHERE trip_case_id = $1::uuid AND code = 'emergency_contact'",
        old["id"],
    )
    live = await lifecycle.run_lifecycle(pool, mode="live", case_ids=[old["id"]])
    assert live["b2c"] == "frozen" and live["candidates"] == 0  # no move on the frozen case
    assert (live["cleared_checks"], live["cleared_cases"]) == (1, 1)
    assert (
        await admin.fetchval(
            "SELECT emergency_contact_name FROM booking.trip_case_checks"
            " WHERE trip_case_id = $1::uuid AND code = 'emergency_contact'",
            old["id"],
        )
        is None
    )

    # DM-6.6: the inbox payload behind a b2c case is redacted by bk_retention_inbox_redact.
    payload = samples.v1_1("it-aa1037-lead@example.test")
    event = await admin.fetchval(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, received_at, status)"
        " VALUES ('tripplanner', 'inquiry.submitted', $1, $2::jsonb, now() - interval '100 days', 'processed')"
        " RETURNING id::text",
        INBOX_EVENT,
        json.dumps(payload),
    )
    await admin.execute("UPDATE booking.trip_cases SET inbox_event_id = $2::uuid WHERE id = $1::uuid", old["id"], event)
    s3 = StubS3()
    dry = result(await run(pool, s3, INBOX_KIND))
    assert dry["mode"] == "dry_run" and dry["candidates_total"] >= 1
    live_redact = result(await run(pool, s3, INBOX_KIND, dry_run=False, max_rows=dry["candidates_total"]))
    assert live_redact["mode"] == "live" and live_redact["affected_total"] == dry["candidates_total"]
    row = await admin.fetchrow(
        "SELECT payload::text AS payload, redacted_at FROM booking.inbox_events WHERE id = $1::uuid", event
    )
    assert row["redacted_at"] is not None and "it-aa1037-lead@example.test" not in row["payload"]
