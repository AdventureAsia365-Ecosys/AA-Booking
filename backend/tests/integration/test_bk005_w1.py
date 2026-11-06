"""bk-005 + the Wave 1 staff endpoints on a real PostgreSQL, as role ``booking``.

Covers D2 (item ids survive a save, so rows referencing them survive too), D3 (sent -> approve -> confirmed,
reopen), D4 (staff directory), the sent lock, notifications, settings, the cross-case version list, activity and audit
reads, and the bk-005 privileges. AA-1046: the B2C half is retired (no Send, no customers, no staff-made case); the
cases here are kept b2c cases written in SQL (``tests.integration.ops``) and a B2B case for the HTTP routes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app import audit
from app.auth import admin as staff_admin
from app.auth.staff import BridgeClaims, Staff, provision
from app.db import Database
from app.errors import Conflict, Forbidden, Invalid
from app.main import create_app
from app.notifications import service as notifications
from app.settings import Settings
from app.trip_cases import service, versions
from app.trip_cases.versions import Item
from tests.integration import ops
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TABLES = (
    "booking.notification_reads, booking.trip_case_countries, booking.trip_case_version_itinerary, "
    "booking.trip_case_versions, booking.staff_assignments, booking.trip_case_status_history, "
    "booking.trip_case_legs, booking.notifications, booking.trip_cases, booking.staff_users, booking.audit_logs"
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


async def staff(pool: asyncpg.Pool, admin: asyncpg.Connection, sub: str, role: str = "admin") -> Staff:
    cis_role = "admin" if role == "admin" else "reviewer"
    async with pool.acquire() as conn, conn.transaction():
        await provision(conn, BridgeClaims(sub, f"{sub}@example.test", sub.title(), cis_role), role)
    await admin.execute("UPDATE booking.staff_users SET active = true, role = $2 WHERE cis_user_id = $1", sub, role)
    return Staff(sub, f"{sub}@example.test", sub.title(), role, True, cis_role)


async def inquiry_case(admin: asyncpg.Connection, legs: list[tuple[int, str, int, int]] = ()) -> dict[str, Any]:
    """A kept b2c case from a TripPlanner hand-off (AA-1046: written in SQL, the intake is retired), with its legs."""
    snapshot = {"days": [{"day": 1, "source_tour_id": "tour-b", "name": "Sapa trek"}], "custom_days": []}
    case = await ops.insert_b2c_case(
        admin, tag=uuid.uuid4().hex[:8], status="new_inquiry", start=None, end=None, source_system="tripplanner"
    )
    await admin.execute(
        "UPDATE booking.trip_cases SET inquiry_snapshot = $2::jsonb, pax = NULL WHERE id = $1::uuid",
        case["id"],
        json.dumps(snapshot),
    )
    await admin.executemany(
        "INSERT INTO booking.trip_case_legs (trip_case_id, position, source_tour_id, day_from, day_to)"
        " VALUES ($1::uuid, $2, $3, $4, $5)",
        [(case["id"], *leg) for leg in legs],
    )
    return case


async def sent_out_of_band(pool: asyncpg.Pool, admin: asyncpg.Connection, actor: Staff, case: dict, n: int) -> None:
    """Version ``n`` sent to the traveller the way the retired Send did it (AA-1046: there is no Send route any more;
    the sent versions of the kept b2c cases still lock and still approve)."""
    status = await admin.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", case["id"])
    chain = ["discovery", "building", "proposal_sent"]
    if status in chain:
        chain = chain[chain.index(status) + 1 :]
    await move(pool, actor, case["code"], *chain)
    await admin.execute(
        "UPDATE booking.trip_case_versions SET is_sent_to_customer = true, sent_at = now(), sent_by = $3,"
        " valid_until = current_date + 14 WHERE trip_case_id = $1::uuid AND version_number = $2",
        case["id"],
        n,
        actor.cis_user_id,
    )


async def move(pool: asyncpg.Pool, actor: Staff, ref: str, *steps: str) -> None:
    for step in steps:
        async with pool.acquire() as conn, conn.transaction():
            await service.change_status(conn, actor, ref, step, f"to {step}")


async def priced_v1(pool: asyncpg.Pool, admin: asyncpg.Connection, actor: Staff, case: dict[str, Any]) -> dict:
    async with pool.acquire() as conn, conn.transaction():
        v1 = await versions.create_version(conn, actor, case["code"], None)
    # The pricing engine (bk-004 /price) is on the AA-810 branch; set the total out of band here.
    await admin.execute("UPDATE booking.trip_case_versions SET total_price = 1990 WHERE id = $1::uuid", v1["id"])
    return v1


# ── D2 ───────────────────────────────────────────────────────────────────────


async def test_d2_put_items_keeps_ids_and_referencing_rows(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(admin, [(1, "tour-a", 1, 3), (2, "tour-b", 4, 5)])
    async with pool.acquire() as conn, conn.transaction():
        v1 = await versions.create_version(conn, boss, case["code"], None)
    first, second = v1["items"]
    # Stand-in for bk-004 version_item_costs: same FK shape (ON DELETE CASCADE on the item id).
    await admin.execute("CREATE SCHEMA IF NOT EXISTS test_refs")
    await admin.execute(
        "CREATE TABLE IF NOT EXISTS test_refs.item_costs (id serial PRIMARY KEY, version_item_id UUID NOT NULL"
        " REFERENCES booking.trip_case_version_itinerary (id) ON DELETE CASCADE, amount NUMERIC NOT NULL)"
    )
    try:
        await admin.execute("TRUNCATE test_refs.item_costs")
        await admin.execute(
            "INSERT INTO test_refs.item_costs (version_item_id, amount) VALUES ($1::uuid, 100), ($2::uuid, 200)",
            first["id"],
            second["id"],
        )
        # Reorder (second first), edit the kept one, drop the first, add a new one.
        async with pool.acquire() as conn, conn.transaction():
            saved = await versions.replace_items(
                conn,
                boss,
                case["code"],
                1,
                [Item("Ha Giang loop", 1, 2, "tour-b", "edited", id=second["id"]), Item("New day", 3, 3)],
            )
        assert [(i["position"], i["title"]) for i in saved["items"]] == [(1, "Ha Giang loop"), (2, "New day")]
        assert saved["items"][0]["id"] == second["id"] and saved["items"][1]["id"] not in (first["id"], second["id"])
        costs = await admin.fetch("SELECT version_item_id::text AS item, amount FROM test_refs.item_costs")
        assert [(c["item"], int(c["amount"])) for c in costs] == [(second["id"], 200)]  # only the removed item's row
        # Saving the same list again (ids kept, a swap of positions) touches no referencing row.
        again = [
            Item(i["title"], i["day_from"], i["day_to"], i["source_tour_id"], i["notes"], id=i["id"])
            for i in saved["items"]
        ]
        async with pool.acquire() as conn, conn.transaction():
            swapped = await versions.replace_items(conn, boss, case["code"], 1, list(reversed(again)))
        assert [i["title"] for i in swapped["items"]] == ["New day", "Ha Giang loop"]
        assert await admin.fetchval("SELECT count(*) FROM test_refs.item_costs") == 1
        async with pool.acquire() as conn, conn.transaction():
            with pytest.raises(Invalid) as foreign:
                await versions.replace_items(conn, boss, case["code"], 1, [Item("x", 1, 1, id=str(uuid.uuid4()))])
        assert foreign.value.code == "unknown_item"
        async with pool.acquire() as conn, conn.transaction():
            with pytest.raises(Invalid):
                await versions.replace_items(
                    conn, boss, case["code"], 1, [Item("x", 1, 1, id=second["id"]), Item("y", 1, 1, id=second["id"])]
                )
    finally:
        await admin.execute("DROP SCHEMA test_refs CASCADE")


# ── D3: send, approve, reopen; sent lock ─────────────────────────────────────


async def test_d3_approve_confirms_and_locks(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    sales = await staff(pool, admin, "sam", "sales")
    case = await inquiry_case(admin, [(1, "tour-a", 1, 3)])
    async with pool.acquire() as conn, conn.transaction():
        await service.assign(conn, boss, case["code"], "sam", "sales")
    await move(pool, boss, case["code"], "discovery")
    v1 = await priced_v1(pool, admin, sales, case)
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as approve_early:
            await versions.approve_version(conn, sales, case["code"], 1, None)
        assert approve_early.value.code == "version_not_sent"
    await sent_out_of_band(pool, admin, sales, case, 1)
    # Sent = locked in the database: no item writes, no commercial field changes.
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute(
                "UPDATE booking.trip_case_version_itinerary SET title = 'x' WHERE trip_case_version_id = $1::uuid",
                v1["id"],
            )
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
            await conn.execute("UPDATE booking.trip_case_versions SET total_price = 1 WHERE id = $1::uuid", v1["id"])
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict):
            await versions.update_version(conn, sales, case["code"], 1, {"title": "late"})
        with pytest.raises(Conflict):
            await versions.delete_version(conn, sales, case["code"], 1)
    async with pool.acquire() as conn, conn.transaction():
        approved = await versions.approve_version(conn, boss, case["code"], 1, "customer said yes by phone")
    assert approved["badge"] == "approved" and approved["approved_by"] == "boss"
    assert approved["trip_case"]["status"] == "confirmed"
    async with pool.acquire() as conn:
        detail = await service.get_case(conn, case["code"])
        feed = await notifications.list_feed(conn, sales)
    assert detail["status"] == "confirmed" and detail["status_history"][-1]["note"].startswith("V1 approved")
    assert {n["type"] for n in feed["items"]} >= {"trip_case.assigned", "trip_case.status_changed", "proposal.approved"}
    actions = [r["action"] for r in await admin.fetch("SELECT action FROM booking.audit_logs ORDER BY created_at")]
    assert "trip_case_version.approved" in actions


async def test_approve_refuses_a_sent_version_without_items(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    """AA-996 (Figma audit item 12): Dev had an approved V1 with no items and a 2,400 USD total, made out of band. A
    version that is sent but empty is refused (422 version_empty) and the case stays at proposal_sent."""
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(admin)  # no legs: V1 starts with no items
    v1 = await priced_v1(pool, admin, boss, case)
    await move(pool, boss, case["code"], "discovery", "building", "proposal_sent")
    # The way a seed script reaches "sent" without Send (Send itself refuses an empty version).
    await admin.execute(
        "UPDATE booking.trip_case_versions SET is_sent_to_customer = true, sent_at = now() WHERE id = $1::uuid",
        v1["id"],
    )
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid) as empty:
            await versions.approve_version(conn, boss, case["code"], 1, "by phone")
    assert empty.value.code == "version_empty"
    row = await admin.fetchrow(
        "SELECT v.is_approved, tc.status FROM booking.trip_case_versions v"
        " JOIN booking.trip_cases tc ON tc.id = v.trip_case_id WHERE v.id = $1::uuid",
        v1["id"],
    )
    assert (row["is_approved"], row["status"]) == (False, "proposal_sent")


async def test_case_detail_derives_countries_from_the_legs(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    """AA-996 (Figma audit item 15): Countries read "—" on a TripPlanner case whose leg is in Laos. The detail now
    carries ``leg_countries``: the CIS tour country of each keyed leg, then the countries of the route-day places;
    stored ``countries`` are left alone."""
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, title, country, duration_days, updated_at)"
        " VALUES ('aa996-laos', 'Pak Ou Caves', 'Laos', 2, now())"
    )
    try:
        case = await inquiry_case(admin, [(1, "tour-b", 1, 2)])
        await admin.execute(
            "UPDATE booking.trip_case_legs SET source_system = 'cis', source_id = 'aa996-laos'"
            " WHERE trip_case_id = $1::uuid",
            case["id"],
        )
        await admin.execute(
            "UPDATE booking.trip_cases SET inquiry_snapshot = $2::jsonb WHERE id = $1::uuid",
            case["id"],
            json.dumps({"days": [{"day": 3, "destination_id": "d0000000-0000-4000-8000-000000000003"}]}),
        )
        async with pool.acquire() as conn:
            detail = await service.get_case(conn, case["code"])
        assert detail["countries"] == [] and detail["leg_countries"] == ["LA", "VN"]
    finally:
        await admin.execute("DELETE FROM catalog.v_tour WHERE source_id = 'aa996-laos'")


async def test_d3_resend_supersedes_and_reopen_needs_reason(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(admin, [(1, "tour-a", 1, 2)])
    await move(pool, boss, case["code"], "discovery", "building")
    await priced_v1(pool, admin, boss, case)
    await sent_out_of_band(pool, admin, boss, case, 1)
    async with pool.acquire() as conn, conn.transaction():
        v2 = await versions.create_version(conn, boss, case["code"], "second try")
    # V2 starts unpriced (review finding 2); price it out of band like V1.
    await admin.execute("UPDATE booking.trip_case_versions SET total_price = 2100 WHERE id = $1::uuid", v2["id"])
    await sent_out_of_band(pool, admin, boss, case, 2)
    async with pool.acquire() as conn, conn.transaction():
        listed = await versions.list_versions(conn, q=case["code"])
        with pytest.raises(Conflict) as superseded:
            await versions.approve_version(conn, boss, case["code"], 1, None)
    assert {v["version_number"]: v["badge"] for v in listed["items"]} == {1: "superseded", 2: "sent"}
    assert {v["trip_case_channel"] for v in listed["items"]} == {"b2c"}  # AA-1037: an inquiry case is a b2c case
    assert superseded.value.code == "version_superseded"
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as bare:  # confirmed is reached by approving a version only
            await service.change_status(conn, boss, case["code"], "confirmed", None)
    assert bare.value.code == "approve_version_first"
    async with pool.acquire() as conn, conn.transaction():
        await service.change_status(conn, boss, case["code"], "cancelled", "no response")
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid) as no_reason:
            await service.change_status(conn, boss, case["code"], "new_inquiry", None)
        assert no_reason.value.code == "reason_required"
    async with pool.acquire() as conn, conn.transaction():
        reopened = await service.change_status(conn, boss, case["code"], "new_inquiry", "customer came back")
    assert reopened["status"] == "new_inquiry"


async def test_new_version_and_item_changes_clear_the_price(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    """Review finding 2: a price belongs to one item list; V2+ and a changed list must be priced again."""
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(admin, [(1, "tour-a", 1, 2), (2, "tour-b", 3, 4)])
    await move(pool, boss, case["code"], "discovery", "building")
    v1 = await priced_v1(pool, admin, boss, case)
    await admin.execute("UPDATE booking.trip_case_versions SET channel = 'b2b' WHERE id = $1::uuid", v1["id"])
    async with pool.acquire() as conn, conn.transaction():
        v2 = await versions.create_version(conn, boss, case["code"], None)
    assert v2["total_price"] is None and v2["currency"] == "USD" and v2["channel"] == "b2b"
    await admin.execute("UPDATE booking.trip_case_versions SET total_price = 2500 WHERE id = $1::uuid", v2["id"])
    same = [
        Item(i["title"], i["day_from"], i["day_to"], i["source_tour_id"], i["notes"], id=i["id"]) for i in v2["items"]
    ]
    async with pool.acquire() as conn, conn.transaction():
        kept = await versions.replace_items(conn, boss, case["code"], 2, same)
    assert kept["total_price"] == "2500.00"  # saving the identical list keeps the price
    async with pool.acquire() as conn, conn.transaction():
        cleared = await versions.replace_items(conn, boss, case["code"], 2, same[:1])
    assert cleared["total_price"] is None
    summary = await admin.fetchval(
        "SELECT summary FROM booking.audit_logs WHERE action = 'trip_case_version.items_replaced'"
        " ORDER BY created_at DESC LIMIT 1"
    )
    assert summary.endswith("price cleared")


async def test_sent_version_cannot_be_hard_deleted_and_confirmed_can_cancel(
    pool: asyncpg.Pool, admin: asyncpg.Connection
) -> None:
    """Review findings 6 and 13."""
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(admin, [(1, "tour-a", 1, 2)])
    await move(pool, boss, case["code"], "discovery", "building")
    v1 = await priced_v1(pool, admin, boss, case)
    await sent_out_of_band(pool, admin, boss, case, 1)
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # role booking only soft-deletes versions
            await conn.execute("DELETE FROM booking.trip_case_versions WHERE id = $1::uuid", v1["id"])
    with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):  # not even the owner, not even by cascade
        await admin.execute("DELETE FROM booking.trip_case_versions WHERE id = $1::uuid", v1["id"])
    assert await admin.fetchval(
        "SELECT count(*) FROM booking.trip_case_version_itinerary WHERE trip_case_version_id = $1::uuid", v1["id"]
    )
    async with pool.acquire() as conn, conn.transaction():
        await versions.approve_version(conn, boss, case["code"], 1, None)
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid) as no_reason:
            await service.change_status(conn, boss, case["code"], "cancelled", None)
        assert no_reason.value.code == "reason_required"
    async with pool.acquire() as conn, conn.transaction():
        cancelled = await service.change_status(conn, boss, case["code"], "cancelled", "customer withdrew")
    assert cancelled["from_status"] == "confirmed" and cancelled["status"] == "cancelled"


async def test_version_patch_delete_and_number_not_reused(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(admin)
    async with pool.acquire() as conn, conn.transaction():
        await versions.create_version(conn, boss, case["code"], None)
        v2 = await versions.create_version(conn, boss, case["code"], None)
        patched = await versions.update_version(conn, boss, case["code"], 2, {"title": "Option B", "notes": "n"})
        await versions.delete_version(conn, boss, case["code"], 2)
        v3 = await versions.create_version(conn, boss, case["code"], None)
        got = await versions.get_version(conn, case["code"], 1)
        detail = await service.get_case(conn, case["code"])
    assert patched["changed"] == ["notes", "title"] and patched["title"] == "Option B"
    assert v2["version_number"] == 2 and v3["version_number"] == 3
    assert [v["version_number"] for v in detail["versions"]] == [1, 3]
    assert got["badge"] == "draft" and got["trip_case"]["code"] == case["code"] and got["fx_locked"] is None


# ── Cases: create, list, summary, activity ───────────────────────────────────


async def test_activity_merges_history_and_audit(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(admin)
    await move(pool, boss, case["code"], "discovery")
    async with pool.acquire() as conn, conn.transaction():
        await service.update_case(conn, boss, case["code"], {"notes": "VIP"})
        await versions.create_version(conn, boss, case["code"], None)
    async with pool.acquire() as conn:
        page1 = await service.activity(conn, case["code"], limit=2)
        page2 = await service.activity(conn, case["code"], limit=10, before=page1["next_before"])
    kinds = [(i["kind"], i["action"]) for i in page1["items"] + page2["items"]]
    assert ("status", "trip_case.status_changed") in kinds and ("audit", "trip_case.updated") in kinds
    assert ("audit", "trip_case_version.created") in kinds
    assert not any(k == ("audit", "trip_case.status_changed") for k in kinds)  # no duplicate of the history row
    assert len(kinds) == len({i["id"] for i in page1["items"] + page2["items"]}) == 4
    assert page2["next_before"] is None


# ── Customers ────────────────────────────────────────────────────────────────


# ── Notifications, staff, settings, audit ────────────────────────────────────


async def test_notification_feed_read_state_is_per_staff(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    sam = await staff(pool, admin, "sam", "sales")
    async with pool.acquire() as conn, conn.transaction():
        for i in range(3):
            await notifications.notify_staff(
                conn, type="trip_case.new_inquiry", title=f"New {i}", message="m", entity_type=None, entity_id=None
            )
        await notifications.notify_staff(
            conn,
            type="trip_case.assigned",
            title="For sam",
            message=None,
            entity_type=None,
            entity_id=None,
            recipient_ref="sam",
        )
    async with pool.acquire() as conn, conn.transaction():
        sam_feed = await notifications.list_feed(conn, sam, limit=2)
        boss_feed = await notifications.list_feed(conn, boss)
        page2 = await notifications.list_feed(conn, sam, limit=2, before=sam_feed["next_before"])
        marked = await notifications.mark_read(conn, sam, ids=[sam_feed["items"][0]["id"]], all_=False)
        unread_only = await notifications.list_feed(conn, sam, unread=True)
        typed = await notifications.list_feed(conn, sam, type="trip_case.assigned", q="sam")
    assert sam_feed["unread_count"] == 4 and boss_feed["unread_count"] == 3
    assert len(sam_feed["items"]) + len(page2["items"]) == 4
    assert marked == {"marked": 1, "unread_count": 3}
    assert len(unread_only["items"]) == 3 and [n["title"] for n in typed["items"]] == ["For sam"]
    async with pool.acquire() as conn, conn.transaction():
        all_marked = await notifications.mark_read(conn, boss, ids=None, all_=True)
        again = await notifications.mark_read(conn, boss, ids=None, all_=True)
        read_only = await notifications.list_feed(conn, boss, unread=False)
    assert all_marked["unread_count"] == 0 and again["marked"] == 0 and len(read_only["items"]) == 3
    assert await admin.fetchval("SELECT count(*) FROM booking.audit_logs WHERE action = 'notification.read'") == 2


async def test_staff_directory_detail_preferences(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    sam = await staff(pool, admin, "sam", "sales")
    await staff(pool, admin, "cathy", "contracting")
    await admin.execute("UPDATE booking.staff_users SET active = false WHERE cis_user_id = 'cathy'")
    case = await inquiry_case(admin)
    async with pool.acquire() as conn, conn.transaction():
        await service.assign(conn, boss, case["code"], "sam", "sales")
        everyone = await staff_admin.directory(conn)
        sales_only = await staff_admin.directory(conn, role="sales")
        mine = await staff_admin.staff_detail(conn, sam, "sam")
        with pytest.raises(Forbidden):
            await staff_admin.staff_detail(conn, sam, "boss")
        other = await staff_admin.staff_detail(conn, boss, "sam")
        prefs = await staff_admin.set_preferences(conn, sam, {"trip_case.status_changed": False})
        same = await staff_admin.set_preferences(conn, sam, {"trip_case.status_changed": False})
        with pytest.raises(Invalid):
            await staff_admin.set_preferences(conn, sam, {"bogus": True})
    assert [s["cis_user_id"] for s in everyone] == ["boss", "sam"]  # active only, no e-mail
    assert set(everyone[0]) == {"cis_user_id", "name", "role"} and [s["cis_user_id"] for s in sales_only] == ["sam"]
    assert mine["counts"]["open_assignments"] == 1 and mine["open_assignments"][0]["code"] == case["code"]
    assert other["cis_user_id"] == "sam"
    assert prefs["notify"]["trip_case.status_changed"] is False and prefs["notify"]["proposal.approved"] is True
    assert same == prefs
    assert (
        await admin.fetchval("SELECT count(*) FROM booking.audit_logs WHERE action = 'staff.preferences_updated'") == 1
    )


async def test_audit_log_read_pages_and_filters(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(admin)
    async with pool.acquire() as conn, conn.transaction():
        for i in range(3):
            await service.update_case(conn, boss, case["code"], {"notes": f"n{i}"})
    async with pool.acquire() as conn:
        first = await audit.list_logs(conn, actor="boss", entity_type="trip_case", limit=2)
        rest = await audit.list_logs(conn, actor="boss", entity_type="trip_case", limit=2, before=first["next_before"])
        # The audit date filter is in UTC days; date.today() is the host day and breaks off UTC.
        utc_today = datetime.now(UTC).date()
        by_entity = await audit.list_logs(conn, entity_id=case["id"], date_from=utc_today, date_to=utc_today)
    assert len(first["items"]) == 2 and len(rest["items"]) == 1 and rest["next_before"] is None
    assert first["items"][0]["entity_label"] == case["code"] and first["items"][0]["actor"]["name"] == "Boss"
    assert first["items"][0]["summary"] == "Updated notes"
    assert len(by_entity["items"]) == 3


async def test_bk005_privileges_and_settings(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for sql in (
            "UPDATE booking.settings SET value = '1'::jsonb",
            "INSERT INTO booking.countries VALUES ('XX', 'Nowhere', true)",
            "UPDATE booking.notification_reads SET read_at = now()",
        ):
            # The view is not auto-updatable either; either error means role booking cannot write it.
            with pytest.raises((asyncpg.InsufficientPrivilegeError, asyncpg.ObjectNotInPrerequisiteStateError)):
                await conn.execute(sql)
        transitions = await conn.fetch(
            "SELECT from_status, to_status FROM booking.trip_case_status_transitions"
            " WHERE (from_status, to_status) IN"
            " (('proposal_sent', 'confirmed'), ('confirmed', 'cancelled'), ('cancelled', 'new_inquiry'))"
        )
        assert len(transitions) == 3
        registered = {r["version"] for r in await conn.fetch("SELECT version FROM booking.v_schema_versions")}
        assert "bk-005" in registered


async def test_http_w1_routes_and_roles(migrated_booking_dsn: str, admin: asyncpg.Connection) -> None:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    # AA-1046: a staff-made case is retired (410); the version routes are exercised on a B2B case (and refused on a
    # kept b2c case).
    b2b = await ops.b2b_case(admin, tag="w1-http")
    # The audit row the B2B confirm writes for its new case (app.b2b.holds), for the audit-log reads below.
    await admin.execute(
        "INSERT INTO booking.audit_logs (actor_type, actor_ref, action, entity_type, entity_id, summary)"
        " VALUES ('system', 'b2b:confirm', 'trip_case.created', 'trip_case', $1::uuid, 'Created by the B2B confirm')",
        b2b["id"],
    )
    b2c = await inquiry_case(admin)
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    boss = {"Authorization": f"Bearer {cis.token(sub='boss', cis_role='admin')}"}
    cathy = {"Authorization": f"Bearer {cis.token(sub='cathy', cis_role='reviewer')}"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking") as client:
            assert (await client.get("/booking/staff/me", headers=boss)).status_code == 200
            await client.get("/booking/staff/me", headers=cathy)  # provisioned inactive
            await client.patch("/booking/staff/cathy", json={"active": True, "role": "contracting"}, headers=boss)
            created = await client.post("/booking/trip-cases", json={"pax": 2}, headers=boss)
            code = b2b["code"]
            denied = await client.post("/booking/trip-cases", json={}, headers=cathy)
            b2c_version = await client.post(f"/booking/trip-cases/{b2c['code']}/versions", headers=boss)
            summary = await client.get("/booking/trip-cases/summary", headers=cathy)
            directory = await client.get("/booking/staff/directory", headers=cathy)
            customers_gone = await client.get("/booking/customers?status=lead", headers=cathy)
            version = await client.post(f"/booking/trip-cases/{code}/versions", headers=boss)
            put = await client.put(
                f"/booking/trip-cases/{code}/versions/1/items",
                json={"items": [{"title": "Hanoi", "day_from": 1, "day_to": 1}]},
                headers=boss,
            )
            item_id = put.json()["items"][0]["id"]
            put_again = await client.put(
                f"/booking/trip-cases/{code}/versions/1/items",
                json={"items": [{"id": item_id, "title": "Hanoi old quarter", "day_from": 1, "day_to": 2}]},
                headers=boss,
            )
            version_get = await client.get(f"/booking/trip-cases/{code}/versions/1", headers=cathy)
            patch = await client.patch(f"/booking/trip-cases/{code}/versions/1", json={"title": "A"}, headers=boss)
            send_gone = await client.post(
                f"/booking/trip-cases/{code}/versions/1/send", json={"subject": "s", "message": "m"}, headers=boss
            )
            proposals = await client.get("/booking/versions?badge=draft", headers=cathy)
            activity = await client.get(f"/booking/trip-cases/{code}/activity", headers=cathy)
            notes = await client.get("/booking/notifications", headers=cathy)
            read = await client.post("/booking/notifications/read", json={"all": True}, headers=cathy)
            read_bad = await client.post("/booking/notifications/read", json={}, headers=cathy)
            prefs = await client.put(
                "/booking/staff/me/preferences", json={"notify": {"trip_case.assigned": False}}, headers=cathy
            )
            prefs_get = await client.get("/booking/staff/me/preferences", headers=cathy)
            settings_rows = await client.get("/booking/settings", headers=cathy)
            countries = await client.get("/booking/countries", headers=cathy)
            audit_denied = await client.get("/booking/audit-logs", headers=cathy)
            audit_ok = await client.get(
                f"/booking/audit-logs?entity_type=trip_case&to={datetime.now(UTC).date()}", headers=boss
            )
            audit_q = await client.get("/booking/audit-logs?q=TRIP_CASE.", headers=boss)
            audit_q_none = await client.get("/booking/audit-logs?q=zz-no-such-text-zz", headers=boss)
            staff_detail = await client.get("/booking/staff/cathy", headers=boss)
            deleted = await client.delete(f"/booking/trip-cases/{code}/versions/1", headers=boss)
            gone = await client.get(f"/booking/trip-cases/{code}/versions/1", headers=boss)
    finally:
        await db.close()
    assert created.status_code == 410 and created.json()["detail"] == "b2b_only"
    assert denied.status_code == 403 and b2c_version.status_code == 410
    assert summary.status_code == 200 and summary.json()["new_inquiry"] == 1  # the kept b2c case
    assert directory.status_code == 200 and {s["cis_user_id"] for s in directory.json()["items"]} == {"boss", "cathy"}
    assert customers_gone.status_code == 404
    assert version.status_code == 201 and put.status_code == 200
    assert put_again.json()["items"][0]["id"] == item_id
    assert version_get.json()["item_count"] == 1 and patch.json()["title"] == "A"
    assert send_gone.status_code in (404, 405)  # AA-1046: no Send to a traveller customer
    assert proposals.json()["total"] == 1 and proposals.json()["items"][0]["title"] == "A"
    assert activity.status_code == 200 and activity.json()["items"]
    assert notes.status_code == 200 and read.status_code == 200 and read_bad.status_code == 422
    assert prefs.json()["notify"]["trip_case.assigned"] is False and prefs_get.json() == prefs.json()
    assert settings_rows.json()["values"] == {
        "deposit_pct": 30,
        "quote_validity_days": 14,
        "b2b_quote_valid_hours": 48,
        "b2b_hold_hours": 48,
        "b2b_hold_extend_hours": 72,
        "b2b_rehold_limit_per_day": 3,
        "b2b_on_request_answer_business_days": 1,
        "b2b_ops_timezone": "Asia/Ho_Chi_Minh",
        "supplier_answer_days": 2,  # bk-026 (AA-682)
        "pre_departure_days": 14,
        "ops_docs_max_bytes": 10485760,
        "trip_check_reminder_days": 3,  # bk-027 (AA-682 S2)
        "emergency_contact_keep_days": 90,
        "deposit_due_days": 3,  # bk-028 (AA-682 S3)
        "balance_due_days_before_start": 30,
        "payment_due_soon_days": 7,
        "payment_overdue_reminder_days": [1, 7],
        "invoice_issuer": {
            "name": "Adventure Asia (legal name to be set)",
            "address": "(registered address to be set)",
        },
        "payment_instructions": "Payment details will be confirmed by your trip consultant.",
        "supplier_bill_due_soon_days": 3,  # bk-030 (AA-682 S4)
    }
    assert {"iso2": "VN", "name": "Vietnam"} in countries.json()["items"]
    assert audit_denied.status_code == 403 and audit_ok.status_code == 200 and audit_ok.json()["items"]
    # AA-996: free-text search (case-insensitive; the action `trip_case.created` matches) and a miss.
    assert audit_q.status_code == 200 and audit_q.json()["items"]
    assert audit_q_none.status_code == 200 and audit_q_none.json()["items"] == []
    assert staff_detail.json()["role"] == "contracting"
    assert deleted.status_code == 204 and gone.status_code == 404
