"""AA-682 S1 over HTTP on a real PostgreSQL (bk-026), as role ``booking``, B2C path (B2B: ``test_bk026_b2b.py``):

- generate from the approved version: one pending booking per supply item (B2C room nights = the pricing rule, net = the
  sum of the cost rows; mixed currencies → 409), idempotent, and two concurrent generates make no duplicate;
- request → the case moves confirmed → supplier_confirming by itself; confirm with the supplier's ref (+ a voucher on
  a B2C case) → supply state ``booked`` ("Confirmed & Booked"), the case stays supplier_confirming (money is S3);
- staff can never set a post-sale status by hand (409 ``status_system_managed``, critic B1);
- decline → a replacement with another supplier is allowed (critic B3); a second live booking of one item is 409;
- cancelling every live booking takes the case back to confirmed;
- staff cancel of the case: pending bookings cancelled, requested / confirmed ones stay + one "cancel with supplier"
  notification; reopen refused while a live booking exists (critic B9);
- documents: upload sniffs the bytes, 503 without a bucket, download is a presigned link and audited, remove is final;
- permissions: content creators get 403; the dashboard summary and the supplier Bookings list read the same rows.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from app.settings import Settings
from tests.integration import ops
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SALES, CONTRACTING, CREATOR = "it-682-sales", "it-682-contracting", "it-682-creator"
BUCKET = "aa-booking-dev-ops-docs-000000000000"
PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class FakeOpsStore:
    bucket = BUCKET

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str, sha256: str) -> None:
        self.objects[key] = (data, content_type)

    async def download_url(self, key: str, file_name: str, content_type: str) -> str:
        return f"https://signed.example/{key}?name={file_name}"


Client = tuple[httpx.AsyncClient, LocalCis, FakeOpsStore]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (SALES, "sales"), (CONTRACTING, "contracting"), (CREATOR, "content_creator"))
    try:
        yield conn
    finally:
        await ops.purge_ops(conn)
        await conn.close()


def _app(dsn: str, store: FakeOpsStore | None) -> tuple[Any, Database, LocalCis]:
    settings = Settings(database_url=dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    return create_app(settings, db, cis.verifier(), ops_store=store), db, cis


@pytest_asyncio.fixture
async def client(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[Client]:
    store = FakeOpsStore()
    app, db, cis = _app(migrated_booking_dsn, store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis, store
    finally:
        await db.close()


@pytest_asyncio.fixture
async def sold(admin: asyncpg.Connection) -> dict[str, Any]:
    sup = await ops.supply(admin)
    return {**await ops.sold_case(admin, sup), "sup": sup}


def _auth(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def _err(response: httpx.Response, status: int, detail: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail, response.json()
    return response.json()


async def _status(admin: asyncpg.Connection, case_id: str) -> str:
    return await admin.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case_id)


async def test_generate_request_confirm_booked(client: Client, admin: asyncpg.Connection, sold: dict) -> None:
    c, cis, _ = client
    sales = _auth(cis, SALES)
    url = f"/trip-cases/{sold['code']}/supplier-bookings"
    empty = await _ok(await c.get(url, headers=sales))
    assert empty["items"] == [] and empty["supply"]["state"] == "confirming"
    assert [i["position"] for i in empty["unbooked_items"]] == [1, 2]

    made = await _ok(await c.post(f"{url}/generate", headers=sales))
    assert made["created"] == 2
    room, service = sorted(made["items"], key=lambda b: b["item_position"])
    assert room["status"] == "pending" and room["supplier"]["id"] == sold["sup"]["supplier"]
    assert room["item_description"] == "Delta Hotel · Deluxe" and room["nights"] == 2 and room["pax"] == 2
    assert Decimal(room["net_amount"]) == Decimal("400.50") and room["net_currency"] == "USD"
    assert room["service_date"] == ops.START.isoformat()
    assert service["nights"] is None and service["service_date"] == (ops.START + timedelta(days=2)).isoformat()
    assert (await _ok(await c.post(f"{url}/generate", headers=sales)))["created"] == 0  # idempotent
    assert await _status(admin, sold["id"]) == "confirmed"  # pending bookings do not move the case

    # The first request moves the case along the supply track by itself.
    asked = await _ok(await c.post(f"/supplier-bookings/{room['id']}/request", headers=sales))
    assert asked["status"] == "requested" and asked["requested_by"]["id"] == SALES
    assert await _status(admin, sold["id"]) == "supplier_confirming"
    history = await admin.fetchrow(
        "SELECT changed_by, note FROM booking.trip_case_status_history WHERE trip_case_id = $1::uuid"
        " ORDER BY seq DESC LIMIT 1",
        sold["id"],
    )
    assert history["changed_by"] is None and history["note"].startswith("Auto: supplier booking")

    # Confirm needs the ref; a pending booking cannot be confirmed (edge).
    _err(await c.post(f"/supplier-bookings/{service['id']}/confirm", json={"confirmation_ref": "C-1"}, headers=sales),
         409, "illegal_transition")  # fmt: skip
    await _ok(await c.post(f"/supplier-bookings/{service['id']}/request", headers=sales))
    done = await _ok(
        await c.post(f"/supplier-bookings/{room['id']}/confirm", json={"confirmation_ref": "HTL-778"}, headers=sales)
    )
    assert done["status"] == "confirmed" and done["confirmation_ref"] == "HTL-778"
    again = await c.post(
        f"/supplier-bookings/{room['id']}/confirm", json={"confirmation_ref": "HTL-778"}, headers=sales
    )
    assert (await _ok(again))["confirmed_at"] == done["confirmed_at"]  # the same answer again
    await _ok(
        await c.post(f"/supplier-bookings/{service['id']}/confirm", json={"confirmation_ref": "K-9"}, headers=sales)
    )
    page = await _ok(await c.get(url, headers=sales))
    assert page["supply"]["state"] == "booked" and page["supply"]["items_booked"] == 2
    case = await _ok(await c.get(f"/trip-cases/{sold['code']}", headers=sales))
    assert case["supply"]["state"] == "booked" and case["status"] == "supplier_confirming"

    # Critic B1: no post-sale status by hand.
    for target in ("pre_departure", "paid", "on_trip", "completed"):
        _err(await c.post(f"/trip-cases/{sold['code']}/status", json={"to_status": target}, headers=sales),
             409, "status_system_managed")  # fmt: skip

    # Facts are fixed once confirmed; notes still change.
    _err(
        await c.patch(f"/supplier-bookings/{room['id']}", json={"pax": 3}, headers=sales),
        409,
        "supplier_booking_locked",
    )
    noted = await _ok(await c.patch(f"/supplier-bookings/{room['id']}", json={"notes": "late check-in"}, headers=sales))
    assert noted["changed"] == ["notes"]

    # A change with the supplier: confirmed -> requested, the old ref only in the audit row.
    re = await _ok(
        await c.post(f"/supplier-bookings/{room['id']}/rerequest", json={"reason": "3 nights"}, headers=sales)
    )
    assert re["status"] == "requested" and re["confirmation_ref"] is None
    assert (await _ok(await c.get(url, headers=sales)))["supply"]["state"] == "confirming"
    audit = await admin.fetchval(
        "SELECT before->>'confirmation_ref' FROM booking.audit_logs WHERE action = 'supplier_booking.rerequested'"
        " AND entity_id = $1::uuid",
        room["id"],
    )
    assert audit == "HTL-778"

    # Dashboard tile and the supplier Bookings tab read the same rows.
    tile = await _ok(await c.get("/supplier-bookings/summary", headers=sales))
    assert tile["requested"] >= 1 and tile["answer_days"] == 2
    tab = await _ok(await c.get(f"/supplier-bookings?supplier_id={sold['sup']['supplier']}", headers=sales))
    assert {b["id"] for b in tab["items"]} == {room["id"], service["id"]} and tab["total"] == 2


async def test_decline_replace_and_back_to_confirmed(client: Client, admin: asyncpg.Connection, sold: dict) -> None:
    c, cis, _ = client
    sales = _auth(cis, SALES)
    url = f"/trip-cases/{sold['id']}/supplier-bookings"
    made = await _ok(await c.post(f"{url}/generate", headers=sales))
    room = next(b for b in made["items"] if b["item_position"] == 1)
    service = next(b for b in made["items"] if b["item_position"] == 2)
    # One live booking per item.
    body = {"supplier_id": sold["sup"]["supplier"], "version_item_id": sold["room_item"]}
    _err(await c.post(url, json=body, headers=sales), 409, "booking_exists")
    await _ok(await c.post(f"/supplier-bookings/{room['id']}/request", headers=sales))
    bad = await c.post(f"/supplier-bookings/{room['id']}/decline", json={"decline_code": "nope"}, headers=sales)
    assert bad.status_code == 422
    declined = await _ok(
        await c.post(f"/supplier-bookings/{room['id']}/decline", json={"decline_code": "sold_out"}, headers=sales)
    )
    assert declined["status"] == "declined" and declined["live"] is False
    # Critic B3: the replacement with another supplier is allowed (its own room type: from that supplier only).
    other = await ops.supply(admin, "Echo")
    wrong = {**body, "supplier_id": other["supplier"], "hotel_room_type_id": sold["sup"]["room"]}
    _err(await c.post(url, json=wrong, headers=sales), 422, "supply_not_of_supplier")
    replacement = await _ok(
        await c.post(url, json={**body, "supplier_id": other["supplier"], "hotel_room_type_id": other["room"],
                                "item_description": "Echo Hotel · Deluxe"}, headers=sales),
        201,
    )  # fmt: skip
    assert replacement["status"] == "pending" and replacement["version_item_id"] == sold["room_item"]
    assert replacement["nights"] == 2 and Decimal(replacement["net_amount"]) == Decimal("400.50")
    # An extra item (no version item) needs a description.
    _err(await c.post(url, json={"supplier_id": other["supplier"]}, headers=sales), 422, "description_required")
    extra = await _ok(
        await c.post(url, json={"supplier_id": other["supplier"], "item_description": "Boat transfer"}, headers=sales),
        201,
    )
    await _ok(await c.post(f"/supplier-bookings/{service['id']}/request", headers=sales))
    assert await _status(admin, sold["id"]) == "supplier_confirming"
    # Cancelling every live booking takes the case back to confirmed.
    for b in (replacement, extra, service):
        await _ok(await c.post(f"/supplier-bookings/{b['id']}/cancel", json={"reason": "plan changed"}, headers=sales))
    assert await _status(admin, sold["id"]) == "confirmed"
    _err(await c.post(f"/supplier-bookings/{service['id']}/request", headers=sales), 409, "illegal_transition")


async def test_mixed_cost_currencies_refused(client: Client, admin: asyncpg.Connection) -> None:
    c, cis, _ = client
    sup = await ops.supply(admin, "Mixed")
    case = await ops.sold_case(admin, sup, tag="mixed", costs=("USD", "VND"))
    _err(
        await c.post(f"/trip-cases/{case['code']}/supplier-bookings/generate", headers=_auth(cis, SALES)),
        409,
        "mixed_cost_currencies",
    )


async def test_concurrent_generates_make_no_duplicate(
    client: Client, admin: asyncpg.Connection, sold: dict, migrated_booking_dsn: str
) -> None:
    c, cis, _ = client
    url = f"/trip-cases/{sold['code']}/supplier-bookings/generate"
    results = await asyncio.gather(*(c.post(url, headers=_auth(cis, SALES)) for _ in range(4)))
    assert [r.status_code for r in results] == [200] * 4
    assert sorted(r.json()["created"] for r in results) == [0, 0, 0, 2]
    assert await admin.fetchval(
        "SELECT count(*) FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid", sold["id"]
    ) == 2  # fmt: skip


async def test_case_cancel_cascade_and_reopen(client: Client, admin: asyncpg.Connection, sold: dict) -> None:
    c, cis, _ = client
    sales = _auth(cis, SALES)
    made = await _ok(await c.post(f"/trip-cases/{sold['code']}/supplier-bookings/generate", headers=sales))
    room = next(b for b in made["items"] if b["item_position"] == 1)
    service = next(b for b in made["items"] if b["item_position"] == 2)
    await _ok(await c.post(f"/supplier-bookings/{room['id']}/request", headers=sales))
    body = {"to_status": "cancelled", "reason": "Customer cancelled"}
    await _ok(await c.post(f"/trip-cases/{sold['code']}/status", json=body, headers=sales))
    rows = {
        r["id"]: r["status"]
        for r in await admin.fetch(
            "SELECT id::text AS id, status FROM booking.supplier_bookings WHERE trip_case_id = $1::uuid", sold["id"]
        )
    }
    assert rows == {room["id"]: "requested", service["id"]: "cancelled"}
    note = await admin.fetchrow(
        "SELECT title, data FROM booking.notifications WHERE type = 'supplier_booking.cancel_with_supplier'"
        " AND entity_id = $1::uuid",
        sold["id"],
    )
    assert note is not None and room["code"] in note["data"]
    # Bookings cannot be requested on a cancelled case; reopening waits for the supplier cancel.
    reopen = {"to_status": "new_inquiry", "reason": "Customer came back"}
    _err(await c.post(f"/trip-cases/{sold['code']}/status", json=reopen, headers=sales), 409, "live_supplier_bookings")
    await _ok(await c.post(f"/supplier-bookings/{room['id']}/cancel", json={"reason": "cancelled with hotel"},
                           headers=sales))  # fmt: skip
    await _ok(await c.post(f"/trip-cases/{sold['code']}/status", json=reopen, headers=sales))
    assert await _status(admin, sold["id"]) == "new_inquiry"


async def test_documents_upload_download_remove(client: Client, admin: asyncpg.Connection, sold: dict) -> None:
    c, cis, store = client
    sales = _auth(cis, SALES)
    made = await _ok(await c.post(f"/trip-cases/{sold['code']}/supplier-bookings/generate", headers=sales))
    room = next(b for b in made["items"] if b["item_position"] == 1)
    body = {
        "kind": "supplier_voucher",
        "entity_type": "supplier_booking",
        "entity_id": room["id"],
        "file_name": "voucher Mr X.pdf",
        "content_type": "application/pdf",
        "data_base64": base64.b64encode(PDF).decode(),
    }
    _err(await c.post("/ops-files", json={**body, "content_type": "image/png"}, headers=sales), 422,
         "content_type_mismatch")  # fmt: skip
    _err(await c.post("/ops-files", json={**body, "data_base64": base64.b64encode(b"MZ...").decode()}, headers=sales),
         422, "unsupported_file")  # fmt: skip
    _err(await c.post("/ops-files", json={**body, "kind": "payment_evidence", "entity_type": "payment"}, headers=sales),
         422, "kind_not_available")  # fmt: skip
    up = await _ok(await c.post("/ops-files", json=body, headers=sales), 201)
    assert up["content_type"] == "application/pdf" and up["bytes"] == len(PDF) and "s3_key" not in up
    key = f"ops-docs/supplier_voucher/{up['id']}.pdf"
    assert store.objects[key][0] == PDF
    _err(await c.post("/ops-files", json=body, headers=sales), 409, "file_exists")
    await _ok(await c.post(f"/supplier-bookings/{room['id']}/request", headers=sales))
    confirm = {"confirmation_ref": "HTL-1", "voucher_file_id": up["id"]}
    done = await _ok(await c.post(f"/supplier-bookings/{room['id']}/confirm", json=confirm, headers=sales))
    assert done["voucher_file_id"] == up["id"] and [f["id"] for f in done["files"]] == [up["id"]]
    link = await _ok(await c.get(f"/ops-files/{up['id']}/download", headers=sales))
    assert link == {"url": f"https://signed.example/{key}?name=voucher Mr X.pdf", "expires_in": 300}
    assert await admin.fetchval(
        "SELECT count(*) FROM booking.audit_logs WHERE action = 'ops_file.downloaded' AND entity_id = $1::uuid",
        room["id"],
    ) == 1  # fmt: skip
    removed = await _ok(await c.post(f"/ops-files/{up['id']}/remove", json={"reason": "wrong hotel"}, headers=sales))
    assert removed["status"] == "removed"
    after = await _ok(await c.get(f"/supplier-bookings/{room['id']}", headers=sales))
    assert after["voucher_file_id"] is None and after["files"] == []
    _err(await c.get(f"/ops-files/{up['id']}/download", headers=sales), 409, "file_removed")


async def test_no_bucket_is_503(admin: asyncpg.Connection, sold: dict, migrated_booking_dsn: str) -> None:
    app, db, cis = _app(migrated_booking_dsn, None)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            sales = _auth(cis, SALES)
            made = await _ok(await c.post(f"/trip-cases/{sold['code']}/supplier-bookings/generate", headers=sales))
            body = {
                "kind": "supplier_confirmation",
                "entity_type": "supplier_booking",
                "entity_id": made["items"][0]["id"],
                "file_name": "c.pdf",
                "content_type": "application/pdf",
                "data_base64": base64.b64encode(PDF).decode(),
            }
            _err(await c.post("/ops-files", json=body, headers=sales), 503, "ops_storage_unavailable")
    finally:
        await db.close()


async def test_permissions(client: Client, sold: dict) -> None:
    c, cis, _ = client
    creator = _auth(cis, CREATOR)
    assert (await c.get(f"/trip-cases/{sold['code']}/supplier-bookings", headers=creator)).status_code == 403
    assert (await c.post(f"/trip-cases/{sold['code']}/supplier-bookings/generate", headers=creator)).status_code == 403
    assert (await c.get("/supplier-bookings/summary", headers=creator)).status_code == 403
    # Contracting books suppliers too (ops.write).
    made = await c.post(f"/trip-cases/{sold['code']}/supplier-bookings/generate", headers=_auth(cis, CONTRACTING))
    assert made.status_code == 200 and made.json()["created"] == 2
