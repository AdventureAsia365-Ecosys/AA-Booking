"""AA-1159 My work over HTTP on a real PostgreSQL, as role ``booking``.

- Each role gets the tiles of its capabilities (never by person); a role with none (content creator) is 403.
- Every tile's count equals the total of the list it opens, read through that list's own API with the same filter
  (the band and the page share one WHERE): my open cases, overdue trip checks, on-request lines, supplier bills overdue
  / due soon / to approve / to pay, agency invoices overdue, agency payments to approve.
- The fixtures make the bill tiles non-zero and different from each other (an overdue, a due-soon and a later bill,
  one of them approved), and give one person an open case.
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
APPROVE_TILES = ("supplier_bills_to_approve", "supplier_bills_to_pay", "agency_payments_to_approve")


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
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


async def _seed(conn: asyncpg.Connection) -> None:
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
        assert f == {"tab": "payments"}
        body = await _ok(await c.get("/agency-payments", params={"state": "recorded", "limit": 1}, headers=headers))
        return body["total"]
    raise AssertionError(f"no list for tile {key}")


async def test_tiles_follow_capabilities_and_equal_their_lists(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    await _seed(admin)

    # The administrator holds every tile capability.
    body = await _ok(await c.get("/my-work/summary", headers=_as(cis, ADMIN)))
    tiles = _tiles(body)
    assert list(tiles) == [*WORK_TILES, *APPROVE_TILES]
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
    assert mine["supplier_bills_to_approve"]["count"] == 3
    assert _tiles(body)["supplier_bills_to_pay"]["count"] == 1
