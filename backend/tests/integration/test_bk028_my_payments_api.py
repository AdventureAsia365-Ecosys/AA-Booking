"""AA-682 S5: My Payments over HTTP on a real PostgreSQL (bk-024 identities + bk-028 invoices), as role ``booking``.

Done-when of the slice (design §2 S5, §10): a signed-in traveller sees only their own invoices — another customer's
number is 404, a draft is hidden, an archived customer or an unlinked sign-in sees nothing — with the schedule chips,
the verified payments only (never a recorded or voided one) and the payment instructions while money is owed; no
staff name, evidence, net, supplier or internal note in any answer; staff and traveller tokens never cross route
classes. The Python branches are in ``tests/unit/test_aa682_my_payments.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.main import create_app
from tests.integration import ops
from tests.staff_tokens import EXTERNAL_ISSUER, LocalCis, LocalExternal
from tests.unit.test_aa682_my_payments import FORBIDDEN, keys_of

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER, CHECKER = "it-682-s5-maker", "it-682-s5-checker"
SUB_A = "a1a1a1a1-0000-4000-8000-0000000000a1"
SUB_B = "b2b2b2b2-0000-4000-8000-0000000000b2"
SUB_NOBODY = "c3c3c3c3-0000-4000-8000-0000000000c3"

Client = tuple[httpx.AsyncClient, LocalExternal, LocalCis]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (MAKER, "admin"), (CHECKER, "admin"))
    try:
        yield conn
    finally:
        await ops.purge_ops(conn)
        await conn.close()


@pytest_asyncio.fixture
async def app_conn(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_booking_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def client(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[Client]:
    ext, cis = LocalExternal(), LocalCis()
    settings = ext.settings(database_url=migrated_booking_dsn, db_ssl="disable", git_sha="ci")
    db = Database(settings)
    app = create_app(settings, db, cis.verifier(), customer_verifier=ext.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, ext, cis
    finally:
        await db.close()


async def _link(admin: asyncpg.Connection, case: dict[str, Any], sub: str) -> str:
    """Attach a sign-in identity to the case's customer (what the AA-908 intake does for a signed-in hand-off)."""
    customer = await admin.fetchval("SELECT customer_id::text FROM booking.trip_cases WHERE id = $1::uuid", case["id"])
    await admin.execute(
        "INSERT INTO booking.customer_identities (iss, sub, customer_id) VALUES ($1, $2, $3::uuid)",
        EXTERNAL_ISSUER,
        sub,
        customer,
    )
    return customer


def _as(ext: LocalExternal, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ext.token(sub=sub)}"}


async def _ok(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    bad = sorted(k for k in keys_of(body) if FORBIDDEN.search(k))
    assert bad == [], bad
    return body


def _not_found(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "invoice_not_found"


async def test_own_invoices_only(client: Client, admin: asyncpg.Connection, app_conn: asyncpg.Connection) -> None:
    c, ext, _ = client
    sup = await ops.supply(admin, "Sierra")
    case_a = await ops.sold_case(admin, sup, tag="s5a")
    case_b = await ops.sold_case(admin, sup, tag="s5b")
    customer_a = await _link(admin, case_a, SUB_A)
    await _link(admin, case_b, SUB_B)
    a, b = _as(ext, SUB_A), _as(ext, SUB_B)

    # A draft is never shown.
    inv_a = await ops.draft_invoice(app_conn, case_a, by=MAKER, tag="s5a")
    assert (await _ok(await c.get("/customer/v1/payments", headers=a))) == {"invoices": []}
    _not_found(await c.get(f"/customer/v1/invoices/{inv_a['invoice_number']}", headers=a))

    # Sent; deposit verified (shown), a second receipt recorded only (not shown), a third voided (not shown).
    await ops.send_invoice(app_conn, inv_a["id"], by=MAKER)
    deposit = await ops.record_payment(app_conn, inv_a["id"], "720", by=MAKER, tag="s5a-dep")
    await ops.verify_payment(app_conn, deposit, by=CHECKER)
    await ops.record_payment(app_conn, inv_a["id"], "100", by=MAKER, tag="s5a-waiting")
    voided = await ops.record_payment(app_conn, inv_a["id"], "50", by=MAKER, tag="s5a-void")
    await app_conn.execute(
        "UPDATE booking.payments SET voided_by = $2, voided_at = now(), void_reason = 'typo' WHERE id = $1::uuid",
        voided,
        CHECKER,
    )
    inv_b = await ops.draft_invoice(app_conn, case_b, by=MAKER, tag="s5b")
    await ops.send_invoice(app_conn, inv_b["id"], by=MAKER)

    listed = await _ok(await c.get("/customer/v1/payments", headers=a))
    (mine,) = listed["invoices"]
    assert mine["invoice_number"] == inv_a["invoice_number"]
    assert (mine["status"], mine["total_amount"], mine["paid_amount"], mine["balance"]) == (
        "partially_paid",
        "2400.00",
        "720.00",
        "1680.00",
    )
    assert mine["trip"]["code"] == case_a["code"] and mine["trip"]["planner_trip_id"] is None
    assert mine["trip"]["start_date"] == ops.START.isoformat()
    assert [(s["kind"], s["state"]) for s in mine["schedule"]] == [("deposit", "paid"), ("final", "scheduled")]
    assert mine["next_due_date"] == (ops.START - timedelta(days=30)).isoformat()
    assert [(p["kind"], p["amount"], p["method"]) for p in mine["payments"]] == [("receipt", "720.00", "cash")]
    assert mine["payment_instructions"]  # the bk-028 placeholder text while money is owed
    detail = await _ok(await c.get(f"/customer/v1/invoices/{inv_a['invoice_number'].lower()}", headers=a))
    assert detail == mine

    # Another customer's number: 404, both ways; each sees exactly one invoice.
    _not_found(await c.get(f"/customer/v1/invoices/{inv_b['invoice_number']}", headers=a))
    _not_found(await c.get(f"/customer/v1/invoices/{inv_a['invoice_number']}", headers=b))
    theirs = await _ok(await c.get("/customer/v1/payments", headers=b))
    assert [i["invoice_number"] for i in theirs["invoices"]] == [inv_b["invoice_number"]]

    # An unlinked sign-in sees nothing.
    nobody = _as(ext, SUB_NOBODY)
    assert (await _ok(await c.get("/customer/v1/payments", headers=nobody))) == {"invoices": []}
    _not_found(await c.get(f"/customer/v1/invoices/{inv_a['invoice_number']}", headers=nobody))

    # Cancelled (net paid 0): still listed, no bank details, no next due date, rows "cancelled".
    await app_conn.execute(
        "UPDATE booking.invoices SET status = 'cancelled', cancelled_at = now(), cancelled_by = $2,"
        " cancel_reason = 'trip dropped' WHERE id = $1::uuid",
        inv_b["id"],
        MAKER,
    )
    (cancelled,) = (await _ok(await c.get("/customer/v1/payments", headers=b)))["invoices"]
    assert (cancelled["status"], cancelled["payment_instructions"], cancelled["next_due_date"]) == (
        "cancelled",
        None,
        None,
    )
    assert {s["state"] for s in cancelled["schedule"]} == {"cancelled"}

    # An archived customer: the identity no longer opens anything.
    await admin.execute("UPDATE booking.customers SET deleted_at = now() WHERE id = $1::uuid", customer_a)
    assert (await _ok(await c.get("/customer/v1/payments", headers=a))) == {"invoices": []}
    _not_found(await c.get(f"/customer/v1/invoices/{inv_a['invoice_number']}", headers=a))


async def test_route_classes_never_cross(client: Client, admin: asyncpg.Connection) -> None:
    c, ext, cis = client
    staff = {"Authorization": f"Bearer {cis.token(sub=MAKER, cis_role='admin')}"}
    workforce = {"Authorization": f"Bearer {cis.workforce.token(groups=['booking-admin'])}"}
    for headers in ({}, staff, workforce):
        r = await c.get("/customer/v1/payments", headers=headers)
        assert r.status_code == 401 and r.json() == {"detail": "sign_in_required"}, r.text
    # A traveller token is not a staff token.
    r = await c.get("/invoices", headers=_as(ext, SUB_A))
    assert r.status_code == 401, r.text


async def test_paid_invoice_hides_instructions(
    client: Client, admin: asyncpg.Connection, app_conn: asyncpg.Connection
) -> None:
    c, ext, _ = client
    sup = await ops.supply(admin, "Tango")
    case = await ops.sold_case(admin, sup, tag="s5c")
    await _link(admin, case, SUB_A)
    inv = await ops.draft_invoice(
        app_conn, case, by=MAKER, tag="s5c", schedule=(("final", "2400.00", date.today() - timedelta(days=2)),)
    )
    await ops.send_invoice(app_conn, inv["id"], by=MAKER)
    (overdue,) = (await _ok(await c.get("/customer/v1/payments", headers=_as(ext, SUB_A))))["invoices"]
    assert [s["state"] for s in overdue["schedule"]] == ["overdue"] and overdue["overdue_amount"] == "2400.00"
    full = await ops.record_payment(app_conn, inv["id"], "2400", by=MAKER, tag="s5c-full")
    await ops.verify_payment(app_conn, full, by=CHECKER)
    (paid,) = (await _ok(await c.get("/customer/v1/payments", headers=_as(ext, SUB_A))))["invoices"]
    assert (paid["status"], paid["balance"], paid["payment_instructions"]) == ("paid", "0.00", None)
    assert [s["state"] for s in paid["schedule"]] == ["paid"]
