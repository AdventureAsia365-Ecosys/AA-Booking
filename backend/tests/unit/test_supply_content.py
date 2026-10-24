"""AA-835 S4 read-back of CIS content (app.supply.content): off until CIS 278 serves catalog.v_supply_content
(feature detection), fail soft on a catalog error, only published text, a CIS-shaped edit path only, https photos
only; the hotel / service detail and the proposal items carry it. The SQL against a stand-in view on PostgreSQL is
tests/integration/test_bk023_media_api.py."""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest

from app.catalog import liveness
from app.supply import content, records
from app.supply.records import HOTEL
from app.trip_cases import versions
from tests.unit.fakes import VIEW_READY_FRAGMENT, ScriptedConn
from tests.unit.test_supply import HID, SID, hotel_conn
from tests.unit.test_trip_case_versions import case_row, version_row

CONTENT_ONE = "AND c.source_id = $1 AND c.entity_type = $2"
CONTENT_MANY = "AND c.source_id = ANY($1::text[])"


def content_row(status: str = "published", **kw: Any) -> dict[str, Any]:
    return {
        "entity_type": "hotel",
        "source_id": HID,
        "content_status": status,
        "content_version": 3,
        "description": "A quiet garden hotel." if status == "published" else None,
        "highlights": json.dumps(["Garden", "Pool"]) if status == "published" else None,
        "photo_urls": json.dumps(["https://img.example/a.webp", "http://img.example/b.webp", "javascript:x"]),
        "edit_url": "/admin/supply-content/42",
    } | kw


def ready(conn: ScriptedConn, value: bool = True) -> ScriptedConn:
    return conn.on(VIEW_READY_FRAGMENT, value)


@pytest.mark.asyncio
async def test_off_until_the_view_exists() -> None:
    conn = ScriptedConn()
    assert await content.read_content(conn, "hotel", HID) is None
    assert await content.published_texts(conn, {("hotel", HID)}) == {}
    assert not conn.sql(CONTENT_ONE) and not conn.sql(CONTENT_MANY)
    detect = conn.sql(VIEW_READY_FRAGMENT)[0]
    assert detect[:2] == ("catalog", "v_supply_content") and "edit_url" in detect[2]


@pytest.mark.asyncio
async def test_published_row() -> None:
    conn = ready(ScriptedConn()).on(CONTENT_ONE, content_row())
    out = await content.read_content(conn, "hotel", HID)
    assert out == {
        "content_status": "published",
        "content_version": 3,
        "description": "A quiet garden hotel.",
        "highlights": ["Garden", "Pool"],
        "photo_urls": ["https://img.example/a.webp"],
        "edit_url": "/admin/supply-content/42",
    }
    assert conn.sql(CONTENT_ONE) == [(HID, "hotel")]


@pytest.mark.parametrize("status", ["draft", "in_review", "stale", "unpublished"])
@pytest.mark.asyncio
async def test_unpublished_rows_carry_no_text_even_if_the_view_leaks_it(status: str) -> None:
    row = content_row(status, description="leaked", highlights=json.dumps(["x"]))
    out = await content.read_content(ready(ScriptedConn()).on(CONTENT_ONE, row), "hotel", HID)
    assert out is not None
    assert out["content_status"] == status and out["description"] is None
    assert out["highlights"] == [] and out["photo_urls"] == []
    assert out["edit_url"] == "/admin/supply-content/42"


@pytest.mark.asyncio
async def test_no_row_and_odd_values() -> None:
    assert await content.read_content(ready(ScriptedConn()), "service", SID) == content.EMPTY
    for edit_url in (
        "https://evil.example/admin/supply-content/1",
        "javascript:alert(1)",
        "/admin/supply-content/../users",
        "//evil.example/x",
        None,
    ):
        conn = ready(ScriptedConn()).on(CONTENT_ONE, content_row(edit_url=edit_url, highlights="not json"))
        out = await content.read_content(conn, "hotel", HID)
        assert out is not None and out["edit_url"] is None and out["highlights"] == []


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.UndefinedTableError("relation catalog.v_supply_content does not exist"),
        asyncpg.InsufficientPrivilegeError("permission denied for view v_supply_content"),
        asyncpg.QueryCanceledError("canceling statement due to statement timeout"),
    ],
)
@pytest.mark.asyncio
async def test_catalog_errors_fail_soft(error: Exception) -> None:
    conn = ready(ScriptedConn()).on(CONTENT_ONE, error).on(CONTENT_MANY, error)
    assert await content.read_content(conn, "hotel", HID) is None
    assert await content.published_texts(conn, {("hotel", HID)}) == {}


@pytest.mark.asyncio
async def test_published_texts_match_type_and_id() -> None:
    rows = [
        content_row(),
        content_row(entity_type="service", source_id=HID),  # same id, other kind: not asked for
        content_row(source_id=SID, description=None),  # published without text: nothing to show
    ]
    conn = ready(ScriptedConn()).on(CONTENT_MANY, rows)
    out = await content.published_texts(conn, {("hotel", HID), ("hotel", SID)})
    assert out == {("hotel", HID): {"description": "A quiet garden hotel.", "highlights": ["Garden", "Pool"]}}
    assert "content_status = 'published'" in conn.calls[-1][0]
    assert conn.sql(CONTENT_MANY) == [([HID, SID],)]


@pytest.mark.asyncio
async def test_hotel_detail_carries_content() -> None:
    hotel = await records.get_record(hotel_conn(), HOTEL, "HTL-000001")
    assert hotel["content"] is None  # read-back off: the UI hides the panel
    liveness.reset_cache()  # the detection is cached per process
    conn = ready(hotel_conn()).on(CONTENT_ONE, content_row("stale"))
    hotel = await records.get_record(conn, HOTEL, "HTL-000001")
    assert hotel["content"]["content_status"] == "stale" and hotel["content"]["description"] is None
    activity_sql = conn.sql("FROM booking.audit_logs l")
    assert activity_sql and any("entity_media" in flat for flat, _ in conn.calls if "booking.audit_logs l" in flat)


def item(supply: dict[str, Any] | None) -> dict[str, Any]:
    return {"id": "i-1", "position": 1, "title": "x", "supply": json.dumps(supply) if supply else None}


ROOM = {"kind": "room_type", "id": "r-1", "code": "HTL-000001", "label": "Emerald · Deluxe", "active": True}
SERVICE = {"kind": "service", "id": SID, "code": "SVC-000001", "label": "Food walk", "active": True}


def items_conn(rows: list[dict[str, Any]], *, view: bool = True) -> ScriptedConn:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.trip_case_versions v", version_row(1))
        .on("SELECT id::text AS id, position, title", rows)
    )
    return ready(conn, view)


@pytest.mark.asyncio
async def test_proposal_items_carry_only_published_text() -> None:
    rows = [
        item({**ROOM, "entity_type": "hotel", "entity_id": HID}),
        item({**SERVICE, "entity_type": "service", "entity_id": SID}),
        item(None),
    ]
    conn = items_conn(rows).on(CONTENT_MANY, [content_row()])
    out = await versions.get_items(conn, "TC-000001", 1)
    room, service, free = (i["supply"] for i in out["items"])
    assert room["description"] == "A quiet garden hotel." and room["highlights"] == ["Garden", "Pool"]
    assert service["description"] is None and service["highlights"] == []
    assert free is None
    assert sorted(conn.sql(CONTENT_MANY)[0][0]) == sorted([HID, SID])


@pytest.mark.asyncio
async def test_proposal_items_without_the_view_or_supply_skip_the_catalog() -> None:
    conn = items_conn([item({**ROOM, "entity_type": "hotel", "entity_id": HID})], view=False)
    out = await versions.get_items(conn, "TC-000001", 1)
    assert out["items"][0]["supply"]["description"] is None and not conn.sql(CONTENT_MANY)
    conn = items_conn([item(None)])
    await versions.get_items(conn, "TC-000001", 1)
    assert not conn.sql(VIEW_READY_FRAGMENT)
