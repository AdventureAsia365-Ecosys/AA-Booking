"""AA-682 S2 over HTTP on a real PostgreSQL (bk-027), as role ``booking``:

- a sold B2C case shows its three seeded checks (standard order), the board and the derived checks (suppliers
  confirming → open, payment open: no paid invoice, bk-028);
- the emergency contact check is done only with a name and a phone; the audit row names the fields, never the values;
  done → not applicable in one call (two edges); reopen clears done_by / done_at;
- custom checks: add (201), rename; a standard check keeps its title (422);
- B2B case: no emergency contact (422 before the DB, and the DB refuses a reopen too: 409);
- the /trip-checks list: filters status / assignee (me, none) / due_before / code / q, customer name for sales;
- a cancelled case is read-only (409 case_closed); an inactive assignee is 422;
- permissions: content creators get 403 on reads and writes, contracting may write;
- GET /case-lifecycle/runs answers the latest runs and the scheduled mode.
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

SALES, CONTRACTING, CREATOR, GONE = "it-682-sales", "it-682-contracting", "it-682-creator", "it-682-gone"
Client = tuple[httpx.AsyncClient, LocalCis]


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await ops.purge_ops(conn)
    await ops.staff(conn, (SALES, "sales"), (CONTRACTING, "contracting"), (CREATOR, "content_creator"), (GONE, "sales"))
    await conn.execute("UPDATE booking.staff_users SET active = false WHERE cis_user_id = $1", GONE)
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


def _auth(cis: LocalCis, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cis.token(sub=sub, cis_role='reviewer')}"}


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _err(response: httpx.Response, status: int, detail: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail, response.json()
    return response.json()


async def test_case_checks_flow(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    sales = _auth(cis, SALES)
    sup = await ops.supply(admin)
    sold = await ops.sold_case(admin, sup, tag="api")
    out = await _ok(await c.get(f"/trip-cases/{sold['code']}/checks", headers=sales))
    assert [i["code"] for i in out["items"]] == ["pre_trip_recheck", "emergency_contact", "insurance"]
    assert all(i["status"] == "open" and i["standard"] for i in out["items"])
    assert out["items"][0]["due_date"] == (ops.START - timedelta(days=14)).isoformat()
    assert out["board"]["checks_open"] == 3 and out["contact_allowed"] and out["writable"]
    suppliers, payment = out["derived"]
    assert (suppliers["code"], suppliers["status"], suppliers["detail"]) == (
        "suppliers_confirmed",
        "open",
        "confirming",
    )
    # bk-028: a B2C case's payment check reads its invoice (none yet: open).
    assert (payment["available"], payment["status"]) == (True, "open")
    contact = next(i for i in out["items"] if i["code"] == "emergency_contact")
    assert contact["contact_allowed"] is True
    insurance = next(i for i in out["items"] if i["code"] == "insurance")

    # Done needs both contact fields.
    _err(
        await c.patch(f"/trip-checks/{contact['id']}", json={"status": "done"}, headers=sales),
        422,
        "emergency_contact_required",
    )
    done = await _ok(
        await c.patch(
            f"/trip-checks/{contact['id']}",
            json={
                "status": "done",
                "emergency_contact_name": "Anna Example",
                "emergency_contact_phone": "+84 90 123 4567",
                "note": "Sister",
            },
            headers=sales,
        )
    )
    assert done["status"] == "done" and done["done_by"]["id"] == SALES and done["done_at"]
    assert done["changed"] == ["emergency_contact_name", "emergency_contact_phone", "note", "status"]
    audit = await admin.fetchrow(
        "SELECT before::text AS b, after::text AS a, summary FROM booking.audit_logs"
        " WHERE entity_type = 'trip_case_check' AND entity_id = $1::uuid ORDER BY created_at DESC LIMIT 1",
        contact["id"],
    )
    assert "Anna" not in audit["a"] and "+84" not in audit["a"] and "Sister" not in audit["a"]
    assert '"(set)"' in audit["a"] and audit["summary"].startswith("Done: Emergency contact")
    same = await _ok(await c.patch(f"/trip-checks/{contact['id']}", json={"status": "done"}, headers=sales))
    assert same["changed"] == []

    # done -> not_applicable in one call; then reopen clears the done stamp.
    na = await _ok(await c.patch(f"/trip-checks/{insurance['id']}", json={"status": "done"}, headers=sales))
    na = await _ok(await c.patch(f"/trip-checks/{insurance['id']}", json={"status": "not_applicable"}, headers=sales))
    assert na["status"] == "not_applicable" and na["done_by"] is None and na["done_at"] is None
    reopened = await _ok(await c.patch(f"/trip-checks/{insurance['id']}", json={"status": "open"}, headers=sales))
    assert reopened["status"] == "open"

    # Custom checks: add, rename; a standard check keeps its title; contact fields only on the contact check.
    custom = await _ok(
        await c.post(
            f"/trip-cases/{sold['code']}/checks",
            json={"title": "Visa letter", "due_date": "2027-01-10", "assignee_ref": CONTRACTING},
            headers=_auth(cis, CONTRACTING),
        ),
        201,
    )
    assert custom["code"] == "custom" and custom["assignee"]["id"] == CONTRACTING and not custom["standard"]
    renamed = await _ok(await c.patch(f"/trip-checks/{custom['id']}", json={"title": "Visa letters"}, headers=sales))
    assert renamed["title"] == "Visa letters"
    _err(await c.patch(f"/trip-checks/{insurance['id']}", json={"title": "x"}, headers=sales), 422, "title_fixed")
    _err(
        await c.patch(f"/trip-checks/{insurance['id']}", json={"emergency_contact_name": "X"}, headers=sales),
        422,
        "contact_on_wrong_check",
    )
    _err(
        await c.patch(f"/trip-checks/{custom['id']}", json={"assignee_ref": GONE}, headers=sales),
        422,
        "staff_not_assignable",
    )
    cleared = await _ok(await c.patch(f"/trip-checks/{custom['id']}", json={"assignee_ref": None}, headers=sales))
    assert cleared["assignee"] is None

    # A cancelled case is read-only.
    await admin.execute("UPDATE booking.trip_cases SET status = 'cancelled' WHERE id = $1::uuid", sold["id"])
    _err(await c.patch(f"/trip-checks/{custom['id']}", json={"note": "x"}, headers=sales), 409, "case_closed")
    _err(await c.post(f"/trip-cases/{sold['code']}/checks", json={"title": "x"}, headers=sales), 409, "case_closed")
    assert (await _ok(await c.get(f"/trip-cases/{sold['code']}/checks", headers=sales)))["writable"] is False


async def test_b2b_case_keeps_no_emergency_contact(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    sales = _auth(cis, SALES)
    case = await ops.b2b_case(admin, tag="api-b2b")
    out = await _ok(await c.get(f"/trip-cases/{case['code']}/checks", headers=sales))
    assert out["contact_allowed"] is False and out["derived"][0]["status"] == "done"  # nothing to book: none
    contact = next(i for i in out["items"] if i["code"] == "emergency_contact")
    assert contact["status"] == "not_applicable" and contact["contact_allowed"] is False
    _err(
        await c.patch(
            f"/trip-checks/{contact['id']}",
            json={"emergency_contact_name": "X", "emergency_contact_phone": "+84 90 123 4567"},
            headers=sales,
        ),
        422,
        "contact_not_allowed_b2b",
    )
    # The database refuses the reopen of the B2B contact check (DM-6.8), whatever the API allowed.
    _err(
        await c.patch(f"/trip-checks/{contact['id']}", json={"status": "open"}, headers=sales),
        409,
        "invalid_trip_check",
    )
    # Other checks of a B2B case work as usual.
    recheck = next(i for i in out["items"] if i["code"] == "pre_trip_recheck")
    assert (await _ok(await c.patch(f"/trip-checks/{recheck['id']}", json={"status": "done"}, headers=sales)))[
        "status"
    ] == "done"


async def test_trip_checks_list_filters(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    sales = _auth(cis, SALES)
    soon = await ops.b2c_case(admin, tag="list-soon", status="confirmed", start=date.today() + timedelta(days=16))
    late = await ops.b2b_case(admin, tag="list-late", start=date.today() + timedelta(days=60))
    presale = await ops.b2c_case(admin, tag="list-pre", status="building")
    soon_checks = (await _ok(await c.get(f"/trip-cases/{soon['code']}/checks", headers=sales)))["items"]
    await _ok(await c.patch(f"/trip-checks/{soon_checks[2]['id']}", json={"assignee_ref": SALES}, headers=sales))
    page = await _ok(await c.get("/trip-checks", params={"case": soon["code"]}, headers=sales))
    assert page["total"] == 3 and page["items"][0]["trip_case"]["customer_name"] is None  # no name on the fixture
    mine = await _ok(await c.get("/trip-checks", params={"assignee": "me"}, headers=sales))
    assert [i["id"] for i in mine["items"]] == [soon_checks[2]["id"]]
    unassigned = await _ok(await c.get("/trip-checks", params={"assignee": "none", "q": late["code"]}, headers=sales))
    assert unassigned["total"] == 2  # the B2B emergency contact is not_applicable, so not open
    due = await _ok(
        await c.get(
            "/trip-checks", params={"due_before": (date.today() + timedelta(days=5)).isoformat()}, headers=sales
        )
    )
    assert {i["trip_case"]["code"] for i in due["items"]} == {soon["code"]}
    allstatus = await _ok(await c.get("/trip-checks", params={"status": "all", "q": late["code"]}, headers=sales))
    assert allstatus["total"] == 3
    # AA-996: tab counts over every status (same search), and each row's trip progress + sales owner.
    assert allstatus["counts"] == {"open": 2, "overdue": 0, "done": 0, "not_applicable": 1, "all": 3}
    assert unassigned["counts"]["not_applicable"] == 1 and unassigned["counts"]["all"] == 3
    assert {
        (i["trip_case"]["checks"]["total"], i["trip_case"]["checks"]["not_applicable"]) for i in allstatus["items"]
    } == {(3, 1)}
    assert {i["trip_case"]["sales_name"] for i in allstatus["items"]} == {None}
    # AA-996 review: the real SQL of the overdue count and of the sales owner subquery. One open check due yesterday,
    # and a sales owner on the case (a closed earlier assignment must not win).
    late_open = next(i for i in allstatus["items"] if i["status"] == "open")
    await admin.execute(
        "UPDATE booking.trip_case_checks SET due_date = current_date - 1 WHERE id = $1::uuid", late_open["id"]
    )
    await admin.execute(
        "INSERT INTO booking.staff_assignments (trip_case_id, staff_user_id, role, assigned_at, unassigned_at)"
        " SELECT id, $2, 'sales', now() - interval '2 days', now() - interval '1 day' FROM booking.trip_cases"
        " WHERE code = $1",
        late["code"],
        CONTRACTING,
    )
    await admin.execute(
        "INSERT INTO booking.staff_assignments (trip_case_id, staff_user_id, role)"
        " SELECT id, $2, 'sales' FROM booking.trip_cases WHERE code = $1",
        late["code"],
        SALES,
    )
    owned = await _ok(await c.get("/trip-checks", params={"status": "all", "q": late["code"]}, headers=sales))
    assert owned["counts"] == {"open": 2, "overdue": 1, "done": 0, "not_applicable": 1, "all": 3}
    assert {i["trip_case"]["sales_name"] for i in owned["items"]} == {SALES}  # ops.staff names each user by its id
    by_code = await _ok(await c.get("/trip-checks", params={"code": "insurance"}, headers=sales))
    assert {i["code"] for i in by_code["items"]} == {"insurance"}
    assert all(i["trip_case"]["code"] != presale["code"] for i in by_code["items"])
    ordered = [i["due_date"] for i in (await _ok(await c.get("/trip-checks", headers=sales)))["items"] if i["due_date"]]
    assert ordered == sorted(ordered)
    assert (await c.get("/trip-checks", params={"status": "closed"}, headers=sales)).status_code == 422


async def test_permissions_and_runs(client: Client, admin: asyncpg.Connection) -> None:
    c, cis = client
    case = await ops.b2b_case(admin, tag="perm")
    creator = _auth(cis, CREATOR)
    assert (await c.get("/trip-checks", headers=creator)).status_code == 403
    assert (await c.get(f"/trip-cases/{case['code']}/checks", headers=creator)).status_code == 403
    assert (await c.post(f"/trip-cases/{case['code']}/checks", json={"title": "x"}, headers=creator)).status_code == 403
    assert (await c.get("/case-lifecycle/runs", headers=creator)).status_code == 403
    assert (await c.get("/trip-checks")).status_code == 401
    runs = await _ok(await c.get("/case-lifecycle/runs", headers=_auth(cis, CONTRACTING)))
    assert runs["scheduled_mode"] in ("dry_run", "live") and isinstance(runs["items"], list)
    _err(await c.get("/trip-cases/TC-999999/checks", headers=_auth(cis, SALES)), 404, "trip_case_not_found")
