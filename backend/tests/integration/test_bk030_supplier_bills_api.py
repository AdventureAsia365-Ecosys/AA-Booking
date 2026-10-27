"""AA-682 S4 over HTTP on a real PostgreSQL (bk-030), as role ``booking`` — the slice's Done-when, end to end:

- a bill matched to two confirmed supplier bookings is entered (Idempotency-Key replay returns the same bill, another
  body is 409), its document uploaded;
- approve by the person who entered it, or by the person who last changed its amounts or lines → 403
  ``maker_checker``; by another with lines ≠ amount → 409
  ``bill_not_ready``; fixed while received, then approved; its facts are then locked (notes stay editable);
- a payout = the balance → ``paid`` (a partial one first leaves it ``approved``); the same Idempotency-Key twice → one
  payout; above the balance → 409; card-like references refused; the bill's creator never pays it (403);
- payout evidence: attached, the current one of a live payout is never removed; voiding a payout → ``approved``; a
  bill with live payouts is not cancelled; cancel with a reason (never "Delete");
- the case cost vs billed panel (net with ``pricing.read``), lists, filters, KPIs, billable bookings;
- permissions: sales reads only, content creators read nothing, contracting does not pay;
- ``bk_ops_reminders`` writes ``supplier_bill.due`` once per bill and step.

The database rules themselves (direct ``UPDATE … SET status = 'paid'`` refused, concurrent payouts) are in
``test_bk030_schema.py``."""

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

ENTERER, CHECKER, ADMIN, ADMIN_2, SALES, CREATOR = (
    "it-682-bill-enterer",
    "it-682-bill-checker",
    "it-682-bill-admin",
    "it-682-bill-admin-2",
    "it-682-bill-sales",
    "it-682-bill-creator",
)
CIS_ROLE = {
    ENTERER: "reviewer",
    CHECKER: "reviewer",
    ADMIN: "admin",
    ADMIN_2: "admin",
    SALES: "reviewer",
    CREATOR: "content",
}
BUCKET = "aa-booking-dev-ops-docs-000000000000"
PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


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
        (ENTERER, "contracting"),
        (CHECKER, "contracting"),
        (ADMIN, "admin"),
        (ADMIN_2, "admin"),
        (SALES, "sales"),
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


@pytest_asyncio.fixture
async def world(admin: asyncpg.Connection) -> dict[str, Any]:
    sup = await ops.supply(admin, "Gamma")
    case = await ops.sold_case(admin, sup, tag="bk030-api")
    room = await ops.confirmed_booking(admin, case, sup["supplier"], item=case["room_item"])
    kayak = await ops.confirmed_booking(admin, case, sup["supplier"], item=case["service_item"], net="80.00")
    return {"sup": sup, "case": case, "room": room, "kayak": kayak}


def _as(cis: LocalCis, sub: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {cis.token(sub=sub, cis_role=CIS_ROLE[sub])}"}
    if key:
        headers["Idempotency-Key"] = f"it-682-{key}"  # 8-200 characters
    return headers


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _err(response: httpx.Response, status: int, detail: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail, response.json()
    return response.json()


def _bill_body(world: dict[str, Any], **over: Any) -> dict[str, Any]:
    today = date.today()
    body = {
        "supplier_id": world["sup"]["supplier"],
        "trip_case": world["case"]["code"],
        "bill_number": "GAMMA-2026-0042",
        "currency": "USD",
        "amount": "380.00",
        "issue_date": today.isoformat(),
        "due_date": (today + timedelta(days=20)).isoformat(),
        "notes": "Rooms and kayak",
        "lines": [
            {"supplier_booking_id": world["room"], "amount": "300.00"},
            {"supplier_booking_id": world["kayak"], "amount": "80.00"},
        ],
    }
    body.update(over)
    return body


async def _upload(
    c: httpx.AsyncClient, headers: dict[str, str], kind: str, entity_type: str, entity_id: str, salt: str
) -> httpx.Response:
    body = {
        "kind": kind,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "file_name": f"{kind}.pdf",
        "content_type": "application/pdf",
        "data_base64": base64.b64encode(PDF + salt.encode()).decode(),
    }
    return await c.post("/ops-files", json=body, headers=headers)


async def test_done_when_sequence(client: Client, admin: asyncpg.Connection, world: dict[str, Any]) -> None:
    c, cis = client
    enterer, checker, payer = _as(cis, ENTERER), _as(cis, CHECKER), _as(cis, ADMIN)

    billable = await _ok(
        await c.get("/supplier-bills/billable", params={"supplier_id": world["sup"]["supplier"]}, headers=enterer)
    )
    assert {i["id"] for i in billable["items"]} == {world["room"], world["kayak"]}
    assert all(i["billed"] == [] for i in billable["items"])
    assert {i["net_amount"] for i in billable["items"]} == {"300.00", "80.00"}

    # Enter: Idempotency-Key required, replay = same bill, another body = 409.
    _err(await c.post("/supplier-bills", json=_bill_body(world), headers=enterer), 422, "idempotency_key_required")
    bill = await _ok(await c.post("/supplier-bills", json=_bill_body(world), headers=_as(cis, ENTERER, "bill-k1")), 201)
    assert bill["status"] == "received" and bill["code"].startswith("BIL-") and bill["replayed"] is False
    assert len(bill["lines"]) == 2 and bill["approve_blockers"] == ["maker_checker"]
    again = await _ok(await c.post("/supplier-bills", json=_bill_body(world), headers=_as(cis, ENTERER, "bill-k1")))
    assert again["id"] == bill["id"] and again["replayed"] is True
    _err(
        await c.post("/supplier-bills", json=_bill_body(world, amount="381.00"), headers=_as(cis, ENTERER, "bill-k1")),
        409,
        "idempotency_conflict",
    )
    _err(
        await c.post("/supplier-bills", json=_bill_body(world), headers=_as(cis, ENTERER, "bill-k2")),
        409,
        "duplicate_bill_number",
    )
    billed = await _ok(
        await c.get("/supplier-bills/billable", params={"supplier_id": world["sup"]["supplier"]}, headers=enterer)
    )
    assert {i["id"]: i["billed"] for i in billed["items"]}[world["room"]] == [{"currency": "USD", "amount": "300.00"}]

    # The supplier's document, while received.
    doc = await _ok(await _upload(c, enterer, "supplier_bill", "supplier_bill", bill["id"], "doc"), 201)
    detail = await _ok(await c.get(f"/supplier-bills/{bill['code']}", headers=checker))
    assert detail["document_file_id"] == doc["id"] and [f["id"] for f in detail["files"]] == [doc["id"]]
    assert detail["actions"]["approve"] is True and detail["approve_blockers"] == []

    # Approve: maker-checker, then lines must add up.
    _err(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=enterer), 403, "maker_checker")
    changed = await _ok(await c.patch(f"/supplier-bills/{bill['id']}", json={"amount": "400.00"}, headers=enterer))
    assert changed["changed"] == ["amount"] and changed["approve_blockers"] == ["maker_checker", "lines_mismatch"]
    _err(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=checker), 409, "bill_not_ready")
    await _ok(await c.patch(f"/supplier-bills/{bill['id']}", json={"amount": "380.00"}, headers=enterer))
    approved = await _ok(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=checker))
    assert approved["status"] == "approved" and approved["approved_by"]["id"] == CHECKER
    assert (await _ok(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=checker)))["status"] == "approved"
    _err(
        await c.patch(f"/supplier-bills/{bill['id']}", json={"amount": "1.00"}, headers=enterer),
        409,
        "supplier_bill_locked",
    )
    await _ok(await c.patch(f"/supplier-bills/{bill['id']}", json={"notes": "checked"}, headers=enterer))
    _err(await _upload(c, enterer, "supplier_bill", "supplier_bill", bill["id"], "late"), 409, "supplier_bill_closed")

    # Payouts: admin only, never the creator, never above the balance, no card numbers, Idempotency-Key.
    pay = {"amount": "200.00", "method": "bank_transfer", "paid_on": date.today().isoformat(), "reference": "TT-001"}
    _err(
        await c.post(f"/supplier-bills/{bill['id']}/payouts", json=pay, headers=_as(cis, CHECKER, "po-0")),
        403,
        "forbidden",
    )
    _err(
        await c.post(f"/supplier-bills/{bill['id']}/payouts", json=pay, headers=payer), 422, "idempotency_key_required"
    )
    _err(
        await c.post(
            f"/supplier-bills/{bill['id']}/payouts", json={**pay, "amount": "380.01"}, headers=_as(cis, ADMIN, "po-x")
        ),
        409,
        "payout_exceeds_balance",
    )
    _err(
        await c.post(
            f"/supplier-bills/{bill['id']}/payouts",
            json={**pay, "reference": "4111 1111 1111 1111"},
            headers=_as(cis, ADMIN, "po-card"),
        ),
        422,
        "card_number_not_allowed",
    )
    first = await _ok(
        await c.post(f"/supplier-bills/{bill['id']}/payouts", json=pay, headers=_as(cis, ADMIN, "po-1")), 201
    )
    assert first["code"].startswith("SPO-") and first["needs_evidence"] is True
    assert first["bill"]["status"] == "approved" and first["bill"]["balance"] == "180.00"
    replay = await _ok(await c.post(f"/supplier-bills/{bill['id']}/payouts", json=pay, headers=_as(cis, ADMIN, "po-1")))
    assert replay["id"] == first["id"] and replay["replayed"] is True
    _err(
        await c.post(
            f"/supplier-bills/{bill['id']}/payouts", json={**pay, "amount": "10.00"}, headers=_as(cis, ADMIN, "po-dup")
        ),
        409,
        "duplicate_reference",
    )
    rest = {**pay, "amount": "180.00", "reference": "TT-002"}
    second = await _ok(
        await c.post(f"/supplier-bills/{bill['id']}/payouts", json=rest, headers=_as(cis, ADMIN, "po-2")), 201
    )
    assert second["bill"]["status"] == "paid" and second["bill"]["balance"] == "0.00"
    assert second["bill"]["paid_at"] is not None

    # Evidence: attached; the current evidence of a live payout stays.
    slip = await _ok(await _upload(c, payer, "payout_evidence", "supplier_payout", second["id"], "slip"), 201)
    _err(await _upload(c, enterer, "payout_evidence", "supplier_payout", second["id"], "x"), 403, "forbidden")
    _err(
        await c.post(f"/ops-files/{slip['id']}/remove", json={"reason": "wrong"}, headers=payer), 409, "evidence_locked"
    )
    _err(
        await c.post(f"/ops-files/{doc['id']}/remove", json={"reason": "wrong"}, headers=enterer),
        409,
        "document_locked",
    )
    download = await _ok(await c.get(f"/ops-files/{slip['id']}/download", headers=_as(cis, SALES)))
    assert download["expires_in"] == 300

    # Cancel refused with payouts; void steps back.
    _err(
        await c.post(f"/supplier-bills/{bill['id']}/cancel", json={"reason": "x"}, headers=enterer),
        409,
        "bill_has_payouts",
    )
    voided = await _ok(
        await c.post(f"/supplier-payouts/{second['code']}/void", json={"reason": "bank returned it"}, headers=payer)
    )
    assert voided["state"] == "voided" and voided["bill"]["status"] == "approved"
    assert voided["bill"]["balance"] == "180.00" and voided["bill"]["paid_at"] is None
    again_void = await _ok(await c.post(f"/supplier-payouts/{second['id']}/void", json={"reason": "x"}, headers=payer))
    assert again_void["voided_at"] == voided["voided_at"]

    # Case cost vs billed: net next to billed, variance 0.
    costs = await _ok(await c.get(f"/trip-cases/{world['case']['code']}/supplier-costs", headers=enterer))
    by_code = {line["supplier_booking"]["id"]: line for line in costs["lines"]}
    assert by_code[world["room"]]["net_amount"] == "300.00" and by_code[world["room"]]["variance"] == "0.00"
    assert costs["totals"] == [{"currency": "USD", "net": "380.00", "billed": "380.00", "variance": "0.00"}]

    audit = await admin.fetch(
        "SELECT action FROM booking.audit_logs WHERE entity_type IN ('supplier_bill', 'supplier_payout')"
        " ORDER BY created_at"
    )
    assert {r["action"] for r in audit} >= {
        "supplier_bill.created",
        "supplier_bill.updated",
        "supplier_bill.approved",
        "supplier_payout.recorded",
        "supplier_payout.voided",
        "ops_file.uploaded",
    }


async def test_maker_checker_on_payouts_and_cancel(client: Client, world: dict[str, Any]) -> None:
    c, cis = client
    body = _bill_body(
        world, bill_number="GAMMA-ADMIN-1", lines=[{"supplier_booking_id": world["room"], "amount": "380.00"}]
    )
    bill = await _ok(await c.post("/supplier-bills", json=body, headers=_as(cis, ADMIN, "bill-admin")), 201)
    await _ok(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=_as(cis, CHECKER)))
    pay = {"amount": "380.00", "method": "cash", "paid_on": date.today().isoformat()}
    _err(
        await c.post(f"/supplier-bills/{bill['id']}/payouts", json=pay, headers=_as(cis, ADMIN, "po-self")),
        403,
        "maker_checker",
    )
    detail = await _ok(await c.get(f"/supplier-bills/{bill['id']}", headers=_as(cis, ADMIN_2)))
    assert detail["actions"]["pay"] is True
    assert (await _ok(await c.get(f"/supplier-bills/{bill['id']}", headers=_as(cis, ADMIN))))["actions"]["pay"] is False
    # Cancel an approved bill with no payout; then it is final.
    cancelled = await _ok(
        await c.post(
            f"/supplier-bills/{bill['id']}/cancel", json={"reason": "entered twice"}, headers=_as(cis, CHECKER)
        )
    )
    assert cancelled["status"] == "cancelled" and cancelled["cancel_reason"] == "entered twice"
    _err(
        await c.patch(f"/supplier-bills/{bill['id']}", json={"notes": "x"}, headers=_as(cis, CHECKER)),
        409,
        "supplier_bill_closed",
    )
    _err(
        await c.post(f"/supplier-bills/{bill['id']}/payouts", json=pay, headers=_as(cis, ADMIN_2, "po-closed")),
        409,
        "bill_not_payable",
    )


async def test_maker_checker_follows_the_last_editor(client: Client, world: dict[str, Any]) -> None:
    """Review S4: contracting A enters 380 USD; contracting B changes the amount and a line to 5000 USD, then approves
    → 403 ``maker_checker``. A third person (an admin) approves; B's numbers are checked by someone else."""
    c, cis = client
    body = _bill_body(world, bill_number="GAMMA-EDIT-1")
    bill = await _ok(await c.post("/supplier-bills", json=body, headers=_as(cis, ENTERER, "bill-edit-1")), 201)
    assert bill["facts_edited_by"]["id"] == ENTERER
    checker = _as(cis, CHECKER)
    noted = await _ok(await c.patch(f"/supplier-bills/{bill['id']}", json={"notes": "seen"}, headers=checker))
    assert noted["facts_edited_by"]["id"] == ENTERER and noted["approve_blockers"] == []  # notes are not money
    edited = await _ok(
        await c.patch(
            f"/supplier-bills/{bill['id']}",
            json={
                "amount": "5000.00",
                "lines": [
                    {"supplier_booking_id": world["room"], "amount": "4920.00"},
                    {"supplier_booking_id": world["kayak"], "amount": "80.00"},
                ],
            },
            headers=checker,
        )
    )
    assert edited["changed"] == ["amount", "lines"] and edited["facts_edited_by"]["id"] == CHECKER
    assert edited["approve_blockers"] == ["maker_checker"] and edited["actions"]["approve"] is False
    _err(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=checker), 403, "maker_checker")
    # A lines-only edit by the enterer makes the enterer the last editor again; the checker may then approve.
    back = await _ok(
        await c.patch(
            f"/supplier-bills/{bill['id']}",
            json={
                "lines": [
                    {"supplier_booking_id": world["room"], "amount": "4900.00"},
                    {"supplier_booking_id": world["kayak"], "amount": "100.00"},
                ]
            },
            headers=_as(cis, ENTERER),
        )
    )
    assert back["changed"] == ["lines"] and back["facts_edited_by"]["id"] == ENTERER
    _err(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=_as(cis, ENTERER)), 403, "maker_checker")
    approved = await _ok(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=checker))
    assert approved["status"] == "approved" and approved["approved_by"]["id"] == CHECKER
    # An approved bill still offers Edit (its notes only).
    assert approved["actions"]["edit"] is True


async def test_lines_permissions_lists_and_summary(
    client: Client, admin: asyncpg.Connection, world: dict[str, Any]
) -> None:
    c, cis = client
    sales, creator = _as(cis, SALES), _as(cis, CREATOR)
    _err(
        await c.post("/supplier-bills", json=_bill_body(world), headers=_as(cis, SALES, "bill-sales")), 403, "forbidden"
    )
    _err(await c.get("/supplier-bills", headers=creator), 403, "forbidden")
    # A line that names a booking that is not confirmed (or of another supplier) is refused by the database rule.
    pending = await admin.fetchval(
        "INSERT INTO booking.supplier_bookings (trip_case_id, supplier_id, item_description) VALUES ($1::uuid,"
        " $2::uuid, 'Extra') RETURNING id::text",
        world["case"]["id"],
        world["sup"]["supplier"],
    )
    bad = _bill_body(world, bill_number="BAD-1", lines=[{"supplier_booking_id": pending, "amount": "380.00"}])
    _err(await c.post("/supplier-bills", json=bad, headers=_as(cis, ENTERER, "bill-bad")), 409, "invalid_bill_line")
    assert await admin.fetchval("SELECT count(*) FROM booking.supplier_bills WHERE bill_number = 'BAD-1'") == 0
    # A statement bill without a case, lines replaced by PATCH.
    stmt = _bill_body(world, bill_number="STMT-9", trip_case=None, amount="80.00", lines=[])
    bill = await _ok(await c.post("/supplier-bills", json=stmt, headers=_as(cis, ENTERER, "bill-stmt")), 201)
    assert bill["trip_case"] is None and bill["approve_blockers"] == ["maker_checker", "no_lines"]
    patched = await _ok(
        await c.patch(
            f"/supplier-bills/{bill['id']}",
            json={"lines": [{"supplier_booking_id": world["kayak"], "amount": "80.00"}]},
            headers=_as(cis, ENTERER),
        )
    )
    assert patched["changed"] == ["lines"] and patched["line_count"] == 1
    # Lists, filters and KPIs (sales reads).
    listed = await _ok(await c.get("/supplier-bills", params={"supplier_id": world["sup"]["supplier"]}, headers=sales))
    assert listed["total"] == 1 and listed["items"][0]["code"] == bill["code"]
    assert (await _ok(await c.get("/supplier-bills", params={"case": world["case"]["code"]}, headers=sales)))[
        "total"
    ] == 1
    assert (await _ok(await c.get("/supplier-bills", params={"status": "paid"}, headers=sales)))["total"] == 0
    assert (await _ok(await c.get("/supplier-bills", params={"q": "stmt-9"}, headers=sales)))["total"] == 1
    summary = await _ok(await c.get("/supplier-bills/summary", headers=sales))
    assert summary["to_approve"] == {"count": 1, "by_currency": [{"currency": "USD", "amount": "80.00"}]}
    assert summary["bills"]["received"] == 1
    _err(await c.post(f"/supplier-bills/{bill['id']}/approve", headers=sales), 403, "forbidden")
    # A bill touching a B2B case takes no document.
    b2b = await ops.b2b_case(admin, tag="bk030-api-b2b")
    agency = await ops.confirmed_booking(admin, b2b, world["sup"]["supplier"])
    b2b_bill = await _ok(
        await c.post(
            "/supplier-bills",
            json=_bill_body(
                world,
                bill_number="B2B-1",
                trip_case=None,
                amount="50.00",
                lines=[{"supplier_booking_id": agency, "amount": "50.00"}],
            ),
            headers=_as(cis, ENTERER, "bill-b2b"),
        ),
        201,
    )
    _err(
        await _upload(c, _as(cis, ENTERER), "supplier_bill", "supplier_bill", b2b_bill["id"], "b2b"),
        422,
        "document_not_allowed_b2b",
    )


async def test_due_reminders_once_per_step(
    client: Client, pool: asyncpg.Pool, admin: asyncpg.Connection, world: dict[str, Any]
) -> None:
    c, cis = client
    soon = (date.today() + timedelta(days=1)).isoformat()
    body = _bill_body(world, bill_number="DUE-1", due_date=soon)
    bill = await _ok(await c.post("/supplier-bills", json=body, headers=_as(cis, ENTERER, "bill-due")), 201)
    first = await lifecycle.run_reminders(pool)
    assert first["bills_due"] >= 1 and first["bills_notified"] >= 1
    second = await lifecycle.run_reminders(pool)
    assert second["bills_notified"] == 0
    rows = await admin.fetch(
        "SELECT title, data->>'reminder_key' AS key, recipient_ref FROM booking.notifications"
        " WHERE type = 'supplier_bill.due' AND entity_id = $1::uuid",
        bill["id"],
    )
    assert len(rows) == 1 and rows[0]["key"].endswith(":soon") and rows[0]["recipient_ref"] is None
    assert "approve" in rows[0]["title"]
