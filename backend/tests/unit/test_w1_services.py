"""Python branches of the Wave 1 services around the SQL (real SQL: tests/integration/test_bk005_w1.py)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app import audit, paging, reference
from app.auth import admin as staff_admin
from app.auth.staff import Staff
from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.notifications import service as notifications
from app.trip_cases import service, versions

pytestmark = pytest.mark.asyncio

from tests.unit.fakes import ScriptedConn  # noqa: E402

ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CASE_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"
CUSTOMER_ID = "55555555-5555-4555-8555-555555555555"
ROW_ID = "88888888-8888-4888-8888-888888888888"
AT = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def case_row(status: str = "building", **extra: Any) -> dict[str, Any]:
    return {
        "id": CASE_ID,
        "code": "TC-000001",
        "status": status,
        "currency": "USD",
        "pax": 2,
        "pax_adults": None,
        "pax_children": None,
        "trip_style": None,
        "start_date": None,
        "notes": None,
        "inquiry_snapshot": None,
        **extra,
    }


def version_row(**extra: Any) -> dict[str, Any]:
    return {
        "id": VERSION_ID,
        "version_number": 1,
        "title": None,
        "notes": None,
        "total_price": "1990.00",
        "currency": "USD",
        "is_sent_to_customer": False,
        "is_approved": False,
        "badge": "draft",
        "fx_rate_locked": None,
        "fx_as_of": None,
        **extra,
    }


def audit_args(conn: ScriptedConn) -> list[tuple[Any, ...]]:
    return conn.sql("INSERT INTO booking.audit_logs")


# ── paging / audit / reference ───────────────────────────────────────────────


async def test_paging_cursor_round_trip_and_errors() -> None:
    cursor = paging.encode(AT, ROW_ID)
    assert paging.decode(cursor) == (AT, ROW_ID)
    assert paging.decode(None) is None
    for bad in ("nonsense", f"{AT.isoformat()}|not-a-uuid", f"2026-10-14T09:00:00|{ROW_ID}"):
        with pytest.raises(Invalid):
            paging.decode(bad)
    assert paging.next_cursor([{"at": AT, "id": ROW_ID}], 1) == cursor
    assert paging.next_cursor([{"at": AT, "id": ROW_ID}], 2) is None
    assert paging.contains(" 50%_a\\b ") == "%50\\%\\_a\\\\b%"


async def test_audit_record_and_list_logs() -> None:
    conn = ScriptedConn()
    await audit.record(
        conn, actor_type="staff", actor_ref="u-1", action="x", entity_type="t", entity_id=None, summary="s" * 600
    )
    assert len(conn.sql("INSERT INTO booking.audit_logs")[0][9]) == 500
    row = {
        "id": ROW_ID,
        "created_at": AT,
        "actor_type": "staff",
        "actor_ref": "u-1",
        "actor_name": "Sales One",
        "action": "trip_case.updated",
        "entity_type": "trip_case",
        "entity_id": CASE_ID,
        "entity_label": "TC-000001",
        "summary": "Updated notes",
        "before": json.dumps({"notes": None}),
        "after": None,
    }
    conn = ScriptedConn().on("FROM booking.audit_logs l", [row])
    out = await audit.list_logs(
        conn,
        actor="u-1",
        entity_type="trip_case",
        entity_id=CASE_ID,
        date_from=date(2026, 10, 1),
        date_to=date(2026, 10, 14),
        before=paging.encode(AT, ROW_ID),
        limit=1,
    )
    assert out["items"][0]["actor"] == {"type": "staff", "id": "u-1", "name": "Sales One"}
    assert out["items"][0]["before"] == {"notes": None} and out["items"][0]["after"] is None
    assert out["next_before"] == paging.encode(AT, ROW_ID)
    sql, args = conn.calls[-1]
    assert "(l.created_at, l.id) < ($6, $7::uuid)" in sql
    assert args[3] == datetime(2026, 10, 1, tzinfo=UTC) and args[4] == datetime(2026, 10, 15, tzinfo=UTC)
    empty = await audit.list_logs(ScriptedConn())
    assert empty == {"items": [], "next_before": None}


async def test_reference_reads() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.settings", [{"key": "deposit_pct", "value": "30", "description": "d", "updated_at": AT}])
        .on("FROM booking.countries", [{"iso2": "VN", "name": "Vietnam"}])
    )
    out = await reference.list_settings(conn)
    assert out["values"] == {"deposit_pct": 30} and out["items"][0]["value"] == 30
    assert await reference.list_countries(conn) == [{"iso2": "VN", "name": "Vietnam"}]


# ── notifications ────────────────────────────────────────────────────────────


async def test_notify_staff_writes_a_staff_row() -> None:
    conn = ScriptedConn().on("RETURNING id::text", ROW_ID)
    await notifications.notify_staff(
        conn, type="trip_case.assigned", title="t", message=None, entity_type=None, entity_id=None, recipient_ref="u-1"
    )
    assert conn.sql("VALUES ('staff'")[0][:2] == ("u-1", "trip_case.assigned")
    assert not hasattr(notifications, "notify_customer")  # AA-1046: no customer feed row is written any more


async def test_feed_filters_and_mark_read() -> None:
    item = {
        "id": ROW_ID,
        "type": "trip_case.assigned",
        "title": "t",
        "message": None,
        "entity_type": None,
        "entity_id": None,
        "data": '{"code": "TC-1"}',
        "created_at": AT,
        "read": False,
    }
    conn = ScriptedConn().on("SELECT count(*) FROM booking.notifications", 3).on("ORDER BY n.created_at DESC", [item])
    out = await notifications.list_feed(
        conn, SALES, unread=True, type="trip_case.assigned", q="tc", before=paging.encode(AT, ROW_ID), limit=1
    )
    assert out["unread_count"] == 3 and out["items"][0]["data"] == {"code": "TC-1"} and out["next_before"]
    sql = conn.sql("ORDER BY n.created_at DESC")[0]
    assert sql[0] == "u-1" and sql[1] == "trip_case.assigned" and sql[2] == "%tc%"
    read = await notifications.list_feed(ScriptedConn(), SALES, unread=False)
    assert read["items"] == [] and read["next_before"] is None

    class Tagged(ScriptedConn):
        async def execute(self, sql: str, *args: Any) -> str:
            self._answer(sql, args)
            return "INSERT 0 2" if "notification_reads" in sql else "OK"

    marking = Tagged().on("SELECT count(*) FROM booking.notifications", 1)
    out = await notifications.mark_read(marking, SALES, ids=[ROW_ID], all_=False)
    assert out == {"marked": 2, "unread_count": 1} and audit_args(marking)[0][3] == "notification.read"
    none = await notifications.mark_read(ScriptedConn(), SALES, ids=None, all_=True)
    assert none["marked"] == 0 and none["unread_count"] is None


# ── staff ────────────────────────────────────────────────────────────────────


async def test_staff_directory_detail_and_preferences() -> None:
    conn = ScriptedConn().on("WHERE active AND", [{"cis_user_id": "u-1", "name": "S", "role": "sales"}])
    assert await staff_admin.directory(conn, role="sales") == [{"cis_user_id": "u-1", "name": "S", "role": "sales"}]
    with pytest.raises(Invalid):
        await staff_admin.directory(conn, role="pilot")
    with pytest.raises(Forbidden):
        await staff_admin.staff_detail(ScriptedConn(), SALES, "a-1")
    with pytest.raises(NotFound):
        await staff_admin.staff_detail(ScriptedConn(), ADMIN, "ghost")
    profile = {
        "cis_user_id": "u-1",
        "email": None,
        "name": "S",
        "role": "sales",
        "active": True,
        "cis_role": "reviewer",
        "created_at": AT,
        "updated_at": AT,
        "last_seen_at": None,
    }
    assigned = [
        {"role": "sales", "code": "TC-1", "status": "building"},
        {"role": "sales", "code": "TC-2", "status": "completed"},
    ]
    conn = (
        ScriptedConn()
        .on("FROM booking.staff_users WHERE cis_user_id", profile)
        .on("FROM booking.staff_assignments a", assigned)
    )
    out = await staff_admin.staff_detail(conn, SALES, "u-1")
    assert out["counts"] == {"open_assignments": 1, "assignments": 2}
    prefs_conn = ScriptedConn().on("SELECT preferences::text", '{"notify": {"proposal.approved": false}}')
    prefs = await staff_admin.get_preferences(prefs_conn, SALES)
    assert prefs["notify"]["proposal.approved"] is False and prefs["notify"]["trip_case.assigned"] is True
    assert (await staff_admin.get_preferences(ScriptedConn(), SALES))["notify"]["proposal.approved"] is True
    unchanged = await staff_admin.set_preferences(prefs_conn, SALES, {"proposal.approved": False})
    assert unchanged == prefs and audit_args(prefs_conn) == []
    changed = await staff_admin.set_preferences(prefs_conn, SALES, {"trip_case.assigned": False})
    assert (
        changed["notify"]["trip_case.assigned"] is False and audit_args(prefs_conn)[0][3] == "staff.preferences_updated"
    )


# ── trip cases ───────────────────────────────────────────────────────────────


async def test_list_cases_w1_filters_and_shape() -> None:
    row = {
        **case_row(),
        "start_date": date(2027, 3, 1),
        "assignees": "[]",
        "countries": ["VN"],
        "legs": json.dumps([{"position": 1, "source_tour_id": "t-a", "day_from": 1, "day_to": 4}]),
        "inquiry_snapshot": json.dumps({"days": [{"day": 1, "source_tour_id": "t-a", "name": "Hanoi"}]}),
    }
    conn = ScriptedConn().on("SELECT count(*)", 1).on("ORDER BY tc.", [row])
    out = await service.list_cases(
        conn,
        q="ann",
        source="manual",
        created_from=date(2026, 10, 1),
        created_to=date(2026, 10, 14),
        sort="start_date",
    )
    item = out["items"][0]
    assert item["trip_title"] == "Hanoi" and item["duration_days"] == 4
    assert item["trip_end_date"] == date(2027, 3, 4) and item["countries"] == ["VN"]
    sql, args = conn.calls[-1]
    assert "ORDER BY tc.start_date ASC NULLS LAST" in sql and args[0] == "%ann%"
    # AA-1046: no customer join (booking.customers is dropped by bk-045), the search is the case code only
    assert "booking.customers" not in sql and "customer_id" not in sql and "tc.code ILIKE $1" in sql
    for bad in ({"source": "fax"}, {"sort": "price"}):
        with pytest.raises(Invalid):
            await service.list_cases(ScriptedConn(), **bad)


async def test_summary_counts() -> None:
    conn = (
        ScriptedConn()
        .on(
            "count(*) FILTER",
            {
                "open": 5,
                "new_inquiry": 2,
                "proposals_awaiting": 1,
                "mine": 3,
                "created_this_month": 4,
                "created_last_month": 6,
            },
        )
        .on("GROUP BY status", [{"status": "building", "n": 2}])
    )
    out = await service.summary(conn, SALES)
    assert out["mom_delta"] == {"created_this_month": 4, "created_last_month": 6, "delta": -2}
    assert out["by_status"]["building"] == 2 and out["by_status"]["paid"] == 0 and out["mine"] == 3


async def test_activity_shapes_status_and_audit_rows() -> None:
    rows = [
        {
            "id": ROW_ID,
            "kind": "status",
            "at": AT,
            "actor_ref": None,
            "actor_name": None,
            "action": "trip_case.status_changed",
            "entity_label": None,
            "summary": None,
            "before": '{"status": null}',
            "after": '{"status": "new_inquiry"}',
        },
        {
            "id": VERSION_ID,
            "kind": "status",
            "at": AT,
            "actor_ref": "u-1",
            "actor_name": "S",
            "action": "trip_case.status_changed",
            "entity_label": None,
            "summary": "call booked",
            "before": '{"status": "new_inquiry"}',
            "after": '{"status": "discovery"}',
        },
        {
            "id": CASE_ID,
            "kind": "audit",
            "at": AT,
            "actor_ref": "u-1",
            "actor_name": "S",
            "action": "trip_case.updated",
            "entity_label": "TC-1",
            "summary": "Updated notes",
            "before": None,
            "after": "{}",
        },
    ]
    conn = ScriptedConn().on("FROM booking.trip_cases tc", case_row()).on("UNION ALL", rows)
    out = await service.activity(conn, "TC-000001", before=paging.encode(AT, ROW_ID), limit=3)
    assert [i["summary"] for i in out["items"]] == [
        "opened -> new_inquiry",
        "new_inquiry -> discovery: call booked",
        "Updated notes",
    ]
    assert out["next_before"] == paging.encode(AT, CASE_ID)


async def test_update_case_pax_split_and_countries() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("UPDATE booking.trip_cases SET pax", {**case_row(), "pax": 3, "pax_adults": 2, "pax_children": 1})
        .on("FROM booking.countries", [{"iso2": "KH"}])
        .on("FROM booking.trip_case_countries", [{"code": "VN"}])
    )
    out = await service.update_case(conn, SALES, "TC-1", {"pax_adults": 2, "pax_children": 1, "countries": ["kh"]})
    assert out["changed"] == ["countries", "pax", "pax_adults", "pax_children"] and out["countries"] == ["VN"]
    only_countries = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.countries", [{"iso2": "KH"}])
        .on("FROM booking.trip_case_countries", [])
    )
    out = await service.update_case(only_countries, SALES, "TC-1", {"countries": ["KH"]})
    assert out["changed"] == ["countries"] and only_countries.sql("SET updated_at = now()")
    same = ScriptedConn().on("FROM booking.trip_cases tc", case_row()).on("FROM booking.trip_case_countries", [])
    assert (await service.update_case(same, SALES, "TC-1", {"countries": []}))["changed"] == []
    unknown = ScriptedConn().on("FROM booking.trip_cases tc", case_row()).on("FROM booking.trip_case_countries", [])
    with pytest.raises(Invalid):
        await service.update_case(unknown, SALES, "TC-1", {"countries": ["ZZ"]})
    with pytest.raises(Invalid):
        await service._set_countries(ScriptedConn(), CASE_ID, [f"A{chr(65 + i)}" for i in range(21)])


async def test_status_change_rules_for_confirmed_and_reopen() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row("proposal_sent"))
        .on("from_status = $1", [{"to_status": "building"}, {"to_status": "cancelled"}, {"to_status": "confirmed"}])
    )
    with pytest.raises(Conflict) as approve:
        await service.change_status(conn, SALES, "TC-1", "confirmed", None)
    assert approve.value.code == "approve_version_first"
    reopen = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row("cancelled"))
        .on("from_status = $1", [{"to_status": "new_inquiry"}])
        .on("role = 'sales' AND unassigned_at IS NULL", "u-7")
    )
    with pytest.raises(Invalid):
        await service.change_status(reopen, SALES, "TC-1", "new_inquiry", " ")
    out = await service.change_status(reopen, SALES, "TC-1", "new_inquiry", "came back")
    assert out["status"] == "new_inquiry"
    notified = reopen.sql("VALUES ('staff'")[0]
    assert notified[:2] == ("u-7", "trip_case.status_changed")
    assert service.reason_required("cancelled", "new_inquiry") and not service.reason_required("discovery", "building")


async def test_assign_notifies_the_new_holder_but_not_self() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.staff_users", {"cis_user_id": "u-2", "active": True})
    )
    await service.assign(conn, SALES, "TC-1", "u-2", "sales")
    assert conn.sql("VALUES ('staff'")[0][:2] == ("u-2", "trip_case.assigned")
    self_conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.staff_users", {"cis_user_id": "u-1", "active": True})
    )
    await service.assign(self_conn, SALES, "TC-1", "u-1", "sales")
    assert self_conn.sql("VALUES ('staff'") == []


def detail_conn(conn: ScriptedConn) -> ScriptedConn:
    return conn.on(
        "tc.inbox_event_id", {**case_row("new_inquiry"), "assignees": "[]", "countries": [], "inbox_event_id": None}
    )


# ── versions ─────────────────────────────────────────────────────────────────


async def test_get_update_delete_version() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.trip_case_versions v", version_row(fx_rate_locked="25000.5", fx_as_of=date(2026, 10, 14)))
        .on("SELECT count(*) FROM booking.trip_case_version_itinerary", 2)
    )
    got = await versions.get_version(conn, "TC-1", 1)
    assert got["fx_locked"] == {"rate": "25000.5", "as_of": date(2026, 10, 14)} and got["item_count"] == 2
    with pytest.raises(Invalid):
        await versions.update_version(conn, SALES, "TC-1", 1, {"total_price": 1})
    same = await versions.update_version(conn, SALES, "TC-1", 1, {"title": None})
    assert same["changed"] == []
    changed = await versions.update_version(conn, SALES, "TC-1", 1, {"title": "Option A"})
    assert changed["changed"] == ["title"]
    assert conn.sql("SET title = $2, updated_by = $3")[0] == (VERSION_ID, "Option A", "u-1")
    await versions.delete_version(conn, SALES, "TC-1", 1)
    assert conn.sql("SET deleted_at = now()") == [(VERSION_ID, "u-1")]
    sent = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.trip_case_versions v", version_row(is_sent_to_customer=True))
    )
    with pytest.raises(Conflict):
        await versions.delete_version(sent, SALES, "TC-1", 1)


def send_conn(status: str = "discovery", **version: Any) -> ScriptedConn:
    return (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row(status))
        .on("FROM booking.trip_case_versions v", version_row(**version))
        .on("SELECT count(*) FROM booking.trip_case_version_itinerary", 1)
        .on(
            "FROM booking.customers WHERE id",
            {"id": CUSTOMER_ID, "code": "CUS-1", "name": "Ann", "email": "ann@x.test"},
        )
        .on("FROM booking.settings", "10")
        .on("RETURNING id::text", ROW_ID)
    )


async def test_approve_refuses_b2b_cases() -> None:
    """AA-973 review: approving would re-confirm a partner booking: 409 before any write."""
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row("proposal_sent", channel="b2b"))
        .on("FROM booking.trip_case_versions v", version_row(is_sent_to_customer=True, badge="sent"))
    )
    with pytest.raises(Conflict) as err:
        await versions.approve_version(conn, SALES, "TC-1", 1, None)
    assert err.value.code == "b2b_case_managed_by_partner_api"
    assert conn.sql("UPDATE booking.") == []
    assert not hasattr(versions, "send_version")  # AA-1046: no send to a traveller customer any more


async def test_approve_rules() -> None:
    def approve_conn(status: str = "proposal_sent", items: int = 3, **version: Any) -> ScriptedConn:
        return (
            ScriptedConn()
            .on("FROM booking.trip_cases tc", case_row(status))
            .on("FROM booking.trip_case_versions v", version_row(**version))
            .on("SELECT count(*) FROM booking.trip_case_version_itinerary", items)
        )

    ok = approve_conn(is_sent_to_customer=True, badge="sent")
    out = await versions.approve_version(ok, SALES, "TC-1", 1, " by phone ")
    assert out["trip_case"]["status"] == "confirmed"
    assert ok.sql("SET is_approved = true")[0] == (VERSION_ID, "u-1", "by phone")
    assert ok.sql("UPDATE booking.trip_cases SET status")[0] == (CASE_ID, "confirmed")
    assert ok.sql("VALUES ('staff'")[0][1] == "proposal.approved"
    for conn, code in (
        (approve_conn(is_approved=True), "version_already_approved"),
        (approve_conn(), "version_not_sent"),
        (approve_conn(is_sent_to_customer=True, badge="superseded"), "version_superseded"),
        (approve_conn("building", is_sent_to_customer=True, badge="sent"), "case_not_awaiting_approval"),
    ):
        with pytest.raises(Conflict) as err:
            await versions.approve_version(conn, SALES, "TC-1", 1, None)
        assert err.value.code == code


async def test_approve_refuses_a_version_without_items() -> None:
    """AA-996 (Figma audit item 12): a sent version with no items (reached out of band) never confirms the case."""
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row("proposal_sent"))
        .on("FROM booking.trip_case_versions v", version_row(is_sent_to_customer=True, badge="sent"))
        .on("SELECT count(*) FROM booking.trip_case_version_itinerary", 0)
    )
    with pytest.raises(Invalid) as err:
        await versions.approve_version(conn, SALES, "TC-1", 1, "by phone")
    assert err.value.code == "version_empty"
    assert conn.sql("UPDATE booking.") == [] and conn.sql("INSERT INTO booking.") == []


def badge_rows(*snapshots: dict[int, str]):
    """Answers for the version badge read (``_version_badges``): one snapshot per call, before then after the write."""
    calls = iter(snapshots)

    def answer(*_: Any) -> list[dict[str, Any]]:
        snap = next(calls)
        return [
            {"id": f"00000000-0000-4000-8000-00000000000{n}", "version_number": n, "badge": b} for n, b in snap.items()
        ]

    return answer


async def test_approve_audits_newly_superseded_versions_only() -> None:
    """AA-910: approving V2 supersedes every other live version; V1 (already superseded by the send of V2) gets no
    second row, the unsent draft V3 gets one."""
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row("proposal_sent"))
        .on("FROM booking.trip_case_versions v", version_row(version_number=2, is_sent_to_customer=True, badge="sent"))
        .on(
            "FROM booking.trip_case_versions AS v",
            badge_rows({1: "superseded", 2: "sent", 3: "draft"}, {1: "superseded", 2: "approved", 3: "superseded"}),
        )
        .on("SELECT count(*) FROM booking.trip_case_version_itinerary", 2)
    )
    await versions.approve_version(conn, SALES, "TC-1", 2, None)
    rows = [r for r in audit_args(conn) if r[3] == "trip_case_version.superseded"]
    assert [(r[8], r[9]) for r in rows] == [("TC-000001 V3", "V3 superseded: V2 was approved")]
    assert json.loads(rows[0][6]) == {"badge": "draft"}


async def test_list_versions_filters_and_shape() -> None:
    row = {
        **version_row(),
        "updated_by": "u-1",
        "updated_by_name": "S",
        "trip_case_id": CASE_ID,
        "trip_case_code": "TC-1",
        "trip_case_status": "building",
        "trip_case_channel": "b2c",
        "start_date": None,
        "sales_id": "u-1",
        "legs": "[]",
        "inquiry_snapshot": None,
    }
    conn = ScriptedConn().on("SELECT count(*)", 1).on("ORDER BY v.", [row])
    out = await versions.list_versions(
        conn,
        q="ann",
        badge="draft",
        assignee="u-1",
        updated_from=date(2026, 10, 1),
        updated_to=date(2026, 10, 14),
        sort="version_number",
    )
    item = out["items"][0]
    assert "customer" not in item and item["updated_by"] == {"id": "u-1", "name": "S"}
    assert "booking.customers" not in conn.calls[-1][0]
    assert item["trip_title"] is None and item["leg_titles"] == []
    assert item["trip_case_channel"] == "b2c" and "tc.channel AS trip_case_channel" in conn.calls[-1][0]
    unassigned = ScriptedConn()
    await versions.list_versions(unassigned, assignee="none")
    assert "IS NULL" in unassigned.calls[0][0]
    for bad in ({"badge": "lost"}, {"sort": "price"}):
        with pytest.raises(Invalid):
            await versions.list_versions(ScriptedConn(), **bad)


# ── customers ────────────────────────────────────────────────────────────────


CUSTOMER = {
    "id": CUSTOMER_ID,
    "code": "CUS-000001",
    "name": "Ann",
    "email": "ann@x.test",
    "phone": None,
    "address": None,
    "verified": False,
    "is_blocked": False,
    "needs_merge_review": False,
    "source": "manual",
}
