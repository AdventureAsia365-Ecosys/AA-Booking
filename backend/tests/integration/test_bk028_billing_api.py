"""AA-682 S3 over HTTP on a real PostgreSQL (bk-028), as role ``booking`` — the slice's Done-when, end to end:

- a 30 / 70 invoice is created from the approved version and sent → the case is ``invoice_sent`` (the default
  schedule from the settings rows; an Idempotency-Key replay returns the same invoice, another body is 409);
- the deposit is recorded by admin A; A verifying it → 403 ``maker_checker``; B verifying without evidence → 422
  ``evidence_required``; with the evidence file → invoice ``partially_paid``, case ``payment_pending``;
- the balance (cash) → ``paid``; voiding it → back to ``payment_pending`` (sales may not void a verified payment);
- the same Idempotency-Key twice → one payment; card-like references are refused, duplicate references warned;
- an overdue schedule row writes its staff and customer reminder once per key (``bk_ops_reminders``);
- amendment, remind, draft edit / delete, cancel rules, case cancel cascade and reopen, B2B refusals, permissions
  (contracting and content creators read no billing; the case money badge is hidden from contracting), lists, KPIs.

The database rules themselves (direct ``UPDATE invoices SET status = 'paid'`` refused, concurrent verifies) are in
``test_bk028_schema.py``."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.ops import lifecycle
from app.settings import Settings
from tests.integration import ops
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN_A, ADMIN_B, SALES, CONTRACTING, CREATOR = (
    "it-682-admin-a",
    "it-682-admin-b",
    "it-682-sales",
    "it-682-contracting",
    "it-682-creator",
)
BUCKET = "aa-booking-dev-ops-docs-000000000000"
PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
CIS_ROLE = {ADMIN_A: "admin", ADMIN_B: "admin", SALES: "reviewer", CONTRACTING: "reviewer", CREATOR: "content"}


class FakeOpsStore:
    bucket = BUCKET

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self.objects[key] = data

    async def download_url(self, key: str, file_name: str, content_type: str) -> str:
        return f"https://signed.example/{key}"


Client = tuple[httpx.AsyncClient, LocalCis]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(
        conn,
        (ADMIN_A, "admin"),
        (ADMIN_B, "admin"),
        (SALES, "sales"),
        (CONTRACTING, "contracting"),
        (CREATOR, "content_creator"),
    )
    try:
        yield conn
    finally:
        await ops.purge_ops(conn)
        await conn.close()


@pytest_asyncio.fixture
async def client(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[Client]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier(), ops_store=FakeOpsStore())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


@pytest_asyncio.fixture
async def pool(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=2)
    try:
        yield p
    finally:
        await p.close()


def _as(cis: LocalCis, sub: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {cis.token(sub=sub, cis_role=CIS_ROLE[sub])}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _err(response: httpx.Response, status: int, detail: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail, response.json()
    return response.json()


async def _case_status(admin: asyncpg.Connection, code: str) -> str:
    return await admin.fetchval("SELECT status FROM booking.trip_cases WHERE code = $1", code)


async def _evidence(c: httpx.AsyncClient, headers: dict[str, str], payment_id: str) -> dict[str, Any]:
    body = {
        "kind": "payment_evidence",
        "entity_type": "payment",
        "entity_id": payment_id,
        "file_name": "bank slip.pdf",
        "content_type": "application/pdf",
        "data_base64": base64.b64encode(PDF + payment_id.encode()).decode(),
    }
    return await _ok(await c.post("/ops-files", json=body, headers=headers), 201)


async def test_done_when_sequence(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    a, b, sales = _as(cis, ADMIN_A), _as(cis, ADMIN_B), _as(cis, SALES)
    sup = await ops.supply(admin)
    sold = await ops.sold_case(admin, sup, tag="s3")
    billing = await _ok(await c.get(f"/trip-cases/{sold['code']}/billing", headers=sales))
    assert billing["invoiceable"] and billing["reason"] is None and billing["invoice"] is None
    assert [(r["kind"], r["amount"]) for r in billing["defaults"]["schedule"]] == [
        ("deposit", "720.00"),
        ("final", "1680.00"),
    ]
    _err(await c.post(f"/trip-cases/{sold['code']}/invoices", json={}, headers=sales), 422, "idempotency_key_required")

    # Invoice 30/70 sent -> case invoice_sent.
    body = {"send_now": True, "terms": "Deposit to confirm, balance 30 days before the trip."}
    inv = await _ok(
        await c.post(f"/trip-cases/{sold['code']}/invoices", json=body, headers=_as(cis, SALES, "inv-key-1")), 201
    )
    assert (inv["status"], inv["total_amount"], inv["document_type"]) == ("sent", "2400.00", "proforma")
    assert [s["amount"] for s in inv["schedule"]] == ["720.00", "1680.00"]
    assert inv["bill_to_email"] == "it-682-s3@example.test" and inv["sent_by"]["id"] == SALES
    assert inv["actions"]["record_payment"] and not inv["actions"]["send"]
    assert await _case_status(admin, sold["code"]) == "invoice_sent"
    replay = await c.post(f"/trip-cases/{sold['code']}/invoices", json=body, headers=_as(cis, SALES, "inv-key-1"))
    assert replay.status_code == 200 and replay.json()["id"] == inv["id"] and replay.json()["replayed"]
    _err(
        await c.post(
            f"/trip-cases/{sold['code']}/invoices", json={"send_now": False}, headers=_as(cis, SALES, "inv-key-1")
        ),
        409,
        "idempotency_conflict",
    )
    _err(
        await c.post(f"/trip-cases/{sold['code']}/invoices", json={}, headers=_as(cis, SALES, "inv-key-2")),
        409,
        "invoice_exists",
    )
    customer_rows = await admin.fetchval(
        "SELECT count(*) FROM booking.notifications WHERE recipient_type = 'customer' AND type = 'invoice.sent'"
        " AND entity_id = $1::uuid",
        inv["id"],
    )
    assert customer_rows == 1
    number = inv["invoice_number"]

    # Deposit recorded by A; the same Idempotency-Key twice -> one payment.
    deposit_body = {"amount": "720.00", "method": "bank_transfer", "received_on": date.today().isoformat(),
                    "reference": "VCB 2026-10-27 0001"}  # fmt: skip
    dep = await _ok(
        await c.post(f"/invoices/{number}/payments", json=deposit_body, headers=_as(cis, ADMIN_A, "payment-key-1")), 201
    )
    again = await c.post(f"/invoices/{number}/payments", json=deposit_body, headers=_as(cis, ADMIN_A, "payment-key-1"))
    assert again.status_code == 200 and again.json()["id"] == dep["id"] and again.json()["replayed"]
    assert await admin.fetchval("SELECT count(*) FROM booking.payments WHERE invoice_id = $1::uuid", inv["id"]) == 1
    assert dep["state"] == "recorded" and dep["needs_evidence"] and not dep["actions"]["verify"]
    # Verify by A -> 403 maker_checker; by B without evidence -> 422; with it -> partially_paid / payment_pending.
    _err(await c.post(f"/payments/{dep['ref']}/verify", headers=a), 403, "maker_checker")
    _err(await c.post(f"/payments/{dep['ref']}/verify", headers=b), 422, "evidence_required")
    _err(await c.post(f"/payments/{dep['ref']}/verify", headers=sales), 403, "forbidden")
    up = await _evidence(c, a, dep["id"])
    detail = await _ok(await c.get(f"/payments/{dep['ref']}", headers=b))
    assert detail["evidence_file_id"] == up["id"] and detail["actions"]["verify"] and not detail["needs_evidence"]
    # AA-996: the payment page's Customer and Related trip cards (e-mail / phone with customer.read).
    assert detail["customer"]["code"].startswith("CUS-") and detail["customer"]["email"] == inv["bill_to_email"]
    assert detail["trip_case"]["code"] == sold["code"] and detail["trip_case"]["status"] == "invoice_sent"
    assert "start_date" in detail["trip_case"] and detail["trip_case"]["channel"] == "b2c"
    verified = await _ok(await c.post(f"/payments/{dep['ref']}/verify", headers=b))
    assert verified["state"] == "verified" and verified["verified_by"]["id"] == ADMIN_B
    assert (await _ok(await c.get(f"/invoices/{number}", headers=sales)))["status"] == "partially_paid"
    assert await _case_status(admin, sold["code"]) == "payment_pending"
    _err(await c.post(f"/ops-files/{up['id']}/remove", json={"reason": "x"}, headers=a), 409, "evidence_locked")

    # Balance (cash, no evidence) -> paid.
    bal = await _ok(
        await c.post(
            f"/invoices/{number}/payments",
            json={"amount": "1680", "method": "cash", "received_on": date.today().isoformat()},
            headers=_as(cis, SALES, "payment-key-2"),
        ),
        201,
    )
    await _ok(await c.post(f"/payments/{bal['ref']}/verify", headers=a))
    paid = await _ok(await c.get(f"/invoices/{number}", headers=sales))
    assert (paid["status"], paid["balance"], paid["net_paid"]) == ("paid", "0.00", "2400.00")
    assert [s["state"] for s in paid["schedule"]] == ["paid", "paid"]
    assert await _case_status(admin, sold["code"]) == "paid"
    checks = await _ok(await c.get(f"/trip-cases/{sold['code']}/checks", headers=sales))
    assert next(d for d in checks["derived"] if d["code"] == "payment_verified")["status"] == "done"

    # Void the balance -> back to payment_pending (sales may not void a verified payment).
    _err(
        await c.post(f"/payments/{bal['ref']}/void", json={"reason": "counted twice"}, headers=sales), 403, "forbidden"
    )
    voided = await _ok(await c.post(f"/payments/{bal['ref']}/void", json={"reason": "counted twice"}, headers=b))
    assert voided["state"] == "voided" and voided["void_reason"] == "counted twice"
    assert (await _ok(await c.get(f"/invoices/{number}", headers=sales)))["status"] == "partially_paid"
    assert await _case_status(admin, sold["code"]) == "payment_pending"
    _err(await c.post(f"/payments/{bal['ref']}/verify", headers=b), 409, "payment_voided")
    # Statuses never set by hand.
    _err(await c.post(f"/trip-cases/{sold['code']}/status", json={"to_status": "paid"}, headers=sales), 409,
         "status_system_managed")  # fmt: skip

    # The audit rows never carry the reference.
    audit = await admin.fetchval(
        "SELECT after::text FROM booking.audit_logs WHERE action = 'payment.recorded' AND entity_id = $1::uuid",
        dep["id"],
    )
    assert "VCB" not in audit and "(set)" in audit


async def test_reference_rules_amend_remind_and_cancel(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    a, b, sales = _as(cis, ADMIN_A), _as(cis, ADMIN_B), _as(cis, SALES)
    sup = await ops.supply(admin, "Golf")
    sold = await ops.sold_case(admin, sup, tag="s3b")
    inv = await _ok(
        await c.post(
            f"/trip-cases/{sold['code']}/invoices",
            json={
                "adjustments": [{"description": "Airport transfer", "unit_price": "100", "reason": "guest asked"}],
                "schedule": [
                    {"kind": "deposit", "amount": "1000", "due_date": (date.today() + timedelta(days=2)).isoformat()},
                    {"kind": "final", "amount": "1500", "due_date": (ops.START - timedelta(days=30)).isoformat()},
                ],
            },
            headers=_as(cis, SALES, "inv-key-b"),
        ),
        201,
    )
    number = inv["invoice_number"]
    assert inv["status"] == "draft" and inv["total_amount"] == "2500.00" and len(inv["items"]) == 2
    _err(await c.post(f"/invoices/{number}/payments", json={"amount": "1", "method": "cash",
                                                          "received_on": date.today().isoformat()},
                      headers=_as(cis, SALES, "payment-key-draft")), 409, "invoice_not_payable")  # fmt: skip
    # Draft edit: new adjustments without a schedule -> the default schedule of the new total.
    edited = await _ok(await c.patch(f"/invoices/{number}", json={"adjustments": []}, headers=sales))
    assert edited["total_amount"] == "2400.00" and [s["amount"] for s in edited["schedule"]] == ["720.00", "1680.00"]
    _err(await c.patch(f"/invoices/{number}", json={"schedule": [{"kind": "final", "amount": "10",
                                                                     "due_date": "2027-01-01"}]}, headers=sales),
         422, "schedule_mismatch")  # fmt: skip
    await _ok(await c.post(f"/invoices/{number}/send", headers=sales))
    assert await _case_status(admin, sold["code"]) == "invoice_sent"
    _err(await c.patch(f"/invoices/{number}", json={"adjustments": []}, headers=sales), 409, "invoice_sent")

    today = date.today().isoformat()
    card = {"amount": "720", "method": "card_offline", "received_on": today, "reference": "4111 1111 1111 1111"}
    _err(await c.post(f"/invoices/{number}/payments", json=card, headers=_as(cis, SALES, "payment-key-card")), 422,
         "card_number_not_allowed")  # fmt: skip
    first = {"amount": "720", "method": "bank_transfer", "received_on": today, "reference": "TX-77"}
    p1 = await _ok(
        await c.post(f"/invoices/{number}/payments", json=first, headers=_as(cis, SALES, "payment-key-b1")), 201
    )
    _err(await c.post(f"/invoices/{number}/payments", json={**first, "amount": "10"},
                      headers=_as(cis, SALES, "payment-key-b2")), 409, "duplicate_reference")  # fmt: skip
    dup = await _ok(
        await c.post(
            f"/invoices/{number}/payments",
            json={**first, "amount": "10", "allow_duplicate_reference": True},
            headers=_as(cis, SALES, "payment-key-b2"),
        ),
        201,
    )
    refund_body = {"amount": "5", "kind": "refund", "method": "cash", "received_on": today}
    _err(await c.post(f"/invoices/{number}/payments", json=refund_body, headers=_as(cis, SALES, "payment-key-r")),
         409, "refund_exceeds_paid")  # fmt: skip
    # Cancel refused while a payment waits; edit before verify only.
    _err(await c.post(f"/invoices/{number}/cancel", json={"reason": "x"}, headers=sales), 409, "invoice_has_payments")
    edited_p = await _ok(await c.patch(f"/payments/{p1['ref']}", json={"note": "first part"}, headers=sales))
    assert edited_p["changed"] == ["note"]
    await _ok(await c.post(f"/payments/{dup['ref']}/void", json={"reason": "duplicate"}, headers=sales))
    await _evidence(c, sales, p1["id"])
    await _ok(await c.post(f"/payments/{p1['ref']}/verify", headers=a))
    _err(await c.patch(f"/payments/{p1['ref']}", json={"note": "x"}, headers=sales), 409, "payment_locked")

    # Amendment: +100 lands on the uncovered row; the deposit (covered) stays.
    amended = await _ok(
        await c.post(
            f"/invoices/{number}/amend",
            json={"adjustments": [{"description": "Extra night", "unit_price": "100", "reason": "guest asked"}]},
            headers=sales,
        )
    )
    assert amended["total_amount"] == "2500.00" and [s["amount"] for s in amended["schedule"]] == ["720.00", "1780.00"]
    assert amended["status"] == "partially_paid" and len(amended["items"]) == 2
    # The amendment window (bk-028 invoices.amending) is closed again in the same transaction; then money moves.
    assert await admin.fetchval("SELECT amending FROM booking.invoices WHERE id = $1::uuid", inv["id"]) is False
    # Outside an amendment the API never writes a sent invoice's lines or schedule; the database refuses it too.
    with pytest.raises(asyncpg.CheckViolationError, match="schedule changes only inside an amendment"):
        await admin.execute(
            "UPDATE booking.payment_schedule SET due_date = due_date + 1 WHERE invoice_id = $1::uuid AND seq = 2",
            inv["id"],
        )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.notifications WHERE type = 'invoice.updated' AND entity_id = $1::uuid",
            inv["id"],
        )
        == 1
    )

    # Remind: one customer row a day.
    r1 = await _ok(await c.post(f"/invoices/{number}/remind", headers=sales))
    r2 = await _ok(await c.post(f"/invoices/{number}/remind", headers=sales))
    assert (r1["reminded"], r2["reminded"]) == (True, False)

    # Refund everything, then cancel: the case steps back to confirmed.
    refund = await _ok(
        await c.post(
            f"/invoices/{number}/payments",
            json={"kind": "refund", "amount": "720", "method": "cash", "received_on": today},
            headers=_as(cis, SALES, "payment-key-refund"),
        ),
        201,
    )
    await _ok(await c.post(f"/payments/{refund['ref']}/verify", headers=b))
    assert await _case_status(admin, sold["code"]) == "invoice_sent"
    # Net 0: voiding the verified receipt would leave -720 (review S253): void the refund first.
    _err(await c.post(f"/payments/{p1['ref']}/void", json={"reason": "bounced"}, headers=b), 409,
         "void_would_overdraw")  # fmt: skip
    cancelled = await _ok(await c.post(f"/invoices/{number}/cancel", json={"reason": "trip changed"}, headers=sales))
    assert cancelled["status"] == "cancelled"
    assert await _case_status(admin, sold["code"]) == "confirmed"
    # A cancelled invoice keeps net verified paid 0: its payments are final, and the UI offers no void.
    for ref in (p1["ref"], refund["ref"]):
        _err(await c.post(f"/payments/{ref}/void", json={"reason": "undo"}, headers=b), 409, "invoice_closed")
        assert not (await _ok(await c.get(f"/payments/{ref}", headers=b)))["actions"]["void"]
    history = await _ok(await c.get(f"/trip-cases/{sold['code']}/billing", headers=sales))
    assert history["invoiceable"] and [h["invoice_number"] for h in history["history"]] == [number]


async def test_case_cancel_cascade_reopen_b2b_and_permissions(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    sales, contracting, creator = _as(cis, SALES), _as(cis, CONTRACTING), _as(cis, CREATOR)
    sup = await ops.supply(admin, "Hotel")
    sold = await ops.sold_case(admin, sup, tag="s3c")
    draft = await _ok(
        await c.post(f"/trip-cases/{sold['code']}/invoices", json={}, headers=_as(cis, SALES, "inv-key-c")), 201
    )
    # Permissions: contracting and content creators read no billing; the money badge is hidden from contracting.
    for headers in (contracting, creator):
        _err(await c.get("/invoices", headers=headers), 403, "forbidden")
        _err(await c.get(f"/invoices/{draft['invoice_number']}", headers=headers), 403, "forbidden")
    _err(await c.get("/billing/summary", headers=contracting), 403, "forbidden")
    assert (await _ok(await c.get(f"/trip-cases/{sold['code']}", headers=contracting)))["money"] is None
    badge = (await _ok(await c.get(f"/trip-cases/{sold['code']}", headers=sales)))["money"]
    assert badge["invoice_number"] == draft["invoice_number"] and badge["status"] == "draft"
    # Lists and KPIs.
    listed = await _ok(await c.get("/invoices", params={"status": "draft", "q": sold["code"]}, headers=sales))
    assert [i["invoice_number"] for i in listed["items"]] == [draft["invoice_number"]]
    # The list rows carry the same actions as the detail (the staff list's row menu).
    detail = await _ok(await c.get(f"/invoices/{draft['invoice_number']}", headers=sales))
    assert listed["items"][0]["actions"] == detail["actions"] and listed["items"][0]["actions"]["cancel"] is False
    kpis = await _ok(await c.get("/billing/summary", headers=sales))
    assert kpis["invoices"]["draft"] >= 1 and {"outstanding", "overdue", "received_this_month"} <= set(kpis)
    assert (await _ok(await c.get("/payments", params={"state": "recorded"}, headers=sales)))["total"] >= 0
    # Cancelling the case deletes the draft; reopen then works (nothing live).
    await _ok(
        await c.post(
            f"/trip-cases/{sold['code']}/status", json={"to_status": "cancelled", "reason": "lost"}, headers=sales
        )
    )
    _err(await c.get(f"/invoices/{draft['invoice_number']}", headers=sales), 404, "invoice_not_found")
    await _ok(
        await c.post(f"/trip-cases/{sold['code']}/status", json={"to_status": "new_inquiry", "reason": "back"},
                     headers=sales)
    )  # fmt: skip

    # Reopen refused while a sent invoice is live.
    other = await ops.sold_case(admin, sup, tag="s3d")
    await _ok(
        await c.post(
            f"/trip-cases/{other['code']}/invoices", json={"send_now": True}, headers=_as(cis, SALES, "inv-key-d")
        ),
        201,
    )
    await _ok(
        await c.post(f"/trip-cases/{other['code']}/status", json={"to_status": "cancelled", "reason": "lost"},
                     headers=sales)
    )  # fmt: skip
    _err(
        await c.post(f"/trip-cases/{other['code']}/status", json={"to_status": "new_inquiry", "reason": "back"},
                     headers=sales),
        409,
        "live_invoice",
    )  # fmt: skip
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.notifications WHERE type = 'invoice.case_cancelled'"
            " AND data ->> 'case_code' = $1",
            other["code"],
        )
        == 1
    )

    # B2B: no invoice.
    b2b = await ops.b2b_case(admin, tag="s3-b2b")
    out = await _ok(await c.get(f"/trip-cases/{b2b['code']}/billing", headers=sales))
    assert (out["invoiceable"], out["reason"]) == (False, "b2b_no_invoice")
    _err(await c.post(f"/trip-cases/{b2b['code']}/invoices", json={}, headers=_as(cis, SALES, "inv-key-e")), 409,
         "b2b_no_invoice")  # fmt: skip


async def test_overdue_reminder_once_per_key(client: Client, admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    c, cis = client
    sup = await ops.supply(admin, "India")
    sold = await ops.sold_case(admin, sup, tag="s3e")
    await ops.staff(admin, (SALES, "sales"))
    await admin.execute(
        "INSERT INTO booking.staff_assignments (trip_case_id, staff_user_id, role, assigned_by)"
        " VALUES ($1::uuid, $2, 'sales', $2)",
        sold["id"],
        SALES,
    )
    today = await admin.fetchval(
        "SELECT (now() AT TIME ZONE coalesce((SELECT value #>> '{}' FROM booking.settings"
        " WHERE key = 'b2b_ops_timezone'), 'Asia/Ho_Chi_Minh'))::date"
    )
    inv = await _ok(
        await c.post(
            f"/trip-cases/{sold['code']}/invoices",
            json={
                "send_now": True,
                "schedule": [
                    {"kind": "deposit", "amount": "720", "due_date": (today - timedelta(days=2)).isoformat()},
                    {"kind": "final", "amount": "1680", "due_date": (today + timedelta(days=3)).isoformat()},
                ],
            },
            headers=_as(cis, SALES, "inv-key-f"),
        ),
        201,
    )
    first = await lifecycle.run_reminders(pool)
    assert first["payments_overdue"] >= 1 and first["payments_due_soon"] >= 1
    rows = await admin.fetch(
        "SELECT recipient_type, type, recipient_ref, data ->> 'reminder_key' AS k FROM booking.notifications"
        " WHERE entity_id = $1::uuid AND type IN ('invoice.overdue', 'payment.due_soon') ORDER BY type, recipient_type",
        inv["id"],
    )
    assert [(r["type"], r["recipient_type"]) for r in rows] == [
        ("invoice.overdue", "customer"),
        ("invoice.overdue", "staff"),
        ("payment.due_soon", "customer"),
        ("payment.due_soon", "staff"),
    ]
    assert rows[1]["recipient_ref"] == SALES and rows[1]["k"].endswith(":d1")
    second = await lifecycle.run_reminders(pool)
    after = await admin.fetchval(
        "SELECT count(*) FROM booking.notifications WHERE entity_id = $1::uuid"
        " AND type IN ('invoice.overdue', 'payment.due_soon')",
        inv["id"],
    )
    assert after == 4 and second["payments_notified"] == 0 and second["customer_rows"] == 0
    # Day 7 is the next step: one more pair.
    await admin.execute("SET ROLE bk_owner")
    try:
        await admin.execute("ALTER TABLE booking.payment_schedule DISABLE TRIGGER USER")
        await admin.execute(
            "UPDATE booking.payment_schedule SET due_date = due_date - 6 WHERE invoice_id = $1::uuid AND seq = 1",
            inv["id"],
        )
    finally:
        await admin.execute("ALTER TABLE booking.payment_schedule ENABLE TRIGGER USER")
        await admin.execute("RESET ROLE")
    third = await lifecycle.run_reminders(pool)
    assert third["payments_notified"] == 1 and third["customer_rows"] == 1
