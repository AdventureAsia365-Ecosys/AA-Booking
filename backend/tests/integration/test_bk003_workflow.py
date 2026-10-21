"""bk-003 + the staff / trip case services on a real PostgreSQL, as role ``booking``.

Covers what the unit fakes cannot: the SQL itself, the transition and lock triggers, the history
trigger with transaction-local actor settings, the partial unique indexes and role privileges.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.auth import admin as staff_admin
from app.auth.staff import Staff
from app.db import Database
from app.errors import Conflict, Invalid
from app.main import create_app
from app.settings import Settings
from app.trip_cases import service, versions
from app.trip_cases.versions import Item
from tests.staff_tokens import LocalStaff, seed_linked_staff

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TABLES = (
    "booking.trip_case_version_itinerary, booking.trip_case_versions, booking.staff_assignments, "
    "booking.trip_case_status_history, booking.trip_case_legs, booking.notifications, booking.trip_cases, "
    "booking.staff_users, booking.audit_logs, booking.customers"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(f"TRUNCATE {TABLES} CASCADE")
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


async def provisioned(pool: asyncpg.Pool, sub: str, cis_role: str, role: str) -> Staff:
    """A linked staff row keyed by ``sub`` (AA-911: the bridge provisioning is gone). Admins start active and the
    others inactive, as the old bridge left them, so the activation flows below still have something to activate."""
    async with pool.acquire() as conn, conn.transaction():
        return await seed_linked_staff(conn, sub, role, active=cis_role == "admin", cis_role=cis_role)


async def new_case(pool: asyncpg.Pool, legs: list[tuple[int, str, int, int]] = ()) -> dict[str, Any]:
    snapshot = {"days": [{"day": 1, "source_tour_id": "tour-b", "name": "Sapa trek"}], "custom_days": []}
    async with pool.acquire() as conn, conn.transaction():
        customer = await conn.fetchval(
            "INSERT INTO booking.customers (name, email) VALUES ('Lead', $1) RETURNING id", f"{uuid.uuid4().hex}@x.test"
        )
        case = await conn.fetchrow(
            "INSERT INTO booking.trip_cases (customer_id, source_system, inquiry_snapshot, currency)"
            " VALUES ($1, 'tripplanner', $2::jsonb, 'USD') RETURNING id::text AS id, code",
            customer,
            json.dumps(snapshot),
        )
        await conn.executemany(
            "INSERT INTO booking.trip_case_legs (trip_case_id, position, source_tour_id, day_from, day_to)"
            " VALUES ($1::uuid, $2, $3, $4, $5)",
            [(case["id"], *leg) for leg in legs],
        )
    return dict(case)


async def test_admin_updates_staff(pool: asyncpg.Pool) -> None:
    boss = await provisioned(pool, "boss", "admin", "admin")
    await provisioned(pool, "rev", "reviewer", "sales")
    async with pool.acquire() as conn, conn.transaction():
        updated = await staff_admin.update_staff(conn, boss, "rev", active=True, role=None)
        listed = await staff_admin.list_staff(conn, active=True)
    assert updated["active"] is True
    assert {s["cis_user_id"] for s in listed} == {"boss", "rev"}


async def test_status_history_trigger_and_transition_rules(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    sales = await provisioned(pool, "boss", "admin", "admin")
    case = await new_case(pool)
    for step in ("discovery", "building", "proposal_sent", "building"):
        async with pool.acquire() as conn, conn.transaction():
            out = await service.change_status(conn, sales, case["code"], step, f"to {step}")
    assert out["allowed_transitions"] == ["cancelled", "discovery", "proposal_sent"]
    history = await admin.fetch(
        "SELECT from_status, to_status, changed_by, note FROM booking.trip_case_status_history"
        " WHERE trip_case_id = $1::uuid ORDER BY changed_at, id",
        case["id"],
    )
    assert [(h["from_status"], h["to_status"]) for h in history] == [
        (None, "new_inquiry"),
        ("new_inquiry", "discovery"),
        ("discovery", "building"),
        ("building", "proposal_sent"),
        ("proposal_sent", "building"),
    ]
    assert history[0]["changed_by"] is None and history[1]["changed_by"] == "boss"
    assert history[2]["note"] == "to building"
    async with pool.acquire() as conn:
        # The trigger rejects an illegal edge even without the service's pre-check.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE booking.trip_cases SET status = 'paid' WHERE id = $1::uuid", case["id"])
        # Settings are transaction-local: a later system change on the same connection has no actor.
        await conn.execute("UPDATE booking.trip_cases SET status = 'cancelled' WHERE id = $1::uuid", case["id"])
    last = await admin.fetchrow(
        "SELECT changed_by, note FROM booking.trip_case_status_history WHERE trip_case_id = $1::uuid"
        " AND to_status = 'cancelled'",
        case["id"],
    )
    assert (last["changed_by"], last["note"]) == (None, None)
    async with pool.acquire() as conn:
        with pytest.raises(Conflict):
            await service.change_status(conn, sales, case["code"], "discovery", None)  # cancelled is terminal


async def test_cancel_needs_a_reason(pool: asyncpg.Pool) -> None:
    sales = await provisioned(pool, "boss", "admin", "admin")
    case = await new_case(pool)
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid):
            await service.change_status(conn, sales, case["id"], "cancelled", None)


async def test_role_booking_cannot_rewrite_rules_or_history(pool: asyncpg.Pool) -> None:
    await new_case(pool)
    async with pool.acquire() as conn:
        for sql in (
            "INSERT INTO booking.trip_case_status_transitions VALUES ('new_inquiry', 'paid')",
            "DELETE FROM booking.trip_case_status_transitions",
            "UPDATE booking.trip_case_status_history SET note = 'x'",
            "DELETE FROM booking.trip_case_status_history",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(sql)


async def test_list_detail_update_and_assign(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await provisioned(pool, "boss", "admin", "admin")
    await provisioned(pool, "rev", "reviewer", "sales")
    first = await new_case(pool, [(1, "tour-a", 1, 3)])
    second = await new_case(pool)
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid):  # inactive staff cannot be assigned
            await service.assign(conn, boss, first["code"], "rev", "sales")
    await admin.execute("UPDATE booking.staff_users SET active = true WHERE cis_user_id = 'rev'")
    async with pool.acquire() as conn, conn.transaction():
        await service.assign(conn, boss, first["code"], "boss", "sales")
        moved = await service.assign(conn, boss, first["code"], "rev", "sales")
        await service.assign(conn, boss, first["code"], "boss", "contracting")
        updated = await service.update_case(conn, boss, first["id"], {"pax": 3, "notes": "VIP", "currency": "EUR"})
    assert moved["changed"] is True
    assert updated["changed"] == ["currency", "notes", "pax"]
    async with pool.acquire() as conn:
        mine = await service.list_cases(conn, assignee="rev")
        unassigned = await service.list_cases(conn, assignee="none")
        new = await service.list_cases(conn, statuses=["new_inquiry"], limit=1)
        detail = await service.get_case(conn, first["code"].lower())
    assert [c["code"] for c in mine["items"]] == [first["code"]]
    assert [c["code"] for c in unassigned["items"]] == [second["code"]]
    assert new["total"] == 2 and len(new["items"]) == 1
    assert {(a["cis_user_id"], a["role"]) for a in detail["assignees"]} == {("rev", "sales"), ("boss", "contracting")}
    # All three were written in one transaction (same now()), so compare without order.
    assert sorted((a["staff_user_id"], a["role"], a["unassigned_at"] is None) for a in detail["assignments"]) == [
        ("boss", "contracting", True),
        ("boss", "sales", False),
        ("rev", "sales", True),
    ]
    # bk-011 (AA-830): the catalog tour key is null for a leg written without one.
    leg = {"position": 1, "source_tour_id": "tour-a", "day_from": 1, "day_to": 3}
    assert detail["legs"] == [leg | {"source_system": None, "source_id": None}]
    assert detail["inquiry_snapshot"]["days"][0]["name"] == "Sapa trek"
    assert (detail["pax"], detail["notes"], detail["currency"]) == (3, "VIP", "EUR")
    assert detail["allowed_transitions"] == ["cancelled", "discovery"]
    assert detail["status_history"][0]["to_status"] == "new_inquiry"
    actions = {r["action"] for r in await admin.fetch("SELECT action FROM booking.audit_logs WHERE actor_ref = 'boss'")}
    assert {"trip_case.assigned", "trip_case.updated"} <= actions
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid):
            await service.update_case(conn, boss, first["id"], {"start_date": None, "pax": 0})


async def test_versions_prefill_copy_and_lock(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    sales = await provisioned(pool, "boss", "admin", "admin")
    case = await new_case(pool, [(1, "tour-a", 4, 6), (2, "tour-b", 1, 3), (3, "tour-c", 1, 1)])
    async with pool.acquire() as conn, conn.transaction():
        v1 = await versions.create_version(conn, sales, case["code"], "first")
    assert v1["version_number"] == 1 and v1["currency"] == "USD" and v1["total_price"] is None
    assert [(i["source_tour_id"], i["day_from"], i["title"]) for i in v1["items"]] == [
        ("tour-b", 1, "Sapa trek"),  # day 1, leg position 2
        ("tour-c", 1, "AA tour tour-c"),  # day 1, leg position 3
        ("tour-a", 4, "AA tour tour-a"),
    ]
    async with pool.acquire() as conn, conn.transaction():
        edited = await versions.replace_items(
            conn, sales, case["code"], 1, [Item("Hanoi", 1, 2, None, "free text"), Item("Halong", 3, 4, "tour-a")]
        )
        v2 = await versions.create_version(conn, sales, case["code"], None)
    assert [i["position"] for i in edited["items"]] == [1, 2]
    assert v2["version_number"] == 2 and v2["notes"] == "first"
    assert [(i["title"], i["notes"]) for i in v2["items"]] == [("Hanoi", "free text"), ("Halong", None)]

    # Approve V2 out of band (no approve endpoint yet): it is then locked in the database.
    await admin.execute(
        "UPDATE booking.trip_case_versions SET is_approved = true, approved_at = now()"
        " WHERE trip_case_id = $1::uuid AND version_number = 2",
        case["id"],
    )
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as locked:
            await versions.replace_items(conn, sales, case["code"], 2, [Item("x", 1, 1)])
    assert locked.value.code == "version_locked"
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "INSERT INTO booking.trip_case_version_itinerary"
                " (trip_case_version_id, position, title, day_from, day_to) VALUES ($1::uuid, 9, 'x', 1, 1)",
                v2["id"],
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute("UPDATE booking.trip_case_versions SET notes = 'x' WHERE id = $1::uuid", v2["id"])
        with pytest.raises(asyncpg.UniqueViolationError):  # one approved version per case
            await conn.execute(
                "UPDATE booking.trip_case_versions SET is_approved = true, approved_at = now() WHERE id = $1::uuid",
                v1["id"],
            )
        with pytest.raises(asyncpg.CheckViolationError):  # approved needs approved_at
            await conn.execute("UPDATE booking.trip_case_versions SET is_approved = true WHERE id = $1::uuid", v1["id"])
    async with pool.acquire() as conn, conn.transaction():
        v3 = await versions.create_version(conn, sales, case["code"], "after approval")
        got = await versions.get_items(conn, case["id"], 3)
    assert v3["version_number"] == 3 and len(got["items"]) == 2
    audit = await admin.fetch(
        "SELECT action, actor_id, actor_ref FROM booking.audit_logs WHERE entity_type = 'trip_case_version'"
    )
    assert {a["action"] for a in audit} == {"trip_case_version.created", "trip_case_version.items_replaced"}
    assert all(a["actor_ref"] == "boss" and a["actor_id"] is None for a in audit)  # "boss" is not a UUID


async def test_staff_uuid_ids_fill_audit_actor_id(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    sub = str(uuid.uuid4())
    await provisioned(pool, sub, "admin", "admin")
    row = await admin.fetchrow(
        "SELECT actor_id::text AS actor_id, entity_id::text AS entity_id FROM booking.audit_logs"
    )
    assert row["actor_id"] == sub and row["entity_id"] == sub


async def test_http_flow_with_a_workforce_token(migrated_booking_dsn: str, admin: asyncpg.Connection) -> None:
    """WORKFORCE token (local RSA key) → linked staff rows → trip case endpoints, through the real app and pool."""
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    db, cis = Database(settings), LocalStaff()
    app = create_app(settings, db, cis.verifier())
    boss = {"Authorization": f"Bearer {cis.token('boss', 'admin')}"}
    rev = {"Authorization": f"Bearer {cis.token('rev', 'sales')}"}
    try:
        await provisioned(await db.pool(), "boss", "admin", "admin")
        await provisioned(await db.pool(), "rev", "reviewer", "sales")  # inactive until boss activates it
        case = await new_case(await db.pool(), [(1, "tour-a", 1, 2)])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking") as client:
            me = await client.get("/booking/staff/me", headers=boss)
            inactive = await client.get("/booking/staff/me", headers=rev)
            activated = await client.patch("/booking/staff/rev", json={"active": True}, headers=boss)
            listed = await client.get("/booking/trip-cases?status=new_inquiry", headers=rev)
            moved = await client.post(
                f"/booking/trip-cases/{case['code']}/status", json={"to_status": "discovery"}, headers=rev
            )
            illegal = await client.post(
                f"/booking/trip-cases/{case['code']}/status", json={"to_status": "paid"}, headers=rev
            )
            created = await client.post(f"/booking/trip-cases/{case['code']}/versions", headers=rev)
            put = await client.put(
                f"/booking/trip-cases/{case['code']}/versions/1/items",
                json={"items": [{"title": "Hanoi", "day_from": 1, "day_to": 1}]},
                headers=rev,
            )
            detail = await client.get(f"/booking/trip-cases/{case['id']}", headers=rev)
    finally:
        await db.close()
    assert me.status_code == 200 and me.json()["role"] == "admin"
    assert inactive.status_code == 403
    assert activated.status_code == 200 and activated.json()["active"] is True
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert moved.status_code == 200 and moved.json()["status"] == "discovery"
    assert illegal.status_code == 409 and illegal.json()["allowed_transitions"] == ["building", "cancelled"]
    assert created.status_code == 201 and created.json()["items"][0]["source_tour_id"] == "tour-a"
    assert put.status_code == 200 and [i["title"] for i in put.json()["items"]] == ["Hanoi"]
    body = detail.json()
    assert body["status"] == "discovery" and body["versions"][0]["item_count"] == 1
    assert [h["changed_by"] for h in body["status_history"]] == [None, "rev"]
