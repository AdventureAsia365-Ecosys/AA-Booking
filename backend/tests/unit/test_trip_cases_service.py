"""Python branches of app.trip_cases.service around the SQL (real SQL: tests/integration/test_bk003_workflow.py)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import asyncpg
import pytest

from app.auth.staff import Staff
from app.errors import Conflict, Invalid, NotFound
from app.trip_cases import service
from tests.unit.fakes import ScriptedConn

pytestmark = pytest.mark.asyncio

STAFF = Staff("u-1", "s@example.test", "Sales One", "sales", True, "reviewer")
CASE_ID = "11111111-1111-4111-8111-111111111111"


def case_row(status: str = "new_inquiry", **extra: Any) -> dict[str, Any]:
    return {
        "id": CASE_ID,
        "code": "TC-000001",
        "status": status,
        "currency": None,
        "pax": 2,
        "pax_adults": None,
        "pax_children": None,
        "trip_style": None,
        "start_date": None,
        "notes": None,
        "inquiry_snapshot": None,
        **extra,
    }


def transitions(*allowed: str):
    return [{"to_status": s} for s in allowed]


async def test_case_filter_accepts_uuid_or_code() -> None:
    assert service._case_filter(CASE_ID) == ("tc.id = $1::uuid", CASE_ID)
    assert service._case_filter(" tc-000042 ") == ("tc.code = $1", "TC-000042")


async def test_resolve_case_not_found() -> None:
    with pytest.raises(NotFound):
        await service.resolve_case(ScriptedConn(), "TC-404404")


async def test_resolve_case_locks_on_request() -> None:
    conn = ScriptedConn().on("FROM booking.trip_cases tc", case_row())
    await service.resolve_case(conn, "TC-000001", lock=True)
    assert conn.calls[0][0].endswith("FOR UPDATE")


async def test_list_cases_builds_filters_and_pages() -> None:
    row = {**case_row(), "assignees": json.dumps([{"cis_user_id": "u-1", "role": "sales"}])}
    conn = ScriptedConn().on("SELECT count(*)", 1).on("ORDER BY tc.created_at DESC", [row])
    out = await service.list_cases(conn, statuses=["new_inquiry", "discovery"], assignee="u-1", limit=10, offset=20)
    assert out["total"] == 1 and out["limit"] == 10 and out["offset"] == 20
    assert out["items"][0]["assignees"] == [{"cis_user_id": "u-1", "role": "sales"}]
    sql, args = conn.calls[-1]
    assert "tc.status = ANY($1::text[])" in sql and "a.staff_user_id = $2" in sql
    assert args == (["new_inquiry", "discovery"], "u-1", 10, 20)


async def test_list_cases_unassigned_filter() -> None:
    conn = ScriptedConn().on("SELECT count(*)", 0)
    out = await service.list_cases(conn, assignee="none")
    assert out["items"] == []
    assert "NOT EXISTS (SELECT 1 FROM booking.staff_assignments" in conn.calls[0][0]


async def test_list_cases_rejects_unknown_status() -> None:
    with pytest.raises(Invalid):
        await service.list_cases(ScriptedConn(), statuses=["archived"])


async def test_get_case_assembles_detail() -> None:
    detail = {**case_row(), "assignees": "[]", "inquiry_snapshot": json.dumps({"days": []}), "inbox_event_id": None}
    conn = (
        ScriptedConn()
        .on("tc.inbox_event_id", detail)
        .on("FROM booking.trip_case_legs", [{"position": 1, "source_tour_id": "t", "day_from": 1, "day_to": 2}])
        .on("FROM booking.trip_case_versions v", [{"version_number": 1}])
        .on("FROM booking.trip_case_status_history", [{"to_status": "new_inquiry"}])
        .on("a.unassigned_at FROM booking.staff_assignments", [])
        .on("FROM booking.trip_case_status_transitions", transitions("cancelled", "discovery"))
    )
    case = await service.get_case(conn, "TC-000001")
    assert case["inquiry_snapshot"] == {"days": []}
    assert case["legs"][0]["source_tour_id"] == "t"
    assert case["versions"] == [{"version_number": 1, "fx_locked": None}]
    assert case["trip_title"] == "AA tour t" and case["duration_days"] == 2
    assert case["allowed_transitions"] == ["cancelled", "discovery"]


async def test_get_case_not_found() -> None:
    with pytest.raises(NotFound):
        await service.get_case(ScriptedConn(), CASE_ID)


async def test_update_case_writes_only_changed_fields_and_audits() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("UPDATE booking.trip_cases", {**case_row(pax=4, currency="USD")})
    )
    out = await service.update_case(conn, STAFF, "TC-000001", {"pax": 4, "currency": "USD", "notes": None})
    assert out["changed"] == ["currency", "pax"]
    update_sql, update_args = conn.calls[1]
    assert "pax = $2, currency = $3" in update_sql and update_args == (CASE_ID, 4, "USD")
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[3] == "trip_case.updated"
    assert json.loads(audit[6]) == {"currency": None, "pax": 2}
    assert json.loads(audit[7]) == {"currency": "USD", "pax": 4}


async def test_update_case_without_changes_writes_nothing() -> None:
    conn = ScriptedConn().on("FROM booking.trip_cases tc", case_row())
    out = await service.update_case(conn, STAFF, "TC-000001", {"pax": 2})
    assert out["changed"] == []
    assert len(conn.calls) == 1


async def test_update_case_rejects_other_fields_and_check_violations() -> None:
    with pytest.raises(Invalid):
        await service.update_case(ScriptedConn(), STAFF, "TC-000001", {"status": "paid"})
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("UPDATE booking.trip_cases", asyncpg.CheckViolationError("trip_cases_dates_chk"))
    )
    with pytest.raises(Invalid):
        await service.update_case(conn, STAFF, "TC-000001", {"start_date": date(2027, 1, 1)})


async def test_update_case_b2b_allows_notes_only() -> None:
    """AA-973 review: on a B2B case staff may annotate (notes, trip style) but not move pax / dates / currency,
    which are what the partner hold sold."""
    b2b = case_row("confirmed", channel="b2b", customer_id=None, pax=2)
    conn = ScriptedConn().on("FROM booking.trip_cases tc", b2b)
    with pytest.raises(Conflict) as err:
        await service.update_case(conn, STAFF, "TC-000001", {"pax": 3})
    assert err.value.code == "b2b_case_managed_by_partner_api"
    assert conn.sql("UPDATE booking.trip_cases") == []
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", b2b)
        .on("UPDATE booking.trip_cases", {**b2b, "notes": "agency rang"})
    )
    out = await service.update_case(conn, STAFF, "TC-000001", {"notes": "agency rang", "pax": 2})
    assert out["changed"] == ["notes"]


async def test_change_status_legal_edge_sets_actor_and_reason() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row("new_inquiry"))
        .on("from_status = $1", lambda s: transitions("cancelled", "discovery") if s == "new_inquiry" else [])
    )
    out = await service.change_status(conn, STAFF, "TC-000001", "discovery", "  first call booked ")
    assert out["status"] == "discovery" and out["reason"] == "first call booked"
    assert conn.sql("set_config")[0] == ("u-1", "first call booked")
    assert conn.sql("UPDATE booking.trip_cases SET status")[0] == (CASE_ID, "discovery")
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "trip_case.status_changed"


async def test_change_status_errors() -> None:
    with pytest.raises(Invalid):
        await service.change_status(ScriptedConn(), STAFF, "x", "archived", None)
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row("discovery"))
        .on("from_status = $1", transitions("building", "cancelled"))
    )
    with pytest.raises(Conflict) as same:
        await service.change_status(conn, STAFF, "x", "discovery", None)
    assert same.value.code == "status_unchanged"
    with pytest.raises(Conflict) as illegal:
        await service.change_status(conn, STAFF, "x", "paid", None)
    assert illegal.value.extra == {"allowed_transitions": ["building", "cancelled"]}
    with pytest.raises(Invalid) as no_reason:
        await service.change_status(conn, STAFF, "x", "cancelled", "   ")
    assert no_reason.value.code == "reason_required"
    conn.on("SET status", asyncpg.CheckViolationError("illegal"))
    with pytest.raises(Conflict):
        await service.change_status(conn, STAFF, "x", "building", None)


@pytest.mark.parametrize("to_status", ["cancelled", "new_inquiry", "discovery"])
async def test_change_status_refuses_b2b_cases(to_status: str) -> None:
    """AA-973 review: a B2B case (no customer, seats sold through a hold) is moved only by the partner API (AA-974
    cancel); a staff cancel would leave the seats sold, a reopen would lead to a send with no customer."""
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row("confirmed", channel="b2b", customer_id=None))
        .on("from_status = $1", transitions("cancelled", "new_inquiry", "discovery"))
    )
    with pytest.raises(Conflict) as err:
        await service.change_status(conn, STAFF, "TC-000001", to_status, "agency asked")
    assert err.value.code == "b2b_case_managed_by_partner_api"
    assert conn.sql("UPDATE booking.trip_cases") == [] and conn.sql("INSERT INTO booking.audit_logs") == []


async def test_assign_new_holder_closes_the_previous_one() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.staff_assignments", {"id": "a-1", "staff_user_id": "u-old"})
        .on("FROM booking.staff_users", {"cis_user_id": "u-2", "active": True})
    )
    out = await service.assign(conn, STAFF, "TC-000001", "u-2", "sales")
    assert out["changed"] is True and out["staff_user_id"] == "u-2"
    assert conn.sql("SET unassigned_at = now()") == [("a-1",)]
    assert conn.sql("INSERT INTO booking.staff_assignments")[0] == (CASE_ID, "u-2", "sales", "u-1")
    audit = conn.sql("INSERT INTO booking.audit_logs")[0]
    assert audit[3] == "trip_case.assigned"


async def test_assign_same_holder_is_a_noop() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.staff_assignments", {"id": "a-1", "staff_user_id": "u-2"})
        .on("FROM booking.staff_users", {"cis_user_id": "u-2", "active": True})
    )
    out = await service.assign(conn, STAFF, "TC-000001", "u-2", "sales")
    assert out["changed"] is False
    assert conn.sql("INSERT INTO booking.audit_logs") == []


async def test_unassign_closes_the_role() -> None:
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.staff_assignments", {"id": "a-1", "staff_user_id": "u-2"})
    )
    out = await service.assign(conn, STAFF, "TC-000001", None, "sales")
    assert out["staff_user_id"] is None
    assert conn.sql("INSERT INTO booking.staff_assignments") == []
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "trip_case.unassigned"


async def test_assign_errors() -> None:
    with pytest.raises(Invalid):
        await service.assign(ScriptedConn(), STAFF, "x", "u-2", "captain")
    conn = (
        ScriptedConn()
        .on("FROM booking.trip_cases tc", case_row())
        .on("FROM booking.staff_users", {"cis_user_id": "u-2", "active": False})
    )
    with pytest.raises(Invalid) as inactive:
        await service.assign(conn, STAFF, "x", "u-2", "sales")
    assert inactive.value.code == "staff_not_assignable"


# ── AA-996: countries from the legs ──────────────────────────────────────────

PLACE_A = "d0000000-0000-4000-8000-000000000002"
PLACE_B = "d0000000-0000-4000-8000-000000000001"


async def test_leg_countries_from_keys_and_route_days_once_each() -> None:
    legs = [
        {"source_system": "cis", "source_id": "grp-laos"},
        {"source_system": None, "source_id": None},  # a legacy leg without a catalog key
        {"source_system": "cis", "source_id": "grp-vn"},
    ]
    snapshot = {
        "itinerary": {
            "days": [
                {"day": 2, "destination_id": PLACE_B},
                {"day": 1, "destination_id": PLACE_A},
                {"day": 3, "destination_id": "not-a-uuid"},
                {"day": 4, "destination_id": PLACE_A},
            ]
        }
    }
    found = [{"iso2": "LA"}, {"iso2": "VN"}, {"iso2": "LA"}, {"iso2": None}]
    conn = ScriptedConn().on("FROM unnest($1::text[])", found)
    assert await service.leg_countries(conn, legs, snapshot) == ["LA", "VN"]
    (args,) = conn.sql("FROM unnest($1::text[])")
    assert args == (["grp-laos", "grp-vn"], [PLACE_A, PLACE_B])


async def test_leg_countries_skip_the_catalog_when_nothing_to_resolve_and_fail_soft() -> None:
    quiet = ScriptedConn()
    assert await service.leg_countries(quiet, [{"source_system": None, "source_id": None}], {"days": "x"}) == []
    assert quiet.calls == []
    broken = ScriptedConn().on("FROM unnest($1::text[])", asyncpg.UndefinedTableError("catalog.v_tour"))
    assert await service.leg_countries(broken, [{"source_system": "cis", "source_id": "k"}], None) == []
