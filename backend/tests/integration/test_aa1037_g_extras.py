"""AA-1037 R4 extras on a real PostgreSQL (bk-041 / bk-042 / bk-044), as role ``booking``, through the HTTP API.
Reuses the AA-973 / AA-974 fixtures (a sellable plan, an active partner, the seeded default cancellation policy).

- G1: a quote and an active hold show the policy a confirm today would lock; the confirmed hold shows it locked;
- G2: the booking confirmation of the confirmed booking (JSON and the HTML file) for the agency and for staff, with a
  supplier confirmation ref, no traveller name and no price; another booking id is 404;
- G4: an issued agency invoice overdue past the default suspend days makes ``POST /holds`` 409
  ``credit_hold_overdue`` and the terms answer ``holds_paused``; the reminder job sends the credit-stop step once and
  never again; approving the payment that settles it lifts the stop;
- G5: the accountant CSV of invoices and payments of a period;
- G6: the draft price-error clause is not in the agency's terms; a second person approves it and it is; an edit makes
  it a draft again.

Dates are relative to today (lessons.md S245). Names and places are invented."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app import mailer
from app.agency_billing import reminders
from app.db import Database
from app.settings import Settings
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    ADMIN,
    CHECKER,
    DAY1,
    MAKER,
    TENANT,
    _departure,
    _hold_body,
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
from tests.integration.test_bk021_b2b import SEED, rules  # noqa: F401 — fixture used by name
from tests.staff_tokens import LocalCis, LocalM2M

FIXTURES = (admin, client, plan, rules)  # the AA-973 / AA-974 fixtures, used by name below

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]
BILLING_TABLES = (
    "booking.agency_invoice_reminder",
    "booking.agency_payment",
    "booking.agency_invoice_line",
    "booking.agency_invoice",
)
UTC_TODAY = datetime.now(UTC).date()
FIN = "it-gx-finance"  # edits clauses, records payments and exports (finance)
CLAUSE_SEED = (
    "SELECT heading, body, free_cancel_days, position FROM booking.partner_terms_clause WHERE code = 'price_error'"
)


async def _purge_billing(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE bk_owner")
        for t in BILLING_TABLES[1:]:
            await conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        for t in BILLING_TABLES:
            await conn.execute(f"DELETE FROM {t}")  # noqa: S608
        for t in BILLING_TABLES[1:]:
            await conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")
        await conn.execute("UPDATE booking.billing_entity SET is_default = false WHERE code LIKE 'ITGX%'")
        await conn.execute("DELETE FROM booking.billing_entity WHERE code LIKE 'ITGX%'")
        await conn.execute("DELETE FROM booking.partner_terms_clause WHERE code LIKE 'itgx%'")


@pytest_asyncio.fixture
async def billing(admin: asyncpg.Connection, rules: dict[str, str]) -> AsyncIterator[dict[str, str]]:
    """The plan + partner of the AA-973 fixtures, no agency billing rows, the seed clause kept as it was."""
    await _purge_billing(admin)
    seed = await admin.fetchrow(CLAUSE_SEED)
    await admin.execute(
        "INSERT INTO booking.staff_users (cis_user_id, role, active, name) VALUES ($1, 'finance', true, $1)"
        " ON CONFLICT (cis_user_id) DO UPDATE SET role = 'finance', active = true",
        FIN,
    )
    try:
        yield rules
    finally:
        await _purge_billing(admin)
        await admin.execute(
            "UPDATE booking.partner_terms_clause SET heading = $1, body = $2, free_cancel_days = $3, position = $4,"
            " status = 'draft', approved_by = NULL, approved_at = NULL, version = 1, updated_by = 'system:bk-044'"
            " WHERE code = 'price_error'",
            *seed,
        )


async def _book(c: httpx.AsyncClient, cis: LocalCis, m2m: LocalM2M, plan_id: str, ref: str) -> dict[str, Any]:
    dep = await _departure(c, cis, DAY1, 6)
    quote = await _ok(await _quote(c, m2m, ref, [_plan_line("tour", plan_id, dep["id"], DAY1, adult=2)]), 201)
    hold = await _ok(
        await c.post("/b2b/v1/holds", json=_hold_body(quote, ref), headers=_m2m(m2m, f"{ref}-hold-key")), 201
    )
    booking = await _ok(
        await c.post(
            f"/b2b/v1/holds/{hold['hold_id']}/confirm",
            json={"price_hash": quote["price_hash"]},
            headers=_m2m(m2m, f"{ref}-confirm-key"),
        ),
        201,
    )
    return {"quote": quote, "hold": hold, "booking": booking, "departure": dep}


async def test_policy_on_answers_and_the_booking_confirmation(
    client: Client, admin: asyncpg.Connection, billing: dict[str, str]
) -> None:
    c, cis, m2m, _ = client
    flow = await _book(c, cis, m2m, billing["plan"], "GX-1")
    quote, hold, booking = flow["quote"], flow["hold"], flow["booking"]
    for answer in (quote, hold):
        policy = answer["cancellation_policy"]
        assert policy["policy_id"] == SEED and policy["basis"] == "in_force_today"
        assert policy["tiers"][0] == {"min_days": 60, "fee_pct": "0.00"}
        assert policy["tiers"][-1] == {"min_days": 0, "fee_pct": "100.00"}
    held = await _ok(await c.get(f"/b2b/v1/holds/{hold['hold_id']}", headers=_m2m(m2m)))
    assert held["status"] == "confirmed" and held["cancellation_policy"]["basis"] == "locked"

    # A supplier booking covering the agency line, confirmed with the supplier's reference.
    supplier = await admin.fetchval("SELECT id FROM booking.suppliers LIMIT 1")
    sb = await admin.fetchval(
        "INSERT INTO booking.supplier_bookings (code, trip_case_id, supplier_id, item_description, b2b_hold_id,"
        " b2b_line_ref, created_by) VALUES ('SB-990001', $1::uuid, $2, 'Room block', $3::uuid, 'tour', $4)"
        " RETURNING id",
        booking["booking_id"],
        supplier,
        hold["hold_id"],
        MAKER,
    )
    await admin.execute(
        "UPDATE booking.supplier_bookings SET status = 'requested', requested_at = now(), requested_by = $2"
        " WHERE id = $1",
        sb,
        MAKER,
    )
    await admin.execute(
        "UPDATE booking.supplier_bookings SET status = 'confirmed', confirmed_at = now(), confirmed_by = $2,"
        " confirmation_ref = 'RIVER-5521' WHERE id = $1",
        sb,
        MAKER,
    )

    url = f"/b2b/v1/bookings/{booking['booking_id']}/confirmation"
    doc = await _ok(await c.get(url, headers=_m2m(m2m)))
    assert doc["trip_case_code"] == booking["trip_case_code"] and doc["status"] == "confirmed"
    assert doc["proposal_ref"] == "GX-1" and doc["travellers"] == {"adult": 2, "child": 0, "infant": 0}
    [line] = doc["lines"]
    assert line["title"] == "Hanoi 1D" and line["kind"] == "trip_plan" and line["status"] == "confirmed"
    assert line["supplier_confirmations"] == ["RIVER-5521"]
    assert doc["cancellation_policy"]["policy_id"] == SEED and doc["cancellation_policy"]["basis"] == "locked"
    assert "b2b_price" not in json.dumps(doc) and "amount" not in json.dumps(doc)
    page = await c.get(url, params={"format": "html"}, headers=_m2m(m2m))
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert booking["trip_case_code"] in page.text and "RIVER-5521" in page.text and "Agency 973" in page.text
    _problem(await c.get("/b2b/v1/bookings/00000000-0000-4000-8000-000000000999/confirmation",
                         headers=_m2m(m2m)), 404, "not_found")  # fmt: skip

    staff = await c.get(f"/b2b-desk/bookings/{booking['trip_case_code']}/confirmation", headers=_staff(cis, MAKER))
    assert staff.status_code == 200 and "attachment" in staff.headers["content-disposition"]
    sales_json = await _ok(
        await c.get(
            f"/b2b-desk/bookings/{booking['booking_id']}/confirmation",
            params={"format": "json"},
            headers=_staff(cis, CHECKER),
        )
    )
    assert sales_json["agency_name"] == "Agency 973"

    # Cancelled: the confirmation says so.
    await _ok(
        await c.post(
            f"/b2b/v1/bookings/{booking['booking_id']}/cancel",
            json={"reason_code": "client_cancelled"},
            headers=_m2m(m2m, "gx-cancel-1"),
        )
    )
    after = await _ok(await c.get(url, headers=_m2m(m2m)))
    assert after["status"] == "cancelled" and after["cancelled_at"] is not None


async def _overdue_invoice(admin: asyncpg.Connection, *, due: date, total: str, email: str | None) -> str:
    entity = await admin.fetchval(
        "INSERT INTO booking.billing_entity (code, legal_entity, invoice_inbox, payment_instructions)"
        " VALUES ('ITGXAA', 'Lantern Bay Ltd', 'billing@lanternbay.example', 'Transfer to the contract account.')"
        " ON CONFLICT (code) DO UPDATE SET active = true RETURNING id"
    )
    inv = await admin.fetchval(
        "INSERT INTO booking.agency_invoice (party_tenant_id, invoice_kind, period_start, period_end, currency,"
        " subtotal, total, source, created_by, idempotency_key)"
        " VALUES ($1::uuid, 'settlement', $2, $3, 'USD', $4, $4, 'job', 'job:test', $5) RETURNING id::text",
        TENANT,
        due - timedelta(days=45),
        due - timedelta(days=15),
        D(total),
        f"settlement:{TENANT}:{due}:USD",
    )
    await admin.execute(
        "INSERT INTO booking.agency_invoice_line (invoice_id, invoice_kind, line_no, line_role, description,"
        " unit_price, amount, created_by) VALUES ($1::uuid, 'settlement', 1, 'adjustment', 'Tour money', $2, $2,"
        " 'job:test')",
        inv,
        D(total),
    )
    bill_to = {"agency_name": "Agency 973", "source": "cis", "legal_entity_name": "Agency 973 Co", "tax_id": "77",
               "billing_country": "VN", **({"billing_email": email} if email else {})}  # fmt: skip
    issuer = {"legal_entity": "Lantern Bay Ltd", "invoice_inbox": "billing@lanternbay.example",
              "payment_instructions": "Transfer to the contract account."}  # fmt: skip
    await admin.execute(
        "UPDATE booking.agency_invoice SET status = 'issued', number = 'AAT-2026-000901', issued_at = now(),"
        " issued_by = 'fin-x', issued_on = $2, due_date = $3, billing_entity_id = $4, issuer = $5::jsonb,"
        " bill_to = $6::jsonb WHERE id = $1::uuid",
        inv,
        due - timedelta(days=14),
        due,
        entity,
        json.dumps(issuer),
        json.dumps(bill_to),
    )
    return inv


async def test_credit_stop_reminders_and_release(
    client: Client, admin: asyncpg.Connection, billing: dict[str, str], migrated_booking_dsn: str
) -> None:
    c, cis, m2m, _ = client
    suspend = await admin.fetchval("SELECT suspend_after_days FROM booking.billing_terms WHERE party_tenant_id IS NULL")
    inv = await _overdue_invoice(
        admin, due=UTC_TODAY - timedelta(days=suspend + 2), total="500.00", email="accounts@agency973.example"
    )
    terms = await _ok(await c.get("/b2b/v1/terms", headers=_m2m(m2m)))
    assert terms["credit"]["status"] == "past_due" and terms["credit"]["holds_paused"] is True
    assert terms["credit"]["overdue_amount"] == [{"amount": "500.00", "currency": "USD"}]
    assert terms["payment"]["deposit_pct"] == "30.00"

    dep = await _departure(c, cis, DAY1, 4)
    quote = await _ok(await _quote(c, m2m, "GX-2", [_plan_line("tour", billing["plan"], dep["id"], DAY1)]), 201)
    refused = _problem(
        await c.post("/b2b/v1/holds", json=_hold_body(quote, "GX-2"), headers=_m2m(m2m, "gx2-hold-key")),
        409,
        "credit_hold_overdue",
    )
    assert "overdue" in refused["message"]
    assert await admin.fetchval("SELECT count(*) FROM booking.b2b_holds WHERE proposal_ref = 'GX-2'") == 0

    # The reminder job: the credit-stop step, once.
    settings = Settings(database_url=migrated_booking_dsn, environment="prod", ses_from_address="noreply@x.example")
    sent: list[mailer.Message] = []

    async def send(message: mailer.Message) -> str:
        sent.append(message)
        return f"ses-{len(sent)}"

    pool = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=2)
    try:
        first = await reminders.run(pool, settings, send, today=UTC_TODAY, actor="job:test")
        again = await reminders.run(pool, settings, send, today=UTC_TODAY, actor="job:test")
    finally:
        await pool.close()
    assert first["sent"] == 1 and again["sent"] == 0 and again["not_due"] == 1
    assert sent[0].to == "accounts@agency973.example" and "New bookings paused" in sent[0].subject
    row = await admin.fetchrow("SELECT * FROM booking.agency_invoice_reminder WHERE invoice_id = $1::uuid", inv)
    assert (row["step_days"], row["step_kind"], row["outcome"]) == (suspend, "credit_stop", "sent")
    detail = await _ok(await c.get(f"/agency-invoices/{inv}", headers=_staff(cis, CHECKER)))
    assert [r["step_kind"] for r in detail["reminders"]] == ["credit_stop"]

    # Finance records the payment, a second person approves it: the stop lifts and the hold goes through.
    pay = await _ok(
        await c.post(
            f"/agency-invoices/{inv}/payments",
            json={
                "amount": "500.00",
                "method": "bank_transfer",
                "received_on": UTC_TODAY.isoformat(),
                "document_ref": "statement line 3",
                "reference": "=TRF-1",
            },  # fmt: skip
            headers={**_staff(cis, FIN), "Idempotency-Key": "gx-pay-0001"},
        ),
        201,
    )
    await _ok(await c.post(f"/agency-payments/{pay['id']}/approve", headers=_staff(cis, CHECKER)))
    await _ok(await c.post("/b2b/v1/holds", json=_hold_body(quote, "GX-2"), headers=_m2m(m2m, "gx2-hold-key")), 201)

    # G5: the accountant export of the period.
    period = {"from": (UTC_TODAY - timedelta(days=90)).isoformat(), "to": UTC_TODAY.isoformat()}
    resp = await c.get("/agency-billing/export.csv", params={"what": "invoices", **period}, headers=_staff(cis, FIN))
    assert resp.status_code == 200, resp.text
    rows = list(csv.DictReader(io.StringIO(resp.text.lstrip("﻿"))))
    assert [(r["number"], r["status"], r["paid"], r["balance"]) for r in rows] == [
        ("AAT-2026-000901", "paid", "500.00", "0")
    ]
    assert rows[0]["bill_to_legal_entity"] == "Agency 973 Co"
    resp = await c.get("/agency-billing/export.csv", params={"what": "payments", **period}, headers=_staff(cis, FIN))
    rows = list(csv.DictReader(io.StringIO(resp.text.lstrip("﻿"))))
    assert len(rows) == 1 and rows[0]["status"] == "approved" and rows[0]["reference"] == "'=TRF-1"
    assert (await c.get("/agency-billing/export.csv", params={"what": "invoices", **period},
                        headers=_staff(cis, "it-973-sales"))).status_code == 403  # fmt: skip
    logged = await admin.fetchval("SELECT count(*) FROM booking.audit_logs WHERE action = 'agency_billing.exported'")
    assert logged >= 2


async def test_terms_clause_needs_a_second_person(
    client: Client, admin: asyncpg.Connection, billing: dict[str, str]
) -> None:
    c, cis, m2m, _ = client
    terms = await _ok(await c.get("/b2b/v1/terms", headers=_m2m(m2m)))
    assert terms["clauses"] == [] and terms["credit"]["holds_paused"] is False  # the seed clause is a draft
    assert terms["cancellation_policy"]["policy_id"] == SEED
    listed = await _ok(await c.get("/partner-terms-clauses", headers=_staff(cis, CHECKER)))
    [seed] = [i for i in listed["items"] if i["code"] == "price_error"]
    assert seed["status"] == "draft" and seed["free_cancel_days"] == 2
    approved = await _ok(
        await c.post(
            "/partner-terms-clauses/price_error/approve",
            json={"version": seed["version"]},
            headers=_staff(cis, CHECKER),
        )  # fmt: skip
    )
    assert approved["status"] == "approved" and approved["approved_by"] == CHECKER
    terms = await _ok(await c.get("/b2b/v1/terms", headers=_m2m(m2m)))
    assert [(cl["code"], cl["free_cancel_days"]) for cl in terms["clauses"]] == [("price_error", 2)]

    # Finance rewords it: a draft again (version + 1), gone from the agency answer; the editor cannot approve it.
    body = {"title": "Price errors", "body": "If a price is clearly wrong, AA corrects it.", "free_cancel_days": 3}
    edited = await _ok(await c.put("/partner-terms-clauses/price_error", json=body, headers=_staff(cis, FIN)))
    assert edited["status"] == "draft" and edited["version"] == seed["version"] + 1
    assert (await _ok(await c.get("/b2b/v1/terms", headers=_m2m(m2m))))["clauses"] == []
    resp = await c.post("/partner-terms-clauses/price_error/approve", json={"version": seed["version"]},
                        headers=_staff(cis, CHECKER))  # fmt: skip
    assert resp.status_code == 409 and resp.json()["detail"] == "clause_changed"
    await admin.execute("UPDATE booking.staff_users SET role = 'admin' WHERE cis_user_id = $1", FIN)
    own = await c.post("/partner-terms-clauses/price_error/approve", json={"version": edited["version"]},
                       headers=_staff(cis, FIN))  # fmt: skip
    assert own.status_code == 403 and own.json()["detail"] == "maker_checker"
    created = await _ok(
        await c.put("/partner-terms-clauses/itgx_test", json={"title": "T", "body": "B"}, headers=_staff(cis, ADMIN))
    )
    assert created["status"] == "draft" and created["version"] == 1
