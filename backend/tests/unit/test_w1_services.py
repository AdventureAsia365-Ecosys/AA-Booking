"""Python branches of the Wave 1 services around the SQL (real SQL: tests/integration/test_bk005_w1.py)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from app import audit, paging, reference
from app.auth import admin as staff_admin
from app.auth.staff import Staff
from app.customers import service as customers
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
        "customer_id": CUSTOMER_ID,
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


async def test_notify_helpers_write_rows() -> None:
    conn = ScriptedConn().on("RETURNING id::text", ROW_ID)
    await notifications.notify_staff(
        conn, type="trip_case.assigned", title="t", message=None, entity_type=None, entity_id=None, recipient_ref="u-1"
    )
    assert conn.sql("VALUES ('staff'")[0][:2] == ("u-1", "trip_case.assigned")
    nid = await notifications.notify_customer(
        conn,
        customer_id=CUSTOMER_ID,
        type="proposal.sent",
        title="t",
        message="m",
        entity_type="trip_case_version",
        entity_id=VERSION_ID,
        data={"valid_until": date(2026, 10, 28)},
    )
    assert nid == ROW_ID and json.loads(conn.sql("VALUES ('customer'")[0][-1]) == {"valid_until": "2026-10-28"}


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
        customer_id=CUSTOMER_ID,
        created_from=date(2026, 10, 1),
        created_to=date(2026, 10, 14),
        sort="start_date",
    )
    item = out["items"][0]
    assert item["trip_title"] == "Hanoi" and item["duration_days"] == 4
    assert item["trip_end_date"] == date(2027, 3, 4) and item["countries"] == ["VN"]
    sql, args = conn.calls[-1]
    assert "ORDER BY tc.start_date ASC NULLS LAST" in sql and args[0] == "%ann%"
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


async def test_create_case_existing_customer_and_new_lead() -> None:
    conn = detail_conn(
        ScriptedConn()
        .on("FROM booking.customers WHERE id = $1::uuid", {"id": CUSTOMER_ID, "is_blocked": False})
        .on("INSERT INTO booking.trip_cases", {"id": CASE_ID, "code": "TC-000001", "status": "new_inquiry"})
        .on("FROM booking.trip_cases tc", case_row("new_inquiry"))
        .on("FROM booking.staff_users", {"cis_user_id": "u-1", "active": True})
    )
    out = await service.create_case(conn, SALES, {"customer_id": CUSTOMER_ID, "pax_adults": 2, "currency": None})
    assert out["code"] == "TC-000001"
    insert = conn.sql("INSERT INTO booking.trip_cases")[0]
    assert insert[:2] == (CUSTOMER_ID, "new_inquiry") and insert[3:6] == (2, 2, None) and insert[6] == "USD"
    assert conn.sql("INSERT INTO booking.staff_assignments")[0][1] == "u-1"  # sales creator owns it
    lead = detail_conn(
        ScriptedConn()
        .on("INSERT INTO booking.customers", {"id": CUSTOMER_ID, "code": "CUS-000001"})
        .on("INSERT INTO booking.trip_cases", {"id": CASE_ID, "code": "TC-000001", "status": "discovery"})
    )
    await service.create_case(
        lead, ADMIN, {"new_customer": {"name": "Bo", "phone": "+1"}, "initial_status": "discovery", "countries": []}
    )
    assert lead.sql("INSERT INTO booking.staff_assignments") == []  # admin: no default owner


async def test_create_case_errors() -> None:
    with pytest.raises(Invalid):
        await service.create_case(ScriptedConn(), SALES, {"customer_id": CUSTOMER_ID, "initial_status": "paid"})
    with pytest.raises(Invalid):
        await service.create_case(ScriptedConn(), SALES, {})
    blocked = ScriptedConn().on("FROM booking.customers WHERE id", {"id": CUSTOMER_ID, "is_blocked": True})
    with pytest.raises(Conflict):
        await service.create_case(blocked, SALES, {"customer_id": CUSTOMER_ID})
    check = (
        ScriptedConn()
        .on("FROM booking.customers WHERE id", {"id": CUSTOMER_ID, "is_blocked": False})
        .on("INSERT INTO booking.trip_cases", asyncpg.CheckViolationError("trip_cases_pax_split_chk"))
    )
    with pytest.raises(Invalid):
        await service.create_case(check, SALES, {"customer_id": CUSTOMER_ID, "pax": 3, "pax_adults": 1})


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


async def test_send_walks_the_chain_and_notifies_the_customer() -> None:
    today = date(2026, 10, 14)
    conn = send_conn()
    out = await versions.send_version(
        conn, SALES, "TC-1", 1, to="ANN@x.test", subject="Trip", message="Hi", valid_until=None, today=today
    )
    assert out["notification_id"] == ROW_ID and out["email_sent"] is False
    assert conn.sql("SET is_sent_to_customer = true")[0] == (VERSION_ID, "u-1", today + timedelta(days=10))
    assert [a[1] for a in conn.sql("UPDATE booking.trip_cases SET status")] == ["building", "proposal_sent"]
    assert audit_args(conn)[0][3] == "trip_case_version.sent"
    resend = send_conn("proposal_sent")
    await versions.send_version(
        resend, SALES, "TC-1", 1, to=None, subject="s", message="m", valid_until=today + timedelta(days=7), today=today
    )
    assert resend.sql("UPDATE booking.trip_cases SET status") == []


@pytest.mark.parametrize(
    ("conn_kwargs", "kwargs", "error", "code"),
    [
        ({"status": "new_inquiry"}, {}, Conflict, "case_not_sendable"),
        ({"total_price": None}, {}, Invalid, "price_missing"),
        ({}, {"to": "x@y.test"}, Invalid, "recipient_mismatch"),
        ({}, {"valid_until": date(2026, 10, 15)}, Invalid, "invalid_valid_until"),
        ({}, {"valid_until": date(2026, 12, 31)}, Invalid, "invalid_valid_until"),
        ({"is_approved": True}, {}, Conflict, "version_locked"),
    ],
)
async def test_send_errors(conn_kwargs: dict[str, Any], kwargs: dict[str, Any], error: type, code: str) -> None:
    status = conn_kwargs.pop("status", "building")
    conn = send_conn(status, **conn_kwargs)
    args = {"to": None, "subject": "s", "message": "m", "valid_until": None, **kwargs}
    with pytest.raises(error) as err:
        await versions.send_version(conn, SALES, "TC-1", 1, today=date(2026, 10, 14), **args)
    assert err.value.code == code


async def test_send_needs_items_and_validity_setting_fallbacks() -> None:
    empty = send_conn().on("SELECT count(*) FROM booking.trip_case_version_itinerary", 0)
    with pytest.raises(Invalid) as err:
        await versions.send_version(empty, SALES, "TC-1", 1, to=None, subject="s", message="m", valid_until=None)
    assert err.value.code == "version_empty"
    assert await versions._validity_days(ScriptedConn()) == versions.DEFAULT_VALIDITY_DAYS
    assert await versions._validity_days(ScriptedConn().on("FROM booking.settings", '"x"')) == 14
    assert await versions._validity_days(ScriptedConn().on("FROM booking.settings", "90")) == 30


async def test_approve_rules() -> None:
    def approve_conn(status: str = "proposal_sent", **version: Any) -> ScriptedConn:
        return (
            ScriptedConn()
            .on("FROM booking.trip_cases tc", case_row(status))
            .on("FROM booking.trip_case_versions v", version_row(**version))
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


async def test_list_versions_filters_and_shape() -> None:
    row = {
        **version_row(),
        "updated_by": "u-1",
        "updated_by_name": "S",
        "trip_case_id": CASE_ID,
        "trip_case_code": "TC-1",
        "trip_case_status": "building",
        "start_date": None,
        "customer_id": CUSTOMER_ID,
        "customer_name": "Ann",
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
    assert item["customer"] == {"id": CUSTOMER_ID, "name": "Ann"} and item["updated_by"] == {"id": "u-1", "name": "S"}
    assert item["trip_title"] is None and item["leg_titles"] == []
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


async def test_list_customers_filters() -> None:
    conn = ScriptedConn().on("SELECT count(*)", 1).on("ORDER BY", [{"id": CUSTOMER_ID}])
    out = await customers.list_customers(
        conn, q="ann", status="lead", merge_review=True, source="manual", sort="name", limit=5
    )
    assert out["total"] == 1
    sql, args = conn.calls[-1]
    assert "s.status = $1" in sql and "ORDER BY lower(s.name) ASC" in sql and args[:3] == ("lead", True, "manual")
    blocked = ScriptedConn()
    await customers.list_customers(blocked, status="blocked")
    assert blocked.calls[0][1] == (True,)
    for bad in ({"status": "vip"}, {"source": "fax"}, {"sort": "age"}):
        with pytest.raises(Invalid):
            await customers.list_customers(ScriptedConn(), **bad)


async def test_get_customer_detail_assembles_parts() -> None:
    summary = {**CUSTOMER, "merge_candidate_id": ROW_ID, "status": "lead"}
    case = {
        **case_row("new_inquiry"),
        "source_system": "tripplanner",
        "created_at": AT,
        "assignees": "[]",
        "countries": [],
        "legs": "[]",
        "inquiry_snapshot": None,
    }
    conn = (
        ScriptedConn()
        .on("FROM booking.v_customer_summary s WHERE", summary)
        .on("deleted_at IS NOT NULL AS archived", {"id": ROW_ID, "code": "CUS-2", "name": "Ann", "verified": True})
        .on("SELECT count(*)", 1)
        .on("ORDER BY tc.", [case])
        .on(
            "LIMIT 20",
            [
                {
                    "id": ROW_ID,
                    "at": AT,
                    "actor_ref": "u-1",
                    "actor_name": "S",
                    "action": "customer.created",
                    "entity_type": "customer",
                    "entity_label": "CUS-1",
                    "summary": "x",
                }
            ],
        )
    )
    out = await customers.get_customer(conn, "cus-000001")
    assert out["merge_candidate"]["code"] == "CUS-2" and out["inquiries"][0]["code"] == "TC-000001"
    assert out["recent_activity"][0]["actor"] == {"id": "u-1", "name": "S"}
    with pytest.raises(NotFound):
        await customers.get_customer(ScriptedConn(), CUSTOMER_ID)
    with pytest.raises(NotFound):
        await customers.resolve_customer(ScriptedConn(), "CUS-404")


async def test_create_lead_rules() -> None:
    for data, code in (({"name": " ", "email": "a@b.test"}, "name_required"), ({"name": "A"}, "contact_required")):
        with pytest.raises(Invalid) as err:
            await customers.create_lead(ScriptedConn(), SALES, data)
        assert err.value.code == code
    conn = (
        ScriptedConn()
        .on("ORDER BY verified DESC", ROW_ID)
        .on("INSERT INTO booking.customers", {"id": CUSTOMER_ID, "code": "CUS-000002"})
    )
    out = await customers.create_lead(conn, SALES, {"name": "Ann", "email": " ANN@X.test ", "contact_consent": True})
    assert out == {"id": CUSTOMER_ID, "code": "CUS-000002", "needs_merge_review": True}
    insert = conn.sql("INSERT INTO booking.customers")[0]
    assert insert[:2] == ("Ann", "ann@x.test") and insert[4:7] == (True, ROW_ID, True)


async def test_update_customer_rules() -> None:
    def conn_for(**row: Any) -> ScriptedConn:
        return ScriptedConn().on("FROM booking.customers WHERE", {**CUSTOMER, **row})

    with pytest.raises(Invalid):
        await customers.update_customer(conn_for(), SALES, CUSTOMER_ID, {"source": "otp"})
    with pytest.raises(Forbidden):
        await customers.update_customer(conn_for(), SALES, CUSTOMER_ID, {"is_blocked": True})
    with pytest.raises(Invalid):
        await customers.update_customer(conn_for(), SALES, CUSTOMER_ID, {"name": "  "})
    with pytest.raises(Conflict) as locked:
        await customers.update_customer(conn_for(verified=True), SALES, CUSTOMER_ID, {"email": "new@x.test"})
    assert locked.value.code == "email_locked"
    with pytest.raises(Invalid):
        await customers.update_customer(conn_for(), SALES, CUSTOMER_ID, {"email": None})
    assert (await customers.update_customer(conn_for(), SALES, CUSTOMER_ID, {"name": "Ann"}))["changed"] == []
    taken = conn_for().on("UPDATE booking.customers", asyncpg.UniqueViolationError("dup"))
    with pytest.raises(Conflict):
        await customers.update_customer(taken, SALES, CUSTOMER_ID, {"phone": "+84 1"})
    block = conn_for()
    out = await customers.update_customer(block, ADMIN, CUSTOMER_ID, {"is_blocked": True})
    assert out["changed"] == ["is_blocked"] and audit_args(block)[0][3] == "customer.blocked"
    edit = conn_for()
    await customers.update_customer(edit, SALES, CUSTOMER_ID, {"address": " Hanoi ", "email": "NEW@x.test"})
    assert edit.sql("UPDATE booking.customers SET")[0] == (CUSTOMER_ID, "Hanoi", "new@x.test")
    assert audit_args(edit)[0][3] == "customer.updated"


async def test_archive_customer() -> None:
    cases = [
        {"id": CASE_ID, "code": "TC-1", "status": "new_inquiry"},
        {"id": ROW_ID, "code": "TC-2", "status": "cancelled"},
    ]
    conn = ScriptedConn().on("FROM booking.customers WHERE", CUSTOMER).on("ORDER BY created_at FOR UPDATE", cases)
    await customers.archive_customer(conn, ADMIN, CUSTOMER_ID)
    assert conn.sql("UPDATE booking.trip_cases SET status") == [(CASE_ID, "cancelled")]
    assert conn.sql("SET deleted_at = now()") == [(CUSTOMER_ID,)]
    assert json.loads(audit_args(conn)[0][7])["cancelled_cases"] == ["TC-1"]
    busy = (
        ScriptedConn()
        .on("FROM booking.customers WHERE", CUSTOMER)
        .on("ORDER BY created_at FOR UPDATE", [{"id": CASE_ID, "code": "TC-3", "status": "building"}])
    )
    with pytest.raises(Conflict) as err:
        await customers.archive_customer(busy, ADMIN, CUSTOMER_ID)
    assert err.value.extra == {"trip_cases": ["TC-3"]}
