"""bk-005 + the Wave 1 staff endpoints on a real PostgreSQL, as role ``booking``.

Covers D2 (item ids survive a save, so rows referencing them survive too), D3 (send -> approve ->
confirmed, reopen), D4 (staff directory), the sent lock, case create, customers, notifications, settings,
the cross-case version list, activity and audit reads, and the bk-005 privileges.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app import audit
from app.auth import admin as staff_admin
from app.auth.staff import Staff, provision
from app.auth.tokens import StaffClaims
from app.customers import service as customers
from app.db import Database
from app.errors import Conflict, Forbidden, Invalid
from app.main import create_app
from app.notifications import service as notifications
from app.settings import Settings
from app.trip_cases import service, versions
from app.trip_cases.versions import Item
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TABLES = (
    "booking.notification_reads, booking.trip_case_countries, booking.trip_case_version_itinerary, "
    "booking.trip_case_versions, booking.staff_assignments, booking.trip_case_status_history, "
    "booking.trip_case_legs, booking.notifications, booking.trip_cases, booking.staff_users, booking.audit_logs, "
    "booking.customers"
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
        await provision(conn, StaffClaims(sub, f"{sub}@example.test", sub.title(), cis_role), role)
    await admin.execute("UPDATE booking.staff_users SET active = true, role = $2 WHERE cis_user_id = $1", sub, role)
    return Staff(sub, f"{sub}@example.test", sub.title(), role, True, cis_role)


async def inquiry_case(pool: asyncpg.Pool, legs: list[tuple[int, str, int, int]] = ()) -> dict[str, Any]:
    snapshot = {"days": [{"day": 1, "source_tour_id": "tour-b", "name": "Sapa trek"}], "custom_days": []}
    async with pool.acquire() as conn, conn.transaction():
        customer = await conn.fetchval(
            "INSERT INTO booking.customers (name, email, source) VALUES ('Ann Lead', $1, 'tripplanner_lead')"
            " RETURNING id::text",
            f"{uuid.uuid4().hex}@x.test",
        )
        case = await conn.fetchrow(
            "INSERT INTO booking.trip_cases (customer_id, source_system, inquiry_snapshot, currency)"
            " VALUES ($1::uuid, 'tripplanner', $2::jsonb, 'USD') RETURNING id::text AS id, code",
            customer,
            json.dumps(snapshot),
        )
        await conn.executemany(
            "INSERT INTO booking.trip_case_legs (trip_case_id, position, source_tour_id, day_from, day_to)"
            " VALUES ($1::uuid, $2, $3, $4, $5)",
            [(case["id"], *leg) for leg in legs],
        )
    return {**dict(case), "customer_id": customer}


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
    case = await inquiry_case(pool, [(1, "tour-a", 1, 3), (2, "tour-b", 4, 5)])
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


async def test_d3_send_approve_confirms_and_locks(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    sales = await staff(pool, admin, "sam", "sales")
    case = await inquiry_case(pool, [(1, "tour-a", 1, 3)])
    async with pool.acquire() as conn, conn.transaction():
        await service.assign(conn, boss, case["code"], "sam", "sales")
    await move(pool, boss, case["code"], "discovery")
    v1 = await priced_v1(pool, admin, sales, case)
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as approve_early:
            await versions.approve_version(conn, sales, case["code"], 1, None)
        assert approve_early.value.code == "version_not_sent"
    today = date.today()
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid):
            await versions.send_version(
                conn, sales, case["code"], 1, to=None, subject="s", message="m", valid_until=today + timedelta(days=3)
            )
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid) as mismatch:
            await versions.send_version(
                conn, sales, case["code"], 1, to="someone@else.test", subject="s", message="m", valid_until=None
            )
    assert mismatch.value.code == "recipient_mismatch"
    async with pool.acquire() as conn, conn.transaction():
        sent = await versions.send_version(
            conn, sales, case["code"], 1, to=None, subject="Your trip", message="Dear Ann", valid_until=None
        )
    assert sent["badge"] == "sent" and sent["valid_until"] == today + timedelta(days=14)
    assert sent["trip_case"]["status"] == "proposal_sent" and sent["email_sent"] is False
    history = await admin.fetch(
        "SELECT from_status, to_status, changed_by, note FROM booking.trip_case_status_history"
        " WHERE trip_case_id = $1::uuid ORDER BY changed_at, seq",
        case["id"],
    )
    assert [(h["from_status"], h["to_status"]) for h in history][-2:] == [
        ("discovery", "building"),
        ("building", "proposal_sent"),
    ]
    assert history[-1]["changed_by"] == "sam" and history[-1]["note"] == "V1 sent"
    note = await admin.fetchrow(
        "SELECT recipient_type, recipient_id::text AS rid, type, title, data FROM booking.notifications"
        " WHERE type = 'proposal.sent'"
    )
    assert (note["recipient_type"], note["rid"], note["title"]) == ("customer", case["customer_id"], "Your trip")
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
    assert "trip_case_version.sent" in actions and "trip_case_version.approved" in actions


async def test_d3_resend_supersedes_and_reopen_needs_reason(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(pool, [(1, "tour-a", 1, 2)])
    await move(pool, boss, case["code"], "discovery", "building")
    await priced_v1(pool, admin, boss, case)
    async with pool.acquire() as conn, conn.transaction():
        await versions.send_version(conn, boss, case["code"], 1, to=None, subject="s", message="m", valid_until=None)
        v2 = await versions.create_version(conn, boss, case["code"], "second try")
    # V2 starts unpriced (review finding 2); price it out of band like V1.
    await admin.execute("UPDATE booking.trip_case_versions SET total_price = 2100 WHERE id = $1::uuid", v2["id"])
    async with pool.acquire() as conn, conn.transaction():
        await versions.send_version(conn, boss, case["code"], 2, to=None, subject="s2", message="m2", valid_until=None)
    async with pool.acquire() as conn, conn.transaction():
        listed = await versions.list_versions(conn, q=case["code"])
        with pytest.raises(Conflict) as superseded:
            await versions.approve_version(conn, boss, case["code"], 1, None)
    assert {v["version_number"]: v["badge"] for v in listed["items"]} == {1: "superseded", 2: "sent"}
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
    case = await inquiry_case(pool, [(1, "tour-a", 1, 2), (2, "tour-b", 3, 4)])
    await move(pool, boss, case["code"], "discovery", "building")
    v1 = await priced_v1(pool, admin, boss, case)
    await admin.execute("UPDATE booking.trip_case_versions SET channel = 'b2b' WHERE id = $1::uuid", v1["id"])
    async with pool.acquire() as conn, conn.transaction():
        v2 = await versions.create_version(conn, boss, case["code"], None)
    assert v2["total_price"] is None and v2["currency"] == "USD" and v2["channel"] == "b2b"
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid) as unpriced:
            await versions.send_version(
                conn, boss, case["code"], 2, to=None, subject="s", message="m", valid_until=None
            )
    assert unpriced.value.code == "price_missing"
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
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Invalid) as stale:
            await versions.send_version(
                conn, boss, case["code"], 2, to=None, subject="s", message="m", valid_until=None
            )
    assert stale.value.code == "price_missing"
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
    case = await inquiry_case(pool, [(1, "tour-a", 1, 2)])
    await move(pool, boss, case["code"], "discovery", "building")
    v1 = await priced_v1(pool, admin, boss, case)
    async with pool.acquire() as conn, conn.transaction():
        await versions.send_version(conn, boss, case["code"], 1, to=None, subject="s", message="m", valid_until=None)
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
    case = await inquiry_case(pool)
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


async def test_create_case_with_new_lead_and_list_filters(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    sam = await staff(pool, admin, "sam", "sales")
    existing = await inquiry_case(pool, [(1, "tour-b", 1, 2)])
    existing_email = await admin.fetchval(
        "SELECT email FROM booking.customers WHERE id = $1::uuid", existing["customer_id"]
    )
    async with pool.acquire() as conn, conn.transaction():
        created = await service.create_case(
            conn,
            sam,
            {
                "new_customer": {"name": "Bob Phone", "email": existing_email.upper(), "contact_consent": True},
                "pax_adults": 2,
                "pax_children": 1,
                "countries": ["vn", "KH"],
                "trip_style": "family",
                "initial_status": "discovery",
            },
        )
    assert created["source_system"] == "manual" and created["status"] == "discovery"
    assert (created["pax"], created["pax_adults"], created["pax_children"]) == (3, 2, 1)
    assert created["countries"] == ["VN", "KH"] and created["currency"] == "USD"
    assert [a["cis_user_id"] for a in created["assignees"]] == ["sam"]  # sales creator assigned by default
    lead = await admin.fetchrow(
        "SELECT code, verified, source, needs_merge_review, merge_candidate_id::text AS cand, contact_consent_at"
        " FROM booking.customers WHERE id = $1::uuid",
        created["customer_id"],
    )
    assert lead["code"].startswith("CUS-") and lead["verified"] is False and lead["source"] == "manual"
    assert lead["needs_merge_review"] and lead["cand"] == existing["customer_id"] and lead["contact_consent_at"]
    with pytest.raises(Invalid):  # raised through the transaction: everything rolls back
        async with pool.acquire() as conn, conn.transaction():
            await service.create_case(
                conn, sam, {"new_customer": {"name": "x", "email": "y@z.test"}, "countries": ["ZZ"]}
            )
    with pytest.raises(Invalid):
        async with pool.acquire() as conn, conn.transaction():
            await service.update_case(conn, sam, created["code"], {"pax": 9})  # split says 3
    async with pool.acquire() as conn, conn.transaction():
        upd = await service.update_case(conn, sam, created["code"], {"pax_children": 0, "countries": ["TH"]})
    assert upd["changed"] == ["countries", "pax", "pax_children"] and upd["pax"] == 2 and upd["countries"] == ["TH"]
    async with pool.acquire() as conn:
        by_q = await service.list_cases(conn, q="bob phone")
        by_source = await service.list_cases(conn, source="tripplanner")
        by_customer_code = await service.list_cases(conn, q=lead["code"].lower())
        sorted_up = await service.list_cases(conn, sort="-updated_at", statuses=["discovery", "new_inquiry"])
        summary = await service.summary(conn, sam)
    assert [c["code"] for c in by_q["items"]] == [created["code"]]
    assert by_source["total"] == 1 and by_source["items"][0]["trip_title"] == "Sapa trek"
    assert by_source["items"][0]["leg_titles"] == ["Sapa trek"] and by_source["items"][0]["duration_days"] == 2
    assert by_customer_code["total"] == 1
    assert sorted_up["items"][0]["code"] == created["code"]
    assert summary["open"] == 2 and summary["new_inquiry"] == 1 and summary["mine"] == 1
    assert summary["by_status"]["discovery"] == 1 and summary["mom_delta"]["created_this_month"] == 2


async def test_activity_merges_history_and_audit(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    case = await inquiry_case(pool)
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


async def test_customers_status_detail_edit_archive(pool: asyncpg.Pool, admin: asyncpg.Connection) -> None:
    boss = await staff(pool, admin, "boss")
    sam = await staff(pool, admin, "sam", "sales")
    inquiry = await inquiry_case(pool, [(1, "tour-b", 1, 2)])
    active = await inquiry_case(pool)
    await move(pool, boss, active["code"], "discovery")
    async with pool.acquire() as conn, conn.transaction():
        lead = await customers.create_lead(
            conn, sam, {"name": " Cara ", "phone": "+84 90 000", "contact_consent": False}
        )
    async with pool.acquire() as conn:
        everyone = await customers.list_customers(conn)
        leads = await customers.list_customers(conn, status="lead")
        actives = await customers.list_customers(conn, status="active")
        by_q = await customers.list_customers(conn, q="cara", source="manual")
        detail = await customers.get_customer(conn, inquiry["customer_id"])
        by_code = await customers.get_customer(conn, lead["code"].lower())
    assert everyone["total"] == 3
    assert {c["id"] for c in leads["items"]} == {inquiry["customer_id"], lead["id"]}
    assert [c["id"] for c in actives["items"]] == [active["customer_id"]] and actives["items"][0]["case_count"] == 1
    assert [c["name"] for c in by_q["items"]] == ["Cara"]
    assert detail["status"] == "lead" and [c["code"] for c in detail["cases"]] == [inquiry["code"]]
    assert detail["inquiries"][0]["trip_title"] == "Sapa trek"
    assert by_code["id"] == lead["id"]
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Forbidden):
            await customers.update_customer(conn, sam, lead["id"], {"is_blocked": True})
        edited = await customers.update_customer(conn, sam, lead["id"], {"email": "Cara@X.test", "address": "Hanoi"})
        blocked = await customers.update_customer(conn, boss, lead["id"], {"is_blocked": True})
    assert edited["changed"] == ["address", "email"] and edited["email"] == "cara@x.test"
    assert blocked["is_blocked"] is True
    await admin.execute("UPDATE booking.customers SET verified = true WHERE id = $1::uuid", lead["id"])
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as locked:
            await customers.update_customer(conn, sam, lead["id"], {"email": "new@x.test"})
    assert locked.value.code == "email_locked"
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict) as has_cases:
            await customers.archive_customer(conn, boss, active["customer_id"])
        assert has_cases.value.extra["trip_cases"] == [active["code"]]
    async with pool.acquire() as conn, conn.transaction():
        await customers.archive_customer(conn, boss, inquiry["customer_id"])
    status = await admin.fetchval("SELECT status FROM booking.trip_cases WHERE id = $1::uuid", inquiry["id"])
    assert status == "cancelled"
    async with pool.acquire() as conn:
        after = await customers.list_customers(conn)
        blocked_list = await customers.list_customers(conn, status="blocked")
    assert after["total"] == 2 and [c["id"] for c in blocked_list["items"]] == [lead["id"]]
    async with pool.acquire() as conn, conn.transaction():
        with pytest.raises(Conflict):
            await service.create_case(conn, sam, {"customer_id": lead["id"]})  # blocked


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
    case = await inquiry_case(pool)
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
    case = await inquiry_case(pool)
    async with pool.acquire() as conn, conn.transaction():
        for i in range(3):
            await service.update_case(conn, boss, case["code"], {"notes": f"n{i}"})
    async with pool.acquire() as conn:
        first = await audit.list_logs(conn, actor="boss", entity_type="trip_case", limit=2)
        rest = await audit.list_logs(conn, actor="boss", entity_type="trip_case", limit=2, before=first["next_before"])
        by_entity = await audit.list_logs(conn, entity_id=case["id"], date_from=date.today(), date_to=date.today())
    assert len(first["items"]) == 2 and len(rest["items"]) == 1 and rest["next_before"] is None
    assert first["items"][0]["entity_label"] == case["code"] and first["items"][0]["actor"]["name"] == "Boss"
    assert first["items"][0]["summary"] == "Updated notes"
    assert len(by_entity["items"]) == 3


async def test_bk005_privileges_and_settings(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for sql in (
            "UPDATE booking.settings SET value = '1'::jsonb",
            "INSERT INTO booking.countries VALUES ('XX', 'Nowhere', true)",
            "DELETE FROM booking.v_customer_summary",
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
        version = await conn.fetchval("SELECT max(version) FROM booking.v_schema_versions")
        assert version == "bk-005"


async def test_http_w1_routes_and_roles(migrated_booking_dsn: str, admin: asyncpg.Connection) -> None:
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    boss = {"Authorization": f"Bearer {cis.token(sub='boss', cis_role='admin')}"}
    cathy = {"Authorization": f"Bearer {cis.token(sub='cathy', cis_role='reviewer')}"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking") as client:
            assert (await client.get("/booking/staff/me", headers=boss)).status_code == 200
            await client.get("/booking/staff/me", headers=cathy)  # provisioned inactive
            await client.patch("/booking/staff/cathy", json={"active": True, "role": "contracting"}, headers=boss)
            created = await client.post(
                "/booking/trip-cases",
                json={"new_customer": {"name": "Dee", "email": "dee@x.test", "contact_consent": True}, "pax": 2},
                headers=boss,
            )
            code = created.json()["code"]
            denied = await client.post(
                "/booking/trip-cases", json={"new_customer": {"name": "E", "phone": "+1 555"}}, headers=cathy
            )
            bad_body = await client.post("/booking/trip-cases", json={"pax": 2}, headers=boss)
            summary = await client.get("/booking/trip-cases/summary", headers=cathy)
            directory = await client.get("/booking/staff/directory", headers=cathy)
            customers_list = await client.get("/booking/customers?status=lead", headers=cathy)
            customer = await client.get(f"/booking/customers/{created.json()['customer_id']}", headers=cathy)
            archive_denied = await client.delete(f"/booking/customers/{created.json()['customer_id']}", headers=cathy)
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
            send_no_price = await client.post(
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
            audit_ok = await client.get(f"/booking/audit-logs?entity_type=trip_case&to={date.today()}", headers=boss)
            staff_detail = await client.get("/booking/staff/cathy", headers=boss)
            deleted = await client.delete(f"/booking/trip-cases/{code}/versions/1", headers=boss)
            gone = await client.get(f"/booking/trip-cases/{code}/versions/1", headers=boss)
    finally:
        await db.close()
    assert created.status_code == 201 and code.startswith("TC-")
    assert denied.status_code == 403 and bad_body.status_code == 422
    assert summary.status_code == 200 and summary.json()["new_inquiry"] == 1
    assert directory.status_code == 200 and {s["cis_user_id"] for s in directory.json()["items"]} == {"boss", "cathy"}
    assert customers_list.json()["total"] == 1 and customer.json()["code"].startswith("CUS-")
    assert archive_denied.status_code == 403
    assert version.status_code == 201 and put.status_code == 200
    assert put_again.json()["items"][0]["id"] == item_id
    assert version_get.json()["item_count"] == 1 and patch.json()["title"] == "A"
    assert send_no_price.status_code == 409  # case is new_inquiry: convert it first
    assert proposals.json()["total"] == 1 and proposals.json()["items"][0]["title"] == "A"
    assert activity.status_code == 200 and activity.json()["items"]
    assert notes.status_code == 200 and read.status_code == 200 and read_bad.status_code == 422
    assert prefs.json()["notify"]["trip_case.assigned"] is False and prefs_get.json() == prefs.json()
    assert settings_rows.json()["values"] == {"deposit_pct": 30, "quote_validity_days": 14}
    assert {"iso2": "VN", "name": "Vietnam"} in countries.json()["items"]
    assert audit_denied.status_code == 403 and audit_ok.status_code == 200 and audit_ok.json()["items"]
    assert staff_detail.json()["role"] == "contracting"
    assert deleted.status_code == 204 and gone.status_code == 404
