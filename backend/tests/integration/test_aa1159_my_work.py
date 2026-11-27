"""AA-1159 My work over HTTP on a real PostgreSQL, as role ``booking``.

- Each role gets the tiles of its capabilities (never by person); a role with none (content creator) is 403.
- Every tile's count equals the total of the list it opens, read through that list's own API with the same filter
  (the band and the page share one WHERE): my open cases, overdue trip checks, on-request lines, supplier bills overdue
  / due soon / to approve / to pay, agency invoices overdue, agency payments to approve.
- The fixtures make the bill tiles non-zero and different from each other (an overdue, a due-soon and a later bill,
  one of them approved), and give one person an open case.
- The approval tiles count only what the caller may act on (maker-checker): the maker of every bill sees none to
  approve, the checker sees them all; stop-sales the caller entered or asked to end are left out too.
- AA-1190: overdue trip checks split into the caller's own and the unowned ones; the price version, add-on, season
  and holiday tiles equal their lists; the price setup card equals the price workbench's summary.
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

WORK_TILES = ("my_open_cases", "trip_checks_overdue", "trip_checks_unassigned", "b2b_on_request",
              "supplier_bills_overdue",
              "supplier_bills_due_soon", "agency_invoices_overdue",
              "contracts_ending_soon")  # fmt: skip  # AA-1177
APPROVE_TILES = (
    "supplier_bills_to_approve",
    "supplier_bills_to_pay",
    "agency_payments_to_approve",
    "stop_sales_to_review",
    "suppliers_to_activate",  # AA-1158
    "billing_changes_to_approve",
    "supplier_bank_to_approve",  # AA-1158 part 2
    "price_versions_to_approve",  # AA-1190
    "addons_to_publish",
    "season_changes_to_approve",
    "holiday_events_to_approve",
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
        # AA-1190: the season calendars of these contracts (and their ranges) and this module's holiday events
        calendars = f"SELECT id FROM booking.season_calendars WHERE supplier_contract_id IN ({mine})"  # noqa: S608
        await conn.execute(f"DELETE FROM booking.season_ranges WHERE calendar_id IN ({calendars})")  # noqa: S608
        await conn.execute(f"DELETE FROM booking.season_calendars WHERE id IN ({calendars})")  # noqa: S608
        await conn.execute("DELETE FROM booking.holiday_events WHERE code LIKE 'it1190_%'")
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
    # AA-1190: one overdue check MAKER owns (the unowned one above is the "no owner" tile's)
    await conn.execute(
        "INSERT INTO booking.trip_case_checks (trip_case_id, code, title, due_date, assignee_ref, created_by)"
        " VALUES ($1::uuid, 'custom', 'Confirm the invented ferry', current_date - 1, $2, $2)",
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
    await _seasons(c, cis, k)


async def _seasons(c: httpx.AsyncClient, cis: LocalCis, contract: str) -> None:
    """AA-1190: on a calendar of the contract, MAKER proposes a season range, a stop-sale range and a default season,
    CHECKER a season range; MAKER proposes a holiday event. Only the other person may approve each."""
    body = {"owner_kind": "supplier_contract", "supplier_contract_id": contract, "name": "Invented contract seasons"}
    r = await c.post("/pricing/season-calendars", json=body, headers=_as(cis, MAKER))
    assert r.status_code == 201, r.text
    cal = r.json()["id"]

    async def propose(who: str, rng: dict[str, Any]) -> None:
        r = await c.post(f"/pricing/season-calendars/{cal}/ranges", json=rng, headers=_as(cis, who))
        assert r.status_code == 201, r.text

    season = {"kind": "season", "season_code": "high", "note": "Invented busy month"}
    await propose(MAKER, {**season, "date_from": "2027-07-01", "date_to": "2027-07-31"})
    await propose(CHECKER, {**season, "date_from": "2027-09-01", "date_to": "2027-09-30"})
    stop = {"kind": "stop_sale", "date_from": "2027-05-01", "date_to": "2027-05-02", "note": "Invented closure"}
    await propose(MAKER, stop)
    await _ok(
        await c.post(
            f"/pricing/season-calendars/{cal}/default",
            json={"default_season": "high", "reason": "Invented busy year"},
            headers=_as(cis, MAKER),
        )
    )
    event = {
        "country_iso2": "VN",
        "code": "it1190_lantern",
        "name": "Invented lantern days",
        "date_from": "2027-02-10",
        "date_to": "2027-02-12",
        "reason": "Invented local holiday",
    }
    r = await c.post("/pricing/holiday-events", json=event, headers=_as(cis, MAKER))
    assert r.status_code == 201, r.text


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
    if key in ("trip_checks_overdue", "trip_checks_unassigned"):
        assert f == {"due": "overdue", "owner": "me" if key == "trip_checks_overdue" else "none"}
        body = await _ok(await c.get("/trip-checks", params={"assignee": f["owner"], "limit": 1}, headers=headers))
        return body["counts"]["overdue"]  # the Overdue tab's badge under the owner filter
    if key == "price_versions_to_approve":
        assert f == {"pending_versions": "me"}
        params = {"status": "pending", "approvable": "me"}
        return len((await _ok(await c.get("/catalog/price-versions", params=params, headers=headers)))["items"])
    if key == "addons_to_publish":
        assert f == {"approvable": "me"}
        return (await _ok(await c.get("/add-ons", params={"approvable": "me", "limit": 1}, headers=headers)))["total"]
    if key == "season_changes_to_approve":  # the Seasons page's ?approvable=me view (each calendar's approvable)
        assert f == {"approvable": "me"}
        items = (await _ok(await c.get("/pricing/season-calendars", headers=headers)))["items"]
        return sum(i["approvable"] for i in items)
    if key == "holiday_events_to_approve":  # the Holiday events page's ?approvable=me view
        assert f == {"approvable": "me"}
        params = {"approvable": "me"}
        return len((await _ok(await c.get("/pricing/holiday-events", params=params, headers=headers)))["items"])
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
    if key == "contracts_ending_soon":  # AA-1177: the Suppliers page's "Contracts ending soon" list
        assert f == {"contracts": "ending"}
        body = await _ok(await c.get("/contracts/expiring", headers=headers))
        return len(body["items"])
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
    if key == "supplier_bank_to_approve":
        assert f == {"bank": "pending", "approvable": "me"}
        body = await _ok(await c.get("/suppliers", params={**f, "limit": 1}, headers=headers))
        return body["total"]
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
        "trip_checks_unassigned",  # AA-1190
        "b2b_on_request",
        "supplier_bills_overdue",
        "supplier_bills_due_soon",
        "contracts_ending_soon",  # AA-1177
        "supplier_bills_to_approve",
        "stop_sales_to_review",
        "suppliers_to_activate",
        "price_versions_to_approve",  # AA-1190
        "addons_to_publish",
        "season_changes_to_approve",
        "holiday_events_to_approve",
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
    # AA-1190: MAKER owns one overdue check (due yesterday); CHECKER owns none; the unowned one is in "no owner"
    assert mine["trip_checks_overdue"]["count"] == 1 and other["trip_checks_overdue"]["count"] == 0
    assert mine["trip_checks_overdue"]["earliest_due"] == (date.today() - timedelta(days=1)).isoformat()
    assert mine["trip_checks_unassigned"]["count"] >= 1
    assert mine["trip_checks_unassigned"]["earliest_due"] <= (date.today() - timedelta(days=2)).isoformat()
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
    # AA-1190: season changes: MAKER may approve only CHECKER's range; CHECKER may approve MAKER's range, stop-sale
    # range and default season. The holiday event is MAKER's: only CHECKER may approve it. The lists hold them all.
    assert mine["season_changes_to_approve"]["count"] == 1 and other["season_changes_to_approve"]["count"] == 3
    assert other["season_changes_to_approve"]["oldest_at"] is not None
    assert mine["holiday_events_to_approve"]["count"] == 0 and other["holiday_events_to_approve"]["count"] == 1
    calendars = (await _ok(await c.get("/pricing/season-calendars", headers=_as(cis, MAKER))))["items"]
    assert sum(i["waiting"] + (1 if i["pending_default_season"] else 0) for i in calendars) == 4
    events = (await _ok(await c.get("/pricing/holiday-events", headers=_as(cis, MAKER))))["items"]
    assert [e["code"] for e in events if e["state"] == "pending"] == ["it1190_lantern"]
    # Finance reads prices but approves no season: its approvable views are empty
    assert (
        sum(
            i["approvable"]
            for i in (await _ok(await c.get("/pricing/season-calendars", headers=_as(cis, FINANCE))))["items"]
        )
        == 0
    )


async def test_the_price_setup_card_equals_the_workbench(client: Client, admin: asyncpg.Connection) -> None:
    """AA-1190: the card counts the plans per next step of the price workbench (its own summary), for the price setup
    makers only."""
    c, cis = client
    card = await _ok(await c.get("/my-work/price-setup", headers=_as(cis, MAKER)))
    bench = await _ok(await c.get("/catalog/price-workbench", headers=_as(cis, MAKER)))
    assert card["by_next_step"] == bench["summary"]["by_next_step"]
    assert card["total"] == sum(bench["summary"]["by_next_step"].values())
    assert card["steps"][0] == "not_served" and card["steps"][-1] == "done"
    r = await c.get("/my-work/price-setup", headers=_as(cis, FINANCE))
    assert r.status_code == 403 and r.json()["detail"] == "forbidden", r.text
