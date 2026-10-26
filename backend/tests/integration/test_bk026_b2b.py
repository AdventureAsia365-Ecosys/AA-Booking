"""AA-682 S1 (bk-026) on a B2B trip case end to end (design D12, D13; critic B2): the supplier booking and the B2B desk
are one action both ways, and the agency's cancel still works once supplier bookings moved the case.

- generate links the room item held ``on_request`` to its hold line; the desk then refuses to answer that line (409
  ``answer_from_suppliers_tab``) and lists the booking; no voucher or document on a B2B case (422);
- request moves the case to supplier_confirming; confirm accepts the line in the same transaction: ``line.confirmed``
  in the agency's ``/events``; the line is answered once, so staff cannot cancel or re-request the booking (409
  ``b2b_line_answered``);
- critic B2: ``POST /bookings/{id}/cancel`` on a supplier_confirming case answers 200, cancels the case, keeps the
  confirmed booking and tells staff to cancel it with the supplier; then staff may cancel it;
- decline declines the line (``line.declined``), the case goes back to confirmed, the declined item needs no supply
  (state none) and gets no replacement (409 ``b2b_line_answered``; generate makes nothing).

Reuses the AA-973 / AA-974 / AA-975 fixtures and helpers (a plan with a room and a transfer, a partner bound to the CRM
client, the seeded cancellation policy)."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from tests.integration import ops
from tests.integration.test_bk020_b2b import (  # noqa: F401 — fixtures admin, client, plan are used by name
    SALES,
    _m2m,
    _ok,
    _staff,
    admin,
    client,
    plan,
)
from tests.integration.test_bk021_b2b import rules  # noqa: F401 — fixture used by name
from tests.integration.test_bk022_b2b import _book, _feed, _room
from tests.staff_tokens import LocalCis, LocalM2M

FIXTURES = (admin, client, plan, rules)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Client = tuple[httpx.AsyncClient, LocalCis, LocalM2M, Database]


@pytest_asyncio.fixture
async def b2b(admin: asyncpg.Connection, rules: dict[str, str]) -> AsyncIterator[dict[str, str]]:  # noqa: F811
    try:
        yield rules
    finally:
        await ops.purge_ops(admin)
        async with admin.transaction():
            owner = await admin.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
            await admin.execute(f'SET LOCAL ROLE "{owner}"')
            await admin.execute("DELETE FROM booking.outbox")
            await admin.execute("DELETE FROM booking.notifications WHERE type = 'b2b.on_request'")


def _err(response: httpx.Response, status: int, detail: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail, response.json()
    return response.json()


async def _status(admin: asyncpg.Connection, case_id: str) -> str:  # noqa: F811
    return await admin.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case_id)


async def _events(c: httpx.AsyncClient, m2m: LocalM2M, booking_id: str) -> list[tuple[str, str]]:
    page = await _feed(c, m2m)
    return [(e["type"], e.get("decline_code") or "") for e in page["items"] if e.get("booking_id") == booking_id]


async def test_confirm_answers_the_line_and_agency_cancel_still_works(
    client: Client,  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    b2b: dict[str, str],
) -> None:
    c, cis, m2m, _ = client
    sales = _staff(cis, SALES)
    booking = await _book(c, m2m, "SB-1", [_room(b2b["room"])])
    case_id, hold_id = booking["booking_id"], booking["hold_id"]
    made = await _ok(await c.post(f"/trip-cases/{case_id}/supplier-bookings/generate", headers=sales))
    assert made["created"] == 1 and made["documents_allowed"] is False
    sb = made["items"][0]
    assert sb["b2b_line"] == {"hold_id": hold_id, "line_ref": "room", "status": "on_request"}

    # The desk refuses a line covered by a live supplier booking, and names it.
    desk_url = f"/b2b-desk/on-request/{hold_id}/lines/room/accept"
    _err(await c.post(desk_url, headers=sales), 409, "answer_from_suppliers_tab")
    open_lines = await _ok(await c.get("/b2b-desk/on-request", headers=sales))
    assert next(i for i in open_lines["items"] if i["hold_id"] == hold_id)["supplier_booking"] == sb["code"]

    # DM-6.8: no voucher or document on a B2B case.
    doc = {
        "kind": "supplier_voucher",
        "entity_type": "supplier_booking",
        "entity_id": sb["id"],
        "file_name": "v.pdf",
        "content_type": "application/pdf",
        "data_base64": base64.b64encode(b"%PDF-1.7\n%%EOF\n").decode(),
    }
    _err(await c.post("/ops-files", json=doc, headers=sales), 422, "document_not_allowed_b2b")

    await _ok(await c.post(f"/supplier-bookings/{sb['id']}/request", headers=sales))
    assert await _status(admin, case_id) == "supplier_confirming"
    voucher = {"confirmation_ref": "HN-55", "voucher_file_id": "00000000-0000-4000-8000-000000000001"}
    _err(await c.post(f"/supplier-bookings/{sb['id']}/confirm", json=voucher, headers=sales), 422,
         "document_not_allowed_b2b")  # fmt: skip
    done = await _ok(await c.post(f"/supplier-bookings/{sb['id']}/confirm", json={"confirmation_ref": "HN-55"},
                                  headers=sales))  # fmt: skip
    assert done["status"] == "confirmed" and done["b2b_line"]["status"] == "confirmed"
    assert await _events(c, m2m, case_id) == [("line.confirmed", "")]
    page = await _ok(await c.get(f"/trip-cases/{case_id}/supplier-bookings", headers=sales))
    assert page["supply"]["state"] == "booked"

    # Answered once: staff cannot cancel or re-request it.
    _err(await c.post(f"/supplier-bookings/{sb['id']}/cancel", json={"reason": "x"}, headers=sales), 409,
         "b2b_line_answered")  # fmt: skip
    _err(await c.post(f"/supplier-bookings/{sb['id']}/rerequest", json={"reason": "x"}, headers=sales), 409,
         "b2b_line_answered")  # fmt: skip

    # Critic B2: the agency cancels a supplier_confirming booking.
    cancel_url = f"/b2b/v1/bookings/{case_id}/cancel"
    cancelled = await c.post(cancel_url, json={"reason_code": "client_cancelled"}, headers=_m2m(m2m, "sb-cancel-key-1"))
    assert cancelled.status_code == 200, cancelled.text
    assert await _status(admin, case_id) == "cancelled"
    assert await admin.fetchval("SELECT status FROM booking.supplier_bookings WHERE id = $1::uuid", sb["id"]) == (
        "confirmed"
    )
    note = await admin.fetchval(
        "SELECT data->>'supplier_bookings' FROM booking.notifications"
        " WHERE type = 'supplier_booking.cancel_with_supplier' AND entity_id = $1::uuid",
        case_id,
    )
    assert sb["code"] in note
    audit = await admin.fetchrow(
        "SELECT before->>'status' AS before FROM booking.audit_logs WHERE action = 'b2b_booking.cancelled'"
        " AND entity_id = $1::uuid",
        case_id,
    )
    assert audit["before"] == "supplier_confirming"
    # The line is cancelled now: staff may record the supplier cancel.
    gone = await _ok(await c.post(f"/supplier-bookings/{sb['id']}/cancel", json={"reason": "cancelled with hotel"},
                                  headers=sales))  # fmt: skip
    assert gone["status"] == "cancelled"


async def test_decline_answers_the_line_and_is_final(
    client: Client,  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    b2b: dict[str, str],
) -> None:
    c, cis, m2m, _ = client
    sales = _staff(cis, SALES)
    booking = await _book(c, m2m, "SB-2", [_room(b2b["room"])])
    case_id = booking["booking_id"]
    made = await _ok(await c.post(f"/trip-cases/{case_id}/supplier-bookings/generate", headers=sales))
    sb = made["items"][0]
    await _ok(await c.post(f"/supplier-bookings/{sb['id']}/request", headers=sales))
    out = await _ok(
        await c.post(f"/supplier-bookings/{sb['id']}/decline", json={"decline_code": "sold_out"}, headers=sales)
    )
    assert out["status"] == "declined" and out["b2b_line"]["status"] == "declined"
    assert await _events(c, m2m, case_id) == [("line.declined", "sold_out")]
    assert await _status(admin, case_id) == "confirmed"
    page = await _ok(await c.get(f"/trip-cases/{case_id}/supplier-bookings", headers=sales))
    assert page["supply"]["state"] == "none" and page["supply"]["items_to_book"] == 0
    item = await admin.fetchval(
        "SELECT version_item_id::text FROM booking.supplier_bookings WHERE id = $1::uuid", sb["id"]
    )
    body = {"supplier_id": sb["supplier"]["id"], "version_item_id": item}
    _err(await c.post(f"/trip-cases/{case_id}/supplier-bookings", json=body, headers=sales), 409, "b2b_line_answered")
    assert (await _ok(await c.post(f"/trip-cases/{case_id}/supplier-bookings/generate", headers=sales)))["created"] == 0


async def test_confirm_racing_the_agency_cancel_never_deadlocks(
    client: Client,  # noqa: F811
    admin: asyncpg.Connection,  # noqa: F811
    b2b: dict[str, str],
) -> None:
    """Lock order hold → case → booking on both paths: a staff confirm and the agency's cancel of the same booking run
    one after the other. Either the confirm wins (the cancel then cancels a supplier_confirming case) or the cancel
    wins (the confirm finds the line cancelled: 409); never a 500, never a confirmed line on a cancelled booking."""
    c, cis, m2m, _ = client
    sales = _staff(cis, SALES)
    booking = await _book(c, m2m, "SB-3", [_room(b2b["room"])])
    case_id = booking["booking_id"]
    sb = (await _ok(await c.post(f"/trip-cases/{case_id}/supplier-bookings/generate", headers=sales)))["items"][0]
    await _ok(await c.post(f"/supplier-bookings/{sb['id']}/request", headers=sales))
    confirm, cancel = await asyncio.gather(
        c.post(f"/supplier-bookings/{sb['id']}/confirm", json={"confirmation_ref": "R-1"}, headers=sales),
        c.post(
            f"/b2b/v1/bookings/{case_id}/cancel",
            json={"reason_code": "client_cancelled"},
            headers=_m2m(m2m, "sb-race-cancel-1"),
        ),
    )
    assert cancel.status_code == 200, cancel.text
    assert confirm.status_code in (200, 409), confirm.text
    assert await _status(admin, case_id) == "cancelled"
    line = await admin.fetchval(
        "SELECT status FROM booking.b2b_hold_lines WHERE hold_id = $1::uuid AND line_ref = 'room'", booking["hold_id"]
    )
    assert line == "cancelled"
    final = await admin.fetchval("SELECT status FROM booking.supplier_bookings WHERE id = $1::uuid", sb["id"])
    assert final == ("confirmed" if confirm.status_code == 200 else "requested")
