"""AA-1159 My work over HTTP on a real PostgreSQL, as role ``booking``.

- Each role gets the tiles of its capabilities (never by person); a role with none (content creator) is 403.
- Every tile's count equals the total of the list it opens, read through that list's own API with the same filter
  (the band and the page share one WHERE): my open cases, overdue trip checks, on-request lines, supplier bills overdue
  / due soon / to approve / to pay, agency invoices overdue, agency payments to approve.
- The fixtures make the bill tiles non-zero and different from each other (an overdue, a due-soon and a later bill,
  one of them approved), and give one person an open case.
- The approval tiles count only what the caller may act on (maker-checker): the maker of every bill sees none to
  approve, the checker sees them all; stop-sales the caller entered or asked to end are left out too.
"""

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
from app.settings import Settings
from tests.integration import ops
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MAKER, CHECKER, ADMIN, FINANCE, CREATOR = (
    "it-1159-maker",
    "it-1159-checker",
    "it-1159-admin",
    "it-1159-finance",
    "it-1159-creator",
)
CIS_ROLE = {MAKER: "reviewer", CHECKER: "reviewer", ADMIN: "admin", FINANCE: "reviewer", CREATOR: "content"}
Client = tuple[httpx.AsyncClient, LocalCis]

WORK_TILES = ("my_open_cases", "trip_checks_overdue", "b2b_on_request", "supplier_bills_overdue",
              "supplier_bills_due_soon", "agency_invoices_overdue")  # fmt: skip
APPROVE_TILES = (
    "supplier_bills_to_approve",
    "supplier_bills_to_pay",
    "agency_payments_to_approve",
    "stop_sales_to_review",
    "suppliers_to_activate",  # AA-1158
    "billing_changes_to_approve",
)
OVERSIGHT_TILES = ("owner_self_approvals",)  # AA-1158: every audit.read holder (group work, listed last)


async def _purge_stop_sales(conn: asyncpg.Connection) -> None:
    """The contracts and stop-sales of this module's suppliers (stop-sales are never deleted by the app: guard off)."""
    async with conn.transaction():
        owner = await conn.fetchval("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'booking'")
        if owner != await conn.fetchval("SELECT current_user"):
            await conn.execute(f'SET LOCAL ROLE "{owner}"')
        mine = (
            "SELECT k.id FROM booking.supplier_contracts k JOIN booking.suppliers s ON s.id = k.supplier_id"
            " WHERE s.company_name LIKE 'it-682 %'"
        )
        await conn.execute("ALTER TABLE booking.supply_restrictions DISABLE TRIGGER USER")
        await conn.execute(f"DELETE FROM booking.supply_restrictions WHERE supplier_contract_id IN ({mine})")  # noqa: S608
        await conn.execute("ALTER TABLE booking.supply_restrictions ENABLE TRIGGER USER")
        await conn.execute("DELETE FROM booking.audit_logs WHERE entity_type = 'supply_restriction'")
        await conn.execute(f"DELETE FROM booking.supplier_contracts WHERE id IN ({mine})")  # noqa: S608


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await _purge_stop_sales(conn)
    await ops.purge_ops(conn)
    await ops.staff(
        conn,
        (MAKER, "contracting"),
        (CHECKER, "contracting"),
        (ADMIN, "admin"),
        (FINANCE, "finance"),
        (CREATOR, "content_creator"),
    )
    try:
        yield conn
    finally:
        await _purge_stop_sales(conn)
        await ops.purge_ops(conn)
        await conn.close()


@pytest_asyncio.fixture
async def client(admin: asyncpg.Connection, migrated_booking_dsn: str) -> AsyncIterator[Client]:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking/booking") as c:
            yield c, cis
    finally:
        await db.close()


def _as(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role=CIS_ROLE[sub])}"}


async def _ok(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


async def _seed(conn: asyncpg.Connection) -> dict[str, str]:
    """An open case of MAKER with an overdue check; four bills of one supplier: overdue, due tomorrow, due in a month
    (received) and one approved."""
    case = await ops.b2b_case(conn, tag="1159")
    await conn.execute(
        "INSERT INTO booking.staff_assignments (trip_case_id, staff_user_id, role, assigned_by)"
        " VALUES ($1::uuid, $2, 'sales', $3)",
        case["id"],
        MAKER,
        ADMIN,
    )
    await conn.execute(
        "INSERT INTO booking.trip_case_checks (trip_case_id, code, title, due_date, created_by)"
        " VALUES ($1::uuid, 'custom', 'Call the invented river lodge', current_date - 2, $2)",
        case["id"],
        MAKER,
    )
    sup = await ops.supply(conn, "Kestrel")
    today = date.today()
    late = await ops.bill(conn, sup["supplier"], by=MAKER, number="KS-1", tag="1159-a")
    await conn.execute(  # issued ten days ago, due three days ago (a bill's due date is never before its issue date)
        "UPDATE booking.supplier_bills SET issue_date = current_date - 10, due_date = current_date - 3"
        " WHERE id = $1::uuid",
        late["id"],
    )
    await ops.bill(conn, sup["supplier"], by=MAKER, number="KS-2", due=today + timedelta(days=1), tag="1159-b")
    await ops.bill(conn, sup["supplier"], by=MAKER, number="KS-3", due=today + timedelta(days=30), tag="1159-c")
    booking = await ops.confirmed_booking(conn, case, sup["supplier"], net="400.00")
    approved = await ops.bill(
        conn,
        sup["supplier"],
        by=MAKER,
        number="KS-4",
        due=today + timedelta(days=20),
        tag="1159-d",
        lines=((booking, "400.00"),),
    )
    await ops.approve_bill(conn, approved["id"], by=CHECKER)
    return sup


async def _stop_sales(c: httpx.AsyncClient, cis: LocalCis, admin: asyncpg.Connection, sup: dict[str, str]) -> None:
    """On a published contract of the supplier: a pending stop-sale of MAKER, one of CHECKER, and one of MAKER that
    CHECKER approved and MAKER asked to end."""
    k = await admin.fetchval(
        "INSERT INTO booking.supplier_contracts (supplier_id, title, valid_from, valid_to, currency, status,"
        " created_by, published_by, published_at) VALUES ($1::uuid, 'Invented 2027 rates', '2027-01-01',"
        " '2027-12-31', 'USD', 'published', $2, $3, now()) RETURNING id::text",
        sup["supplier"],
        MAKER,
        CHECKER,
    )

    async def stop(who: str, day: str) -> str:
        body = {"component_id": f"sv:{sup['service']}", "date_from": day, "date_to": day, "note": "Invented closure"}
        r = await c.post(f"/contracts/{k}/stop-sales", json=body, headers=_as(cis, who))
        assert r.status_code == 201, r.text
        return r.json()["id"]

    await stop(MAKER, "2027-03-01")
    await stop(CHECKER, "2027-03-02")
    ended = await stop(MAKER, "2027-03-03")
    await _ok(
        await c.post(f"/supply/stop-sales/{ended}/decision", json={"action": "approve"}, headers=_as(cis, CHECKER))
    )
    await _ok(
        await c.post(f"/supply/stop-sales/{ended}/end-request", json={"reason": "Reopened"}, headers=_as(cis, MAKER))
    )


def _tiles(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["key"]: t for t in body["tiles"]}


async def _list_total(c: httpx.AsyncClient, headers: dict[str, str], key: str, tile: dict[str, Any]) -> int:
    """The total the tile's target list shows for the tile's filter, through that list's own API."""
    f = tile["filter"]
    if key == "my_open_cases":
        assert f["tab"] == "mine"
        body = await _ok(
            await c.get("/trip-cases", params={"assignee": "me", "status": f["status"], "limit": 1}, headers=headers)
        )
        return body["total"]
    if key == "trip_checks_overdue":
        assert f == {"due": "overdue"}
        body = await _ok(await c.get("/trip-checks", params={"limit": 1}, headers=headers))
        return body["counts"]["overdue"]  # the Overdue tab's badge
    if key == "b2b_on_request":
        assert f == {}
        body = await _ok(await c.get("/b2b-desk/on-request", params={"state": "open", "limit": 1}, headers=headers))
        return body["total"]
    if key.startswith("supplier_bills_"):
        params: dict[str, Any] = {"status": f["bill_status"], "limit": 1}
        if "approvable" in f:
            params["approvable"] = f["approvable"]
        if f.get("bill_due") == "overdue":
            params["overdue"] = "true"
        if f.get("bill_due") == "soon":
            params["due_soon"] = "true"
        body = await _ok(await c.get("/supplier-bills", params=params, headers=headers))
        return body["total"]
    if key == "agency_invoices_overdue":
        body = await _ok(await c.get("/agency-invoices", params={"status": f["status"], "limit": 1}, headers=headers))
        return body["total"]
    if key == "agency_payments_to_approve":
        assert f == {"tab": "payments", "approvable": "me"}
        params = {"state": "recorded", "approvable": "me", "limit": 1}
        body = await _ok(await c.get("/agency-payments", params=params, headers=headers))
        return body["total"]
    if key == "stop_sales_to_review":
        assert f == {"review": "me"}
        params = {"kind": "stop_sale", "review": "me", "limit": 1}
        body = await _ok(await c.get("/supply/restrictions", params=params, headers=headers))
        return body["total"]
    if key == "suppliers_to_activate":
        assert f == {"status": "pending", "approvable": "me"}
        body = await _ok(await c.get("/suppliers", params={**f, "limit": 1}, headers=headers))
        return body["total"]
    if key == "billing_changes_to_approve":
        assert f == {"tab": "settings", "approvable": "me"}
        me = (await _ok(await c.get("/staff/me", headers=headers)))["cis_user_id"]
        entities = (await _ok(await c.get("/billing-entities", headers=headers)))["items"]
        return sum(1 for e in entities if e.get("pending_change") and e["pending_change"]["requested_by"] != me)
    if key == "owner_self_approvals":
        assert f["owner_self_approved"] == "true" and f["from"].endswith("-01")
        body = await _ok(await c.get("/audit-logs", params={**f, "limit": 200}, headers=headers))
        return len(body["items"])
    raise AssertionError(f"no list for tile {key}")


async def test_tiles_follow_capabilities_and_equal_their_lists(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    sup = await _seed(admin)
    await _stop_sales(c, cis, admin, sup)

    # The administrator holds every tile capability.
    body = await _ok(await c.get("/my-work/summary", headers=_as(cis, ADMIN)))
    tiles = _tiles(body)
    assert list(tiles) == [*WORK_TILES, *APPROVE_TILES, *OVERSIGHT_TILES]
    assert all(t["available"] for t in tiles.values()), tiles
    assert {t["group"] for k, t in tiles.items() if k in APPROVE_TILES} == {"approve"}

    # Contracting (Product & operations): no payouts, no agency billing.
    tiles = _tiles(await _ok(await c.get("/my-work/summary", headers=_as(cis, MAKER))))
    assert list(tiles) == [
        "my_open_cases",
        "trip_checks_overdue",
        "b2b_on_request",
        "supplier_bills_overdue",
        "supplier_bills_due_soon",
        "supplier_bills_to_approve",
        "stop_sales_to_review",
        "suppliers_to_activate",
    ]

    # Finance: reads cases, operations and bills; agency billing and its payment approvals; no bill approval.
    tiles = _tiles(await _ok(await c.get("/my-work/summary", headers=_as(cis, FINANCE))))
    assert list(tiles) == [
        "my_open_cases",
        "trip_checks_overdue",
        "supplier_bills_overdue",
        "supplier_bills_due_soon",
        "agency_invoices_overdue",
        "agency_payments_to_approve",
        "billing_changes_to_approve",
    ]

    # A content creator has no queue here.
    r = await c.get("/my-work/summary", headers=_as(cis, CREATOR))
    assert r.status_code == 403 and r.json()["detail"] == "forbidden", r.text

    # Every tile equals its target list, for every caller.
    for who in (ADMIN, MAKER, FINANCE):
        headers = _as(cis, who)
        for key, tile in _tiles(await _ok(await c.get("/my-work/summary", headers=headers))).items():
            assert tile["count"] == await _list_total(c, headers, key, tile), (who, key, tile)

    # The fixture numbers: mine is per person; the bill tiles are told apart.
    mine = _tiles(await _ok(await c.get("/my-work/summary", headers=_as(cis, MAKER))))
    other = _tiles(await _ok(await c.get("/my-work/summary", headers=_as(cis, CHECKER))))
    assert mine["my_open_cases"]["count"] == 1 and other["my_open_cases"]["count"] == 0
    assert mine["my_open_cases"]["oldest_at"] is not None
    assert mine["trip_checks_overdue"]["count"] >= 1
    assert mine["trip_checks_overdue"]["earliest_due"] <= (date.today() - timedelta(days=2)).isoformat()
    assert mine["supplier_bills_overdue"]["count"] == 1
    assert mine["supplier_bills_overdue"]["earliest_due"] == (date.today() - timedelta(days=3)).isoformat()
    soon = mine["supplier_bills_due_soon"]
    assert soon["count"] == 1 and soon["days"] >= 1
    assert soon["earliest_due"] == (date.today() + timedelta(days=1)).isoformat()
    # Maker-checker: MAKER entered the three received bills and can approve none; CHECKER can approve all three.
    assert mine["supplier_bills_to_approve"]["count"] == 0
    assert other["supplier_bills_to_approve"]["count"] == 3
    assert _tiles(body)["supplier_bills_to_pay"]["count"] == 1
    everyone = await _ok(await c.get("/supplier-bills", params={"status": "received"}, headers=_as(cis, MAKER)))
    assert everyone["total"] == 3  # the list without the filter still shows every bill
    # Stop-sales: MAKER may decide only CHECKER's pending one; CHECKER may decide MAKER's pending one and the end
    # MAKER asked for. Three wait in all.
    assert mine["stop_sales_to_review"]["count"] == 1 and other["stop_sales_to_review"]["count"] == 2
    assert other["stop_sales_to_review"]["oldest_at"] is not None
    waiting = await _ok(await c.get("/supply/restrictions", params={"review": "waiting"}, headers=_as(cis, FINANCE)))
    assert waiting["total"] == 3 and {r["supplier_name"] for r in waiting["items"]} == {"it-682 Kestrel"}
    assert [r["status"] for r in waiting["items"]].count("approved") == 1
    asked = next(r for r in waiting["items"] if r["status"] == "approved")
    assert asked["end_requested_by"] == MAKER and asked["created_by_name"]
