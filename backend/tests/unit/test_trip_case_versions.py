"""Proposal versions: V1 prefill ordering and titles, Vn copy, locks (real SQL: integration tests)."""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest

from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound
from app.trip_cases import versions
from app.trip_cases.versions import Item, leg_title, prefill_from_legs
from tests.unit.fakes import ScriptedConn

STAFF = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CASE_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"


def leg(position: int, tour: str, day_from: int, day_to: int) -> dict[str, Any]:
    return {"position": position, "source_tour_id": tour, "day_from": day_from, "day_to": day_to}


def case_row(status: str = "building", snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": CASE_ID,
        "code": "TC-000001",
        "status": status,
        "currency": "USD",
        "inquiry_snapshot": json.dumps(snapshot) if snapshot is not None else None,
    }


def version_row(number: int = 1, *, approved: bool = False, sent: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "id": VERSION_ID,
        "version_number": number,
        "is_approved": approved,
        "is_sent_to_customer": sent,
        "title": None,
        "notes": None,
        "total_price": None,
        "currency": "USD",
        "badge": "approved" if approved else ("sent" if sent else "draft"),
        **extra,
    }


def test_prefill_orders_legs_by_day_then_position() -> None:
    legs = [leg(1, "t-a", 4, 6), leg(3, "t-c", 1, 3), leg(2, "t-b", 1, 1)]
    items = prefill_from_legs(legs, None)
    assert [(i.source_tour_id, i.day_from, i.day_to) for i in items] == [("t-b", 1, 1), ("t-c", 1, 3), ("t-a", 4, 6)]
    assert items[0].title == "AA tour t-b"


def test_leg_title_uses_the_first_named_day_of_that_tour() -> None:
    snapshot = {
        "days": [
            {"day": 3, "source_tour_id": "t-a", "name": "Halong Bay"},
            {"day": 2, "source_tour_id": "t-a", "name": "  "},
            {"day": 1, "source_tour_id": "t-a", "name": "Hanoi Old Quarter"},
            {"day": 1, "source_tour_id": "t-b", "name": "Elsewhere"},
            {"day": "x", "source_tour_id": "t-a"},
            "junk",
        ]
    }
    assert leg_title(snapshot, "t-a") == "Hanoi Old Quarter"
    assert leg_title(snapshot, "t-z") == "AA tour t-z"
    assert leg_title({"days": None}, "t-a") == "AA tour t-a"


@pytest.mark.asyncio
async def test_create_v1_prefills_from_legs_in_day_order() -> None:
    snapshot = {"days": [{"day": 1, "source_tour_id": "t-b", "name": "Sapa trek"}]}
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row(snapshot=snapshot))
        .on("FROM booking.trip_case_legs", [leg(1, "t-a", 3, 5), leg(2, "t-b", 1, 2)])
        .on("INSERT INTO booking.trip_case_versions", VERSION_ID)
        .on("FROM booking.trip_case_versions v", version_row(1))
        .on("SELECT id::text AS id, position, title", [{"position": 1}, {"position": 2}])
    )
    out = await versions.create_version(conn, STAFF, "TC-000001", "first draft")
    assert out["version_number"] == 1 and len(out["items"]) == 2
    insert_version = conn.sql("INSERT INTO booking.trip_case_versions")[0]
    assert insert_version == (CASE_ID, 1, None, "first draft", None, "USD", None, "u-1")
    items = conn.sql("INSERT INTO booking.trip_case_version_itinerary")
    assert items == [
        (VERSION_ID, 1, "Sapa trek", 1, 2, "t-b", None, None, None),
        (VERSION_ID, 2, "AA tour t-a", 3, 5, "t-a", None, None, None),
    ]
    audit = json.loads(conn.sql("INSERT INTO booking.audit_logs")[0][7])
    assert audit["version_number"] == 1 and audit["copied_from"] is None and audit["item_count"] == 2


@pytest.mark.asyncio
async def test_create_vn_copies_the_latest_version() -> None:
    latest = {
        "id": "v-2",
        "version_number": 2,
        "title": "Option B",
        "notes": "v2 notes",
        "currency": "EUR",
        "channel": "b2c_tailor_made",
    }
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("ORDER BY version_number DESC", latest)
        .on("coalesce(max(version_number), 0) + 1", 3)
        .on("INSERT INTO booking.trip_case_versions", VERSION_ID)
        .on("FROM booking.trip_case_versions v", version_row(3))
    )
    out = await versions.create_version(conn, STAFF, "TC-000001", None)
    assert out["version_number"] == 3
    assert conn.sql("INSERT INTO booking.trip_case_versions")[0] == (
        CASE_ID,
        3,
        "Option B",
        "v2 notes",
        None,  # the total is never copied: V3 starts unpriced (review finding 2)
        "EUR",
        "b2c_tailor_made",
        "u-1",
    )
    assert conn.sql("SELECT $1::uuid, position") == [(VERSION_ID, "v-2")]
    assert conn.sql("FROM booking.trip_case_legs") == []


@pytest.mark.asyncio
async def test_closed_case_refuses_versions_and_edits() -> None:
    conn = ScriptedConn().on("FROM booking.trip_cases tc", case_row("cancelled"))
    with pytest.raises(Conflict):
        await versions.create_version(conn, STAFF, "TC-000001", None)
    with pytest.raises(Conflict):
        await versions.replace_items(conn, STAFF, "TC-000001", 1, [])


@pytest.mark.asyncio
async def test_get_items_and_missing_version() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.trip_case_versions v", version_row(1))
        .on("SELECT id::text AS id, position, title", [{"position": 1, "title": "x"}])
    )
    out = await versions.get_items(conn, "TC-000001", 1)
    assert out["items"] == [{"position": 1, "title": "x", "costs": [], "supply": None, "cis": None}]
    assert out["fx_locked"] is None
    with pytest.raises(NotFound):
        await versions.get_items(ScriptedConn().on("FROM booking.trip_cases tc", case_row()), "TC-000001", 9)


ITEM_A = "33333333-3333-4333-8333-333333333333"
ITEM_B = "44444444-4444-4444-8444-444444444444"


def item_row(item_id: str, position: int, title: str, *, day_from: int = 1, day_to: int = 1) -> dict[str, Any]:
    return {
        "id": item_id,
        "position": position,
        "title": title,
        "day_from": day_from,
        "day_to": day_to,
        "source_tour_id": None,
        "notes": None,
    }


@pytest.mark.asyncio
async def test_replace_items_upserts_by_id_and_deletes_only_removed_items() -> None:
    existing = [item_row(ITEM_A, 1, "A"), item_row(ITEM_B, 2, "B")]
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.trip_case_versions v", version_row(1, total_price="1500.00"))
        .on("SELECT id::text AS id, position, title", existing)
    )
    items = [Item("B first", 1, 2, None, "kept", id=ITEM_B), Item("New", 3, 5, "t-x")]
    await versions.replace_items(conn, STAFF, "TC-000001", 1, items)
    # The item list changed, so the price set for the old list is cleared ($3 = true).
    assert conn.sql("total_price = CASE WHEN $3") == [(VERSION_ID, "u-1", True)]
    assert conn.sql("DELETE FROM booking.trip_case_version_itinerary") == [([ITEM_A],)]
    assert conn.sql("SET position = position + $2") == [([ITEM_B], versions.POSITION_PARK)]
    assert conn.sql("SET position = $2, title = $3") == [(ITEM_B, 1, "B first", 1, 2, None, "kept", None, None)]
    assert conn.sql("INSERT INTO booking.trip_case_version_itinerary") == [
        (VERSION_ID, 2, "New", 3, 5, "t-x", None, None, None)
    ]
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[3] == "trip_case_version.items_replaced" and json.loads(audit[7])["removed"] == [ITEM_A]
    assert json.loads(audit[7])["price_cleared"] is True


@pytest.mark.asyncio
async def test_replace_items_with_the_same_list_keeps_the_price() -> None:
    existing = [item_row(ITEM_A, 1, "A", day_from=1, day_to=2)]
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.trip_case_versions v", version_row(1, total_price="1500.00"))
        .on("SELECT id::text AS id, position, title", existing)
    )
    await versions.replace_items(conn, STAFF, "TC-000001", 1, [Item("A", 1, 2, id=ITEM_A)])
    assert conn.sql("total_price = CASE WHEN $3") == [(VERSION_ID, "u-1", False)]


@pytest.mark.asyncio
async def test_replace_items_rejects_foreign_or_duplicate_ids() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.trip_case_versions v", version_row(1))
        .on("SELECT id::text AS id, position, title", [{"id": ITEM_A, "position": 1, "title": "A"}])
    )
    with pytest.raises(Invalid) as foreign:
        await versions.replace_items(conn, STAFF, "TC-000001", 1, [Item("x", 1, 1, id=ITEM_B)])
    assert foreign.value.code == "unknown_item"
    with pytest.raises(Invalid) as dup:
        await versions.replace_items(
            conn, STAFF, "TC-000001", 1, [Item("x", 1, 1, id=ITEM_A), Item("y", 1, 1, id=ITEM_A)]
        )
    assert dup.value.code == "duplicate_item_id"
    assert conn.sql("DELETE FROM") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "code"), [(version_row(approved=True), "version_locked"), (version_row(sent=True), "version_sent")]
)
async def test_replace_items_refuses_locked_or_sent_versions(row: dict[str, Any], code: str) -> None:
    conn = ScriptedConn().on("FROM booking.trip_cases tc", case_row()).on("FROM booking.trip_case_versions v", row)
    with pytest.raises(Conflict) as err:
        await versions.replace_items(conn, STAFF, "TC-000001", 1, [])
    assert err.value.code == code
    assert conn.sql("DELETE FROM") == []


@pytest.mark.asyncio
async def test_replace_items_maps_the_db_lock_to_409() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.trip_case_versions v", version_row(1))
        .on("INSERT INTO booking.trip_case_version_itinerary", asyncpg.ObjectNotInPrerequisiteStateError("locked"))
    )
    with pytest.raises(Conflict) as err:
        await versions.replace_items(conn, STAFF, "TC-000001", 1, [Item("x", 1, 1)])
    assert err.value.code == "version_locked"
