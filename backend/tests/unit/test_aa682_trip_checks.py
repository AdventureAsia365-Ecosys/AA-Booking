"""AA-682 S2 (bk-027) without a database: the lifecycle plan (state machine over the travel dates, B2C / B2B gates,
date boundaries), the run mode (DRY RUN first), the payload ids, the daily schedule, the derived checks, the
permission matrix and the read-only caps (preview, E2E) on the new write routes. The flows on a real PostgreSQL are in
tests/integration/test_bk027_*.py."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.auth.staff import PERMISSIONS, PREVIEW_PERMISSIONS, Staff
from app.jobs import kinds
from app.jobs.worker import PermanentError
from app.ops import lifecycle as lc
from app.ops import trip_checks as tc
from app.routers.trip_checks import CheckChanges
from tests.staff_tokens import BOOKING_STAFF_PREVIEW_CLIENT, LocalCis
from tests.unit.fakes import ScriptedConn
from tests.unit.test_aa910_e2e_read_only import e2e_token
from tests.unit.test_aa911_preview_cap import auth, client

TODAY = date(2026, 11, 20)
CHECK = "00000000-0000-4000-8000-000000000682"


def _facts(**over: Any) -> lc.CaseFacts:
    base = {
        "id": "case-1",
        "code": "TC-000001",
        "status": "confirmed",
        "channel": "b2b",
        "start_date": TODAY + timedelta(days=14),
        "end_date": TODAY + timedelta(days=18),
        "supply_state": "booked",
        "open_checks": 2,
    }
    return lc.CaseFacts(**{**base, **over})


def _plan(**over: Any) -> lc.Plan:
    return lc.plan(_facts(**over), TODAY, 14)


# ── The plan: state machine over the travel dates ────────────────────────────


def test_pre_departure_exactly_at_start_minus_days() -> None:
    assert _plan().moves == ["pre_departure"] and _plan().blocked is None
    # One day earlier: nothing is due yet, nothing is blocked.
    later = _plan(start_date=TODAY + timedelta(days=15))
    assert later.moves == [] and later.blocked is None


def test_b2b_from_supplier_confirming_and_supply_none() -> None:
    assert _plan(status="supplier_confirming", supply_state="none").moves == ["pre_departure"]


def test_not_booked_blocks_and_open_checks_never_block() -> None:
    p = _plan(supply_state="confirming", open_checks=3)
    assert p.moves == [] and p.blocked == ("pre_departure", ("not_booked",))
    assert _plan(open_checks=9).moves == ["pre_departure"]


def test_b2c_needs_paid_until_then_blocked_not_paid() -> None:
    p = _plan(channel="b2c")
    assert p.moves == [] and p.blocked == ("pre_departure", ("not_paid",))
    both = _plan(channel="b2c", status="invoice_sent", supply_state="confirming")
    assert both.blocked == ("pre_departure", ("not_booked", "not_paid"))
    assert _plan(channel="b2c", status="paid").moves == ["pre_departure"]


def test_blocked_after_the_start_date_targets_on_trip() -> None:
    p = _plan(channel="b2c", start_date=TODAY, end_date=TODAY + timedelta(days=3))
    assert p.blocked == ("on_trip", ("not_paid",))


def test_chain_catches_up_on_missed_days() -> None:
    p = _plan(start_date=TODAY - timedelta(days=5), end_date=TODAY - timedelta(days=1))
    assert p.moves == ["pre_departure", "on_trip", "completed"] and p.blocked is None


def test_on_trip_on_the_start_date_and_completed_the_day_after_the_end() -> None:
    assert _plan(status="pre_departure", start_date=TODAY).moves == ["on_trip"]
    assert _plan(status="pre_departure", start_date=TODAY + timedelta(days=1)).moves == []
    assert _plan(status="on_trip", start_date=TODAY - timedelta(days=3), end_date=TODAY).moves == []
    ended = _plan(status="on_trip", start_date=TODAY - timedelta(days=3), end_date=TODAY - timedelta(days=1))
    assert ended.moves == ["completed"]
    # A one-day trip: start = end = yesterday.
    one_day = _plan(start_date=TODAY - timedelta(days=1), end_date=TODAY - timedelta(days=1))
    assert one_day.moves == ["pre_departure", "on_trip", "completed"]


def test_missing_dates() -> None:
    assert _plan(start_date=None, end_date=None).blocked == ("pre_departure", ("no_start_date",))
    assert _plan(status="pre_departure", start_date=None).blocked is None  # nothing a date could move
    on_trip = _plan(status="on_trip", start_date=TODAY - timedelta(days=2), end_date=None)
    assert on_trip.moves == [] and on_trip.blocked == ("completed", ("no_end_date",))
    assert _plan(status="on_trip", start_date=TODAY, end_date=None).blocked is None  # first day: not yet


@pytest.mark.parametrize("status", ["new_inquiry", "proposal_sent", "completed", "cancelled"])
def test_other_statuses_never_move(status: str) -> None:
    p = _plan(status=status, start_date=TODAY - timedelta(days=30), end_date=TODAY - timedelta(days=20))
    assert p.moves == [] and p.blocked is None


def test_gate_key_is_stable_per_target_and_reasons() -> None:
    assert lc.gate_key("c", "pre_departure", ("not_paid", "not_booked")) == lc.gate_key(
        "c", "pre_departure", ("not_booked", "not_paid")
    )
    assert lc.gate_key("c", "on_trip", ("not_paid",)) != lc.gate_key("c", "pre_departure", ("not_paid",))


# ── Mode, payload, schedule ──────────────────────────────────────────────────


def test_decide_mode_dry_run_first() -> None:
    assert lc.decide_mode({"trigger": "schedule"}, live_done=False)[0] == "dry_run"
    assert lc.decide_mode({"trigger": "schedule"}, live_done=True)[0] == "live"
    assert lc.decide_mode({"trigger": "manual"}, live_done=True)[0] == "dry_run"
    assert lc.decide_mode({"trigger": "manual", "dry_run": True}, live_done=False)[0] == "dry_run"
    assert lc.decide_mode({"trigger": "manual", "dry_run": False}, live_done=False)[0] == "live"
    assert lc.decide_mode({}, live_done=True)[0] == "dry_run"


def test_case_ids_are_validated_uuids() -> None:
    assert lc._case_ids({}) is None
    assert lc._case_ids({"case_ids": [CHECK, CHECK.upper()]}) == [CHECK]
    for bad in ([], ["x"], "x", [CHECK] * 0, [f"00000000-0000-4000-8000-{i:012d}" for i in range(51)]):
        with pytest.raises(PermanentError):
            lc._case_ids({"case_ids": bad})


def test_setting_values_fall_back_to_defaults() -> None:
    assert lc._int_setting("21", "pre_departure_days") == 21
    assert lc._int_setting(30, "pre_departure_days") == 30
    for bad in ("-1", "1000", "true", "x", None, '"14"'):
        assert lc._int_setting(bad, "pre_departure_days") == 14


class FakePool:
    def __init__(self, conflict: bool = False) -> None:
        self.inserts: list[tuple[Any, ...]] = []
        self.conflict = conflict

    async def fetchval(self, sql: str, *args: Any) -> str | None:
        assert "INSERT INTO booking.job" in sql
        self.inserts.append(args)
        return None if self.conflict else f"job-{len(self.inserts)}"


@pytest.mark.asyncio
async def test_daily_schedule_queues_both_kinds_once_per_utc_day_after_the_hour() -> None:
    clock = {"now": datetime(2026, 11, 20, 0, 59, tzinfo=UTC)}
    hook = lc.OpsDailySchedule(now=lambda: clock["now"])
    pool = FakePool()
    assert await hook(pool) == [] and pool.inserts == []  # before 01:00 UTC
    clock["now"] = datetime(2026, 11, 20, lc.SCHEDULE_UTC_HOUR, 0, tzinfo=UTC)
    assert await hook(pool) == ["job-1", "job-2"]
    assert [a[0] for a in pool.inserts] == list(lc.KINDS)
    assert [json.loads(a[1]) for a in pool.inserts] == [{"trigger": "schedule"}] * 2  # ids only: nothing else
    assert [a[2] for a in pool.inserts] == [f"{k}:2026-11-20" for k in lc.KINDS]
    assert await hook(pool) == [] and len(pool.inserts) == 2  # same day
    clock["now"] = datetime(2026, 11, 21, 23, 30, tzinfo=UTC)
    await hook(pool)
    assert len(pool.inserts) == 4


@pytest.mark.asyncio
async def test_a_second_worker_finds_the_days_runs_already_queued() -> None:
    pool = FakePool(conflict=True)
    assert await lc.OpsDailySchedule(now=lambda: datetime(2026, 11, 20, 5, tzinfo=UTC))(pool) == []


def test_kinds_have_expected_seconds_and_are_not_exempt() -> None:
    for kind in lc.KINDS:
        assert kinds.EXPECTED_SECONDS[kind] <= 300
        assert kind not in kinds.DEPLOY_GATE_EXEMPT


# ── Derived checks ───────────────────────────────────────────────────────────


def test_derived_checks() -> None:
    board = {"supply_state": "booked", "suppliers_confirmed": True, "payment_verified": None}
    suppliers, payment = tc.derived_checks(board)
    assert suppliers["status"] == "done" and suppliers["detail"] == "booked" and suppliers["available"]
    assert payment["status"] is None and payment["available"] is False  # invoices arrive with bk-028
    suppliers, _ = tc.derived_checks({**board, "supply_state": "confirming", "suppliers_confirmed": False})
    assert suppliers["status"] == "open"
    assert [d["available"] for d in tc.derived_checks(None)] == [False, False]


def test_audit_view_never_copies_personal_values() -> None:
    view = tc._audit_view({"emergency_contact_phone": "+84 90 1", "note": "Mr X", "status": "done", "note2": None})
    assert view == {"emergency_contact_phone": "(set)", "note": "(set)", "status": "done", "note2": None}
    assert tc._audit_view({"emergency_contact_name": None}) == {"emergency_contact_name": None}


# ── Permissions and read-only sessions ───────────────────────────────────────


def test_permission_matrix() -> None:
    assert PERMISSIONS["ops.read"] == PERMISSIONS["ops.write"] == {"admin", "sales", "contracting"}
    assert "ops.read" in PREVIEW_PERMISSIONS and "ops.write" not in PREVIEW_PERMISSIONS


WRITES = [
    ("POST", "/booking/trip-cases/TC-000001/checks", {"title": "Visa letter"}),
    ("PATCH", f"/booking/trip-checks/{CHECK}", {"status": "done"}),
]


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
def test_preview_and_e2e_sessions_never_write(method: str, path: str, body: Any) -> None:
    cis = LocalCis()
    preview = cis.workforce.token(["booking-admin"], client_id=BOOKING_STAFF_PREVIEW_CLIENT)
    for token, detail in ((preview, "preview_read_only"), (e2e_token(cis), "e2e_read_only")):
        conn = ScriptedConn()
        with client(cis, conn) as c:
            r = c.request(method, path, json=body, headers=auth(token))
        assert r.status_code == 403 and r.json()["detail"] == detail
        assert conn.calls == []


def test_patch_body_model() -> None:
    assert CheckChanges(status="done", emergency_contact_phone="+84 90 123 4567").status == "done"
    for body in ({"status": "closed"}, {"emergency_contact_phone": "call me"}, {"code": "custom"}, {"title": ""}):
        with pytest.raises(ValidationError):
            CheckChanges(**body)
    assert CheckChanges(due_date=None).model_dump(exclude_unset=True) == {"due_date": None}  # explicit null clears


# ── AA-996: tab counts and per-trip progress on /trip-checks ──────────────────


def _list_row(**extra: Any) -> dict[str, Any]:
    return {
        "id": CHECK,
        "trip_case_id": "00000000-0000-4000-8000-0000000000c1",
        "trip_case_code": "TC-000042",
        "channel": "b2c",
        "trip_case_status": "confirmed",
        "start_date": TODAY,
        "end_date": TODAY + timedelta(days=9),
        "code": "insurance",
        "title": "Insurance",
        "status": "open",
        "due_date": TODAY,
        "assignee_ref": None,
        "assignee_name": None,
        "done_by": None,
        "done_by_name": None,
        "done_at": None,
        "note": None,
        "emergency_contact_name": None,
        "emergency_contact_phone": None,
        "cleared_at": None,
        "created_by": "system",
        "created_at": datetime(2026, 11, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 1, tzinfo=UTC),
        "customer_name": "Ann Lead",
        "case_checks_total": 4,
        "case_checks_done": 2,
        "case_checks_not_applicable": 1,
        "sales_name": "Sam Sales",
        **extra,
    }


@pytest.mark.asyncio
async def test_list_counts_every_status_and_rows_carry_trip_progress() -> None:
    sales = Staff("u-7", "s@example.test", "Sam", "sales", True, "reviewer")
    counts = {"open": 5, "overdue": 2, "done": 7, "not_applicable": 1, "all": 13}
    conn = (
        ScriptedConn()
        .on("SELECT count(*) FROM booking.trip_case_checks k", 5)
        .on("AS case_checks_total", [_list_row()])
        .on("count(*) FILTER (WHERE k.status = 'open') AS open", counts)
    )
    out = await tc.list_checks(conn, sales, status="done", due_before=TODAY, assignee="me", q="TC-42", limit=10)
    assert out["counts"] == counts and out["total"] == 5
    (row,) = out["items"]
    assert row["trip_case"]["checks"] == {"total": 4, "done": 2, "not_applicable": 1}
    assert row["trip_case"]["sales_name"] == "Sam Sales" and row["trip_case"]["customer_name"] == "Ann Lead"
    assert not {"case_checks_total", "case_checks_done", "sales_name", "customer_name"} & set(row)
    # The counts ignore the status and due filters (the tabs choose between them) but keep owner and search.
    (count_args,) = conn.sql("AS not_applicable")
    assert count_args == (list(tc.BOARD_STATUSES), "u-7", None, None, "%TC-42%")
    empty = await tc.list_checks(ScriptedConn(), sales)
    assert empty["counts"] == dict.fromkeys(tc.COUNT_KEYS, 0)
