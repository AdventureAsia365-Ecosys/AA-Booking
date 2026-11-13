"""AA-1073 (bk-046) with a scripted connection: the sales cutoff and the allotment seat accounting of availability, the
contract 1.6 departure fields, the recurring generator (dates, dry run, insert), the departure rules (minimum,
capacity against allotments, cancel ends allotments), the allotment life (propose, approve with maker-checker and the
free-seat check, end, release), the departure watch (guarantee, at risk, dry run), the daily schedule, the manual run
and the staff routes. The SQL itself runs against Postgres in tests/integration/test_aa1073_departures.py and
test_bk046_schema.py. Names are invented."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.staff import Staff, current_staff, get_pool
from app.b2b import allotments, departure_watch, departures
from app.b2b.departures import AVAILABLE, ON_REQUEST, SOLD_OUT, Allotment, SeatAsk, evaluate
from app.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from app.jobs.queue import Job
from app.main import create_app
from app.settings import Settings
from tests.unit.fakes import POLICY_FRAGMENT, ScriptedConn, policy_rows

PLAN = "8e000000-0000-4000-8000-000000001073"
DEP = "9f000000-0000-4000-8000-000000001073"
AGENCY = "7a000000-0000-4000-8000-000000000001"
OTHER = "7a000000-0000-4000-8000-000000000002"
ALLOT = "6b000000-0000-4000-8000-000000000001"
OLD_ALLOT = "6b000000-0000-4000-8000-000000000002"
NOW = datetime(2027, 2, 1, 9, 0, tzinfo=UTC)
TODAY = NOW.date()
START = date(2027, 3, 1)
ADMIN = Staff("a-1", None, "Admin", "admin", True, "admin")
CONTRACTING = Staff("c-1", None, "Contracting", "contracting", True, "reviewer")
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CREATOR = Staff("w-1", None, "Writer", "content_creator", True, "reviewer")

ALLOTMENTS_FRAGMENT = "FROM booking.departure_allotments a WHERE a.departure_id = ANY"
USAGE_FRAGMENT = "FROM booking.b2b_hold_lines l"
LOCK_FRAGMENT = "ORDER BY d.id FOR UPDATE"
BY_IDS_FRAGMENT = "FROM booking.departures d WHERE d.id = ANY"
ROW_FRAGMENT = "JOIN booking.b2b_partners p ON p.id = a.partner_id"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def dep_row(**kw: Any) -> dict[str, Any]:
    row = {
        "id": DEP,
        "trip_plan_id": PLAN,
        "start_date": START,
        "capacity": 10,
        "status": "scheduled",
        "auto_confirm": True,
        "min_to_operate": 0,
        "sales_cutoff_days": 0,
        "decision_days_before": 21,
        "at_risk_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return row | kw


def allot_row(**kw: Any) -> dict[str, Any]:
    row = {
        "id": ALLOT,
        "departure_id": DEP,
        "partner_id": AGENCY,
        "agency_name": "Lanternfield Travel",
        "partner_status": "active",
        "seats": 4,
        "release_days_before": 14,
        "replaces_id": None,
        "created_by": "c-1",
        "created_by_name": "Contracting",
        "created_at": NOW,
        "approved_by": None,
        "approved_by_name": None,
        "approved_at": None,
        "released_at": None,
        "ended_at": None,
        "ended_by": None,
        "ended_by_name": None,
        "end_reason": None,
        "start_date": START,
    }
    return row | kw


def live(*rows: tuple[str, int, int]) -> list[dict[str, Any]]:
    """ALLOTMENTS_SQL rows: (partner, seats, used)."""
    return [
        {"departure_id": DEP, "partner_id": p, "seats": s, "used": u, "release_days_before": 14} for p, s, u in rows
    ]


def ask(seats: int = 2, on: date = START) -> SeatAsk:
    return SeatAsk(line_ref="L1", component_id=f"tp:{PLAN}", service_date=on, departure_id=DEP, seats=seats)


# ── Availability: cutoff and allotments ──────────────────────────────────────


def test_sales_cutoff_closes_sales_on_its_day() -> None:
    row = dep_row(sales_cutoff_days=7)
    assert evaluate([ask()], {DEP: row}, {}, today=START - timedelta(days=8)) == [(AVAILABLE, DEP, None)]
    [(state, _, why)] = evaluate([ask()], {DEP: row}, {}, today=START - timedelta(days=7))
    assert state == SOLD_OUT and why == "sales closed on 2027-02-22"
    # Cutoff 0 keeps the old rule: open until the day before the start.
    assert evaluate([ask()], {DEP: dep_row()}, {}, today=START - timedelta(days=1))[0][0] == AVAILABLE
    assert evaluate([ask()], {DEP: dep_row()}, {}, today=START)[0][2] == "the departure has started"


def test_other_agencies_unused_allotments_are_not_offered() -> None:
    row = dep_row(capacity=10, auto_confirm=False)
    used = {DEP: (2, 1)}  # 7 free before allotments
    assert evaluate([ask(4)], {DEP: row}, used, today=TODAY, reserved={DEP: 3})[0][0] == ON_REQUEST
    [(state, _, why)] = evaluate([ask(5)], {DEP: row}, used, today=TODAY, reserved={DEP: 3})
    assert state == SOLD_OUT and why == "4 seats left, 5 asked"


def test_reserved_for_others_and_unused() -> None:
    rows = {DEP: [Allotment(AGENCY, 4, 1), Allotment(OTHER, 3, 5)]}
    assert rows[DEP][0].unused == 3 and rows[DEP][1].unused == 0
    assert departures.reserved_for_others(rows, AGENCY) == {DEP: 0}
    assert departures.reserved_for_others(rows, OTHER) == {DEP: 3}
    assert departures.reserved_for_others(rows, None) == {DEP: 3}


def test_availability_reads_allotments_for_the_caller() -> None:
    conn = ScriptedConn()
    conn.on(BY_IDS_FRAGMENT, [dep_row(capacity=6)])
    conn.on(USAGE_FRAGMENT, [{"departure_id": DEP, "held": 0, "sold": 0}])
    conn.on(ALLOTMENTS_FRAGMENT, live((OTHER, 4, 0)))
    assert run(departures.availability(conn, [ask(3)], today=TODAY, partner_id=AGENCY))[0][0] == SOLD_OUT
    assert run(departures.availability(conn, [ask(3)], today=TODAY, partner_id=OTHER))[0][0] == AVAILABLE
    [args] = conn.sql(ALLOTMENTS_FRAGMENT)[:1]
    assert args == ([DEP], None)
    assert run(departures.live_allotments(conn, [])) == {}


def test_lock_departures_counts_allotments_under_the_lock() -> None:
    conn = ScriptedConn()
    conn.on(LOCK_FRAGMENT, [dep_row(capacity=6)])
    conn.on(ALLOTMENTS_FRAGMENT, live((OTHER, 4, 1)))
    result, locked = run(departures.lock_departures(conn, [ask(3)], today=TODAY, partner_id=AGENCY))
    assert result == [(AVAILABLE, DEP, None)] and set(locked) == {DEP}
    result, _ = run(departures.lock_departures(conn, [ask(4)], today=TODAY, partner_id=AGENCY))
    assert result[0][0] == SOLD_OUT


def test_contract_departure_fields() -> None:
    row = dep_row(min_to_operate=6, sales_cutoff_days=10, source_system="cis", source_id="LF-9D")
    own = Allotment(AGENCY, 4, 1, 14)
    out = departures.departure_out(row, 1, 2, reserved=3, own=own)
    assert out["available"] == 4  # 10 - 1 held - 2 sold - 3 kept for others
    assert out["min_to_operate"] == 6 and out["seats_to_guarantee"] == 4
    assert out["last_sale_date"] == "2027-02-18"
    assert out["allotment"] == {"seats": 4, "used": 1, "available": 3, "release_date": "2027-02-15"}
    assert departures.departure_out(dep_row(status="guaranteed", min_to_operate=6, source_system="cis",
                                            source_id="x"), 0, 0)["seats_to_guarantee"] == 0  # fmt: skip
    plain = departures.departure_out(dep_row(source_system="cis", source_id="x"), 0, 0)
    assert plain["seats_to_guarantee"] is None and plain["allotment"] is None


def test_list_for_tour_offers_the_callers_own_allotment() -> None:
    conn = ScriptedConn()
    conn.on("JOIN booking.trip_plans p ON p.id = d.trip_plan_id", [dep_row(source_system="cis", source_id="LF-9D")])
    conn.on(USAGE_FRAGMENT, [{"departure_id": DEP, "held": 0, "sold": 2}])
    conn.on(ALLOTMENTS_FRAGMENT, live((AGENCY, 4, 1), (OTHER, 3, 0)))
    mine = run(departures.list_for_tour(conn, source_system="cis", source_id="LF-9D", start=None, end=None,
                                        today=TODAY, partner_id=AGENCY))["items"][0]  # fmt: skip
    theirs = run(departures.list_for_tour(conn, source_system="cis", source_id="LF-9D", start=None, end=None,
                                          today=TODAY, partner_id=OTHER))["items"][0]  # fmt: skip
    assert mine["available"] == 5 and mine["allotment"]["available"] == 3  # 10 - 2 sold - 3 of the other
    assert theirs["available"] == 5 and theirs["allotment"]["available"] == 3  # 10 - 2 sold - 3 of the first


def test_staff_out_at_risk_and_allotments() -> None:
    row = dep_row(min_to_operate=6, at_risk_at=NOW)
    out = departures.staff_out(row, 1, 2, [Allotment(AGENCY, 4, 1)])
    assert out["at_risk"] is True and out["allotted"] == 4 and out["allotment_unused"] == 3
    assert out["available"] == 4 and out["decision_date"] == date(2027, 2, 8)
    assert out["sales_close_date"] == START
    assert departures.staff_out(row, 0, 6)["at_risk"] is False  # minimum reached
    assert (
        departures.staff_out(dep_row(min_to_operate=6, at_risk_at=NOW, status="guaranteed"), 0, 0)["at_risk"] is False
    )


# ── Generator ────────────────────────────────────────────────────────────────


def test_build_dates_weekly_and_monthly() -> None:
    assert departures.build_dates(date(2027, 3, 1), date(2027, 3, 14), weekdays=[1, 5]) == [
        date(2027, 3, 1),
        date(2027, 3, 5),
        date(2027, 3, 8),
        date(2027, 3, 12),
    ]
    # Day 31: months without it are skipped, never moved.
    assert departures.build_dates(date(2027, 1, 15), date(2027, 5, 31), month_day=31) == [
        date(2027, 1, 31),
        date(2027, 3, 31),
        date(2027, 5, 31),
    ]
    assert departures.build_dates(date(2027, 12, 2), date(2028, 1, 31), month_day=1) == [date(2028, 1, 1)]


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"weekdays": [1], "month_day": 1}, "invalid_pattern"),
        ({}, "invalid_pattern"),
        ({"weekdays": []}, "invalid_pattern"),
        ({"weekdays": [8]}, "invalid_pattern"),
        ({"month_day": 32}, "invalid_pattern"),
    ],
)
def test_build_dates_refuses_bad_patterns(kw: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as exc:
        departures.build_dates(date(2027, 3, 1), date(2027, 3, 31), **kw)
    assert exc.value.code == code


def test_build_dates_window_and_count_limits() -> None:
    with pytest.raises(Invalid):
        departures.build_dates(date(2027, 3, 2), date(2027, 3, 1), weekdays=[1])
    with pytest.raises(Invalid):
        departures.build_dates(date(2027, 1, 1), date(2028, 3, 1), weekdays=[1])
    assert len(departures.build_dates(date(2027, 1, 1), date(2028, 2, 4), weekdays=list(range(1, 8)))) == 400
    with pytest.raises(Invalid) as exc:
        departures.build_dates(date(2027, 1, 1), date(2028, 2, 5), weekdays=list(range(1, 8)))
    assert exc.value.code == "too_many_dates"


def plan_conn(plan: str | None = PLAN) -> ScriptedConn:
    return ScriptedConn().on("FROM booking.trip_plans WHERE source_system = 'cis'", plan)


def gen_body(**kw: Any) -> dict[str, Any]:
    body = {
        "date_from": date(2027, 3, 1),
        "date_to": date(2027, 3, 31),
        "pattern": {"kind": "weekly", "weekdays": [1]},
        "capacity": 16,
        "min_to_operate": 6,
        "sales_cutoff_days": 3,
        "decision_days_before": 21,
        "auto_confirm": True,
        "dry_run": False,
    }
    return body | kw


def test_generate_dry_run_lists_and_writes_nothing() -> None:
    conn = plan_conn().on("SELECT start_date FROM booking.departures", [{"start_date": date(2027, 3, 8)}])
    out = run(departures.generate_departures(conn, ADMIN, "LF-9D", gen_body(dry_run=True), today=TODAY))
    assert out["created"] == [date(2027, 3, 1), date(2027, 3, 15), date(2027, 3, 22), date(2027, 3, 29)]
    assert out["skipped"] == [date(2027, 3, 8)] and out["dry_run"] is True
    assert not conn.sql("INSERT INTO") and not conn.sql("UPDATE booking.trip_plans")


def test_generate_inserts_skips_existing_and_audits_once() -> None:
    conn = plan_conn().on("ON CONFLICT (trip_plan_id, start_date) DO NOTHING", [{"start_date": date(2027, 4, 1)}])
    body = gen_body(date_to=date(2027, 5, 1), pattern={"kind": "monthly", "day": 1})
    out = run(departures.generate_departures(conn, ADMIN, "LF-9D", body, today=TODAY))
    assert (out["created_count"], out["skipped_count"]) == (1, 2)
    [args] = conn.sql("ON CONFLICT (trip_plan_id, start_date)")
    assert args == (PLAN, [date(2027, 3, 1), date(2027, 4, 1), date(2027, 5, 1)], 16, True, 6, 3, 21, "a-1")
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "departure.generated" and "1 departures generated (2 dates" in audit[9]


def test_generate_nothing_new_writes_no_audit() -> None:
    conn = plan_conn()
    out = run(departures.generate_departures(conn, ADMIN, "LF-9D", gen_body(), today=TODAY))
    assert out["created_count"] == 0 and not conn.sql("INSERT INTO booking.audit_logs")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (gen_body(date_from=TODAY), "invalid_window"),
        (gen_body(min_to_operate=17), "invalid_min_to_operate"),
        (gen_body(pattern={"kind": "monthly", "day": 31}, date_to=date(2027, 3, 30)), "no_dates"),
    ],
)
def test_generate_rules(body: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as exc:
        run(departures.generate_departures(plan_conn(), ADMIN, "LF-9D", body, today=TODAY))
    assert exc.value.code == code


def test_generate_needs_a_saved_plan() -> None:
    with pytest.raises(NotFound):
        run(departures.generate_departures(plan_conn(None), ADMIN, "LF-9D", gen_body(), today=TODAY))


# ── Departure rules ──────────────────────────────────────────────────────────


def update_conn(before: dict[str, Any], *, held: int = 0, sold: int = 0, allots: Any = ()) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(LOCK_FRAGMENT, [before])
    conn.on(USAGE_FRAGMENT, [{"departure_id": DEP, "held": held, "sold": sold}])
    conn.on(ALLOTMENTS_FRAGMENT, list(allots))
    conn.on("UPDATE booking.departures SET capacity", lambda *a: before | {"status": a[2] or before["status"]})
    return conn


@pytest.mark.parametrize(
    ("before", "changes", "code"),
    [
        (dep_row(), {"min_to_operate": 11}, "invalid_min_to_operate"),
        (dep_row(min_to_operate=8), {"capacity": 7}, "invalid_min_to_operate"),
        (dep_row(), {"sales_cutoff_days": 91}, "invalid_sales_cutoff"),
        (dep_row(), {"decision_days_before": 400}, "invalid_decision_days"),
    ],
)
def test_update_departure_bk046_rules(before: dict[str, Any], changes: dict[str, Any], code: str) -> None:
    with pytest.raises(Invalid) as exc:
        run(departures.update_departure(update_conn(before), ADMIN, DEP, changes))
    assert exc.value.code == code


def test_capacity_never_below_the_unused_allotments() -> None:
    conn = update_conn(dep_row(), held=1, sold=2, allots=live((AGENCY, 5, 0)))
    with pytest.raises(Conflict) as exc:
        run(departures.update_departure(conn, ADMIN, DEP, {"capacity": 7}))
    assert exc.value.code == "capacity_below_taken" and "5 kept for agency allotments" in exc.value.message
    out = run(departures.update_departure(conn, ADMIN, DEP, {"capacity": 8}))
    assert out["capacity"] == 10  # the fake echoes the row; the write went through
    [args] = conn.sql("UPDATE booking.departures SET capacity")
    assert args[1] == 8


def test_cancel_ends_the_allotments_and_audits_it() -> None:
    conn = update_conn(dep_row(), allots=live((AGENCY, 4, 0)))
    conn.on("end_reason = 'departure_cancelled'", "UPDATE 2")
    conn.execute = _execute_with(conn, {"end_reason = 'departure_cancelled'": "UPDATE 2"})  # type: ignore[method-assign]
    out = run(departures.update_departure(conn, ADMIN, DEP, {"status": "cancelled"}))
    assert out["status"] == "cancelled" and out["allotted"] == 0
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert '"allotments_ended": 2' in audit[7]


def _execute_with(conn: ScriptedConn, statuses: dict[str, str]) -> Any:
    original = conn.execute

    async def execute(sql: str, *args: Any) -> str:
        await original(sql, *args)
        flat = " ".join(sql.split())
        return next((v for k, v in statuses.items() if k in flat), "OK")

    return execute


def test_rowcount() -> None:
    assert departures._rowcount("UPDATE 3") == 3
    assert departures._rowcount("OK") == 0 and departures._rowcount(None) == 0


# ── Allotments ───────────────────────────────────────────────────────────────


def create_conn(*, dep: dict[str, Any] | None = None, status: str | None = "active", current: Any = ()) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(LOCK_FRAGMENT, [dep or dep_row()])
    conn.on("SELECT status FROM booking.b2b_partners", status)
    conn.on("AND ended_at IS NULL AND released_at IS NULL", list(current))
    conn.on("INSERT INTO booking.departure_allotments", ALLOT)
    conn.on(ROW_FRAGMENT, allot_row())
    return conn


def proposal(**kw: Any) -> dict[str, Any]:
    return {"partner_id": AGENCY, "seats": 4, "release_days_before": 14} | kw


def test_create_proposes_a_pending_allotment_replacing_the_one_in_force() -> None:
    conn = create_conn(current=[{"id": OLD_ALLOT, "approved_at": NOW}])
    out = run(allotments.create(conn, CONTRACTING, DEP, proposal(), today=TODAY))
    assert out["state"] == "pending" and out["partner"]["agency_name"] == "Lanternfield Travel"
    assert out["release_date"] == date(2027, 2, 15) and out["unused"] is None
    [args] = conn.sql("INSERT INTO booking.departure_allotments")
    assert args == (DEP, AGENCY, 4, 14, OLD_ALLOT, "c-1")
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "allotment.created" and "waiting for approval" in audit[9]


@pytest.mark.parametrize(
    ("conn_kw", "data", "error", "code"),
    [
        ({}, proposal(seats=0), Invalid, "invalid_seats"),
        ({}, proposal(seats=11), Invalid, "invalid_seats"),
        ({}, proposal(release_days_before=366), Invalid, "invalid_release_days"),
        ({"dep": dep_row(status="cancelled")}, proposal(), Conflict, "departure_cancelled"),
        ({}, proposal(release_days_before=28), Conflict, "release_date_passed"),
        ({"status": None}, proposal(), Invalid, "partner_not_found"),
        ({}, proposal(partner_id="not-a-uuid"), Invalid, "partner_not_found"),
        ({"status": "suspended"}, proposal(), Conflict, "partner_suspended"),
        ({"current": [{"id": OLD_ALLOT, "approved_at": None}]}, proposal(), Conflict, "allotment_pending"),
    ],
)
def test_create_rules(conn_kw: dict[str, Any], data: dict[str, Any], error: type, code: str) -> None:
    with pytest.raises(error) as exc:
        run(allotments.create(create_conn(**conn_kw), CONTRACTING, DEP, data, today=TODAY))
    assert exc.value.code == code


def test_create_on_an_unknown_departure() -> None:
    with pytest.raises(NotFound):
        run(allotments.create(ScriptedConn(), CONTRACTING, DEP, proposal(), today=TODAY))
    with pytest.raises(NotFound):
        run(allotments.create(ScriptedConn(), CONTRACTING, "nope", proposal(), today=TODAY))


def approve_conn(
    row: dict[str, Any], *, held: int = 0, sold: int = 0, others: Any = (), own_used: int = 0
) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on(ROW_FRAGMENT, row)
    conn.on(LOCK_FRAGMENT, [dep_row()])
    conn.on(USAGE_FRAGMENT, [{"departure_id": DEP, "held": held, "sold": sold}])
    conn.on(ALLOTMENTS_FRAGMENT, list(others))
    conn.on("FROM booking.b2b_hold_lines pl", own_used)
    conn.on("RETURNING id::text", [])
    return conn


def test_approve_by_another_person_ends_the_replaced_one() -> None:
    conn = approve_conn(allot_row(replaces_id=OLD_ALLOT), held=1, sold=2, others=live((OTHER, 2, 0)), own_used=1)
    conn.on("WHERE id = $1::uuid AND ended_at IS NULL RETURNING id::text", OLD_ALLOT)
    out = run(allotments.approve(conn, ADMIN, ALLOT, today=TODAY))
    assert out["used"] == 1
    assert conn.sql("SET approved_by = $2, approved_at = now()") == [(ALLOT, "a-1")]
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "allotment.approved" and OLD_ALLOT in audit[7] and '"free_after": 2' in audit[7]


def test_approve_refuses_the_maker_and_too_many_seats() -> None:
    with pytest.raises(Forbidden) as exc:
        run(allotments.approve(approve_conn(allot_row()), CONTRACTING, ALLOT, today=TODAY))
    assert exc.value.code == "maker_checker"
    conn = approve_conn(allot_row(seats=6), held=2, sold=1, others=live((OTHER, 3, 0)))
    with pytest.raises(Conflict) as exc:
        run(allotments.approve(conn, ADMIN, ALLOT, today=TODAY))
    assert exc.value.code == "allotment_exceeds_free" and "2 too many" in exc.value.message
    assert not conn.sql("SET approved_by")


def test_approve_in_force_is_a_no_op_and_ended_is_refused() -> None:
    conn = approve_conn(allot_row(approved_at=NOW, approved_by="a-1"))
    assert run(allotments.approve(conn, ADMIN, ALLOT, today=TODAY))["state"] == "in_force"
    assert not conn.sql("SET approved_by")
    ended = approve_conn(allot_row(ended_at=NOW, ended_by="a-1", end_reason="rejected"))
    with pytest.raises(Conflict) as exc:
        run(allotments.approve(ended, ADMIN, ALLOT, today=TODAY))
    assert exc.value.code == "allotment_ended"
    with pytest.raises(NotFound):
        run(allotments.approve(ScriptedConn(), ADMIN, ALLOT, today=TODAY))


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (allot_row(), "rejected"),
        (allot_row(approved_at=NOW, approved_by="a-1"), "withdrawn"),
        (allot_row(approved_at=NOW, approved_by="a-1", released_at=NOW), "withdrawn"),
    ],
)
def test_end(row: dict[str, Any], reason: str) -> None:
    conn = ScriptedConn().on(ROW_FRAGMENT, row)
    run(allotments.end(conn, SALES, ALLOT))
    [args] = conn.sql("SET ended_at = now(), ended_by = $2, end_reason = $3")
    assert args == (ALLOT, "u-1", reason)
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == f"allotment.{reason}"


def test_end_of_an_ended_allotment_is_a_no_op() -> None:
    conn = ScriptedConn().on(ROW_FRAGMENT, allot_row(ended_at=NOW, ended_by="a-1", end_reason="rejected"))
    assert run(allotments.end(conn, SALES, ALLOT))["state"] == "ended"
    assert not conn.sql("SET ended_at")


def test_list_for_departure() -> None:
    conn = ScriptedConn()
    conn.on(BY_IDS_FRAGMENT, [dep_row()])
    conn.on(USAGE_FRAGMENT, [{"departure_id": DEP, "held": 1, "sold": 1}])
    conn.on(ALLOTMENTS_FRAGMENT, live((AGENCY, 4, 1)))
    conn.on(
        "ORDER BY a.created_at DESC",
        [
            allot_row(approved_at=NOW, approved_by="a-1"),
            allot_row(id=OLD_ALLOT, partner_id=OTHER, approved_at=NOW, approved_by="a-1"),
            allot_row(id="6b000000-0000-4000-8000-000000000003", ended_at=NOW, ended_by="a-1", end_reason="rejected"),
        ],
    )
    conn.on("FROM booking.b2b_hold_lines pl", 2)
    conn.on("WHERE status = 'active' ORDER BY agency_name", [{"id": AGENCY, "agency_name": "Lanternfield Travel"}])
    out = run(allotments.list_for_departure(conn, DEP))
    assert out["departure"]["allotted"] == 4 and out["departure"]["available"] == 5
    assert [(i["state"], i["used"], i["unused"]) for i in out["items"]] == [
        ("in_force", 1, 3),
        ("in_force", 2, 2),  # in force, not in the live read: its own seats are read on their own
        ("ended", None, None),
    ]
    assert out["partner_options"] == [{"id": AGENCY, "agency_name": "Lanternfield Travel"}]


def test_release_due_live_and_dry_run() -> None:
    due = [{"id": ALLOT, "departure_id": DEP, "partner_id": AGENCY, "seats": 4, "start_date": START}]
    conn = ScriptedConn().on("d.start_date - a.release_days_before::int <= $1::date", due)
    out = run(allotments.release_due(conn, today=TODAY))
    assert out["released"] == 1 and out["allotments"] == [ALLOT] and out["more"] is False
    assert conn.sql("SET released_at = greatest(now(), approved_at)") == [([ALLOT],)]
    assert "SKIP LOCKED" in next(f for f, _ in conn.calls if "release_days_before::int" in f)
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "allotment.released"
    dry = ScriptedConn().on("d.start_date - a.release_days_before::int <= $1::date", due)
    assert run(allotments.release_due(dry, today=TODAY, dry_run=True))["released"] == 1
    assert not dry.sql("SET released_at") and "SKIP LOCKED" not in dry.calls[0][0]


def test_release_handler_reads_the_day() -> None:
    conn = ScriptedConn()
    out = run(
        allotments.handle_release(conn, Job("j-1", allotments.KIND, {"trigger": "manual", "day": "2027-02-14"}, 1, 3))
    )
    assert out["outcome"] == "ok" and out["day"] == "2027-02-14" and out["trigger"] == "manual"
    assert conn.calls[0][1][0] == date(2027, 2, 14)


# ── Departure watch ──────────────────────────────────────────────────────────


def watch_conn(rows: list[dict[str, Any]], sold: dict[str, int]) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("WHERE d.status = 'scheduled' AND d.min_to_operate >= 1", rows)
    conn.on(USAGE_FRAGMENT, [{"departure_id": k, "held": 0, "sold": v} for k, v in sold.items()])
    conn.on("INSERT INTO booking.notifications", "n-1")
    return conn


GUARANTEE = "9f000000-0000-4000-8000-00000000a001"
RISKY = "9f000000-0000-4000-8000-00000000a002"
EARLY = "9f000000-0000-4000-8000-00000000a003"
STAMPED = "9f000000-0000-4000-8000-00000000a004"


def watch_rows() -> list[dict[str, Any]]:
    return [
        dep_row(id=GUARANTEE, min_to_operate=4, source_id="LF-9D"),
        dep_row(id=RISKY, min_to_operate=6, start_date=TODAY + timedelta(days=21), source_id="LF-9D"),
        dep_row(id=EARLY, min_to_operate=6, start_date=TODAY + timedelta(days=22), source_id="LF-9D"),
        dep_row(id=STAMPED, min_to_operate=6, start_date=TODAY + timedelta(days=5), at_risk_at=NOW, source_id="LF-9D"),
    ]


def test_watch_guarantees_at_the_minimum_and_stamps_at_risk_on_the_decision_date() -> None:
    conn = watch_conn(watch_rows(), {GUARANTEE: 4, RISKY: 2, EARLY: 1, STAMPED: 0})
    out = run(departure_watch.run_watch(conn, today=TODAY))
    assert (out["checked"], out["guaranteed_count"], out["at_risk_count"]) == (4, 1, 1)
    assert [g["departure_id"] for g in out["guaranteed"]] == [GUARANTEE]
    assert [a["departure_id"] for a in out["at_risk"]] == [RISKY]
    assert conn.sql("SET status = 'guaranteed'") == [(GUARANTEE,)]
    assert conn.sql("SET at_risk_at = now()") == [(RISKY,)]
    [note] = conn.sql("INSERT INTO booking.notifications")
    assert note[1] == "departure.at_risk" and note[5] == RISKY and '"reminder_key": "at_risk"' in note[6]
    actions = [a[3] for a in conn.sql("INSERT INTO booking.audit_logs")]
    assert actions == ["departure.guaranteed", "departure.at_risk"]
    assert "FOR UPDATE OF d SKIP LOCKED" in conn.calls[0][0]


def test_watch_dry_run_reports_and_writes_nothing() -> None:
    conn = watch_conn(watch_rows(), {GUARANTEE: 4, RISKY: 2})
    out = run(departure_watch.run_watch(conn, today=TODAY, dry_run=True))
    assert (out["guaranteed_count"], out["at_risk_count"], out["dry_run"]) == (1, 1, True)
    assert not conn.sql("UPDATE booking.departures") and not conn.sql("INSERT INTO")
    assert "SKIP LOCKED" not in conn.calls[0][0]


def test_watch_handler() -> None:
    conn = watch_conn([], {})
    out = run(departure_watch.handle_watch(conn, Job("j-1", departure_watch.KIND, {"trigger": "schedule"}, 1, 3)))
    assert out["outcome"] == "ok" and out["checked"] == 0 and out["trigger"] == "schedule"
    assert set(departure_watch.HANDLERS) == {"bk_departure_watch", "bk_allotment_release"}


def test_daily_schedule_queues_both_kinds_once_a_day() -> None:
    clock = {"now": datetime(2027, 2, 1, 0, 30, tzinfo=UTC)}
    schedule = departure_watch.DepartureDailySchedule(now=lambda: clock["now"])
    conn = ScriptedConn().on("INSERT INTO booking.job", "job-1")
    assert run(schedule(conn)) == []  # before 01:00 UTC
    clock["now"] = datetime(2027, 2, 1, 1, 5, tzinfo=UTC)
    assert run(schedule(conn)) == ["job-1", "job-1"]
    assert run(schedule(conn)) == []  # once a day
    keys = [args[2] for args in conn.sql("INSERT INTO booking.job")]
    assert keys == ["bk_departure_watch:2027-02-01", "bk_allotment_release:2027-02-01"]


def test_enqueue_now_and_recent_runs() -> None:
    conn = ScriptedConn().on("INSERT INTO booking.job", "job-9")
    out = run(departure_watch.enqueue_now(conn, SALES, dry_run=True, now=NOW))
    assert out == {"dry_run": True, "jobs": {"bk_departure_watch": "job-9", "bk_allotment_release": "job-9"}}
    keys = [args[2] for args in conn.sql("INSERT INTO booking.job")]
    assert keys == ["bk_departure_watch:manual:dry:20270201T0900", "bk_allotment_release:manual:dry:20270201T0900"]
    runs = ScriptedConn().on(
        "FROM booking.job WHERE kind = ANY",
        [
            {
                "id": "j-1",
                "kind": "bk_departure_watch",
                "status": "succeeded",
                "result": '{"checked": 2}',
                "error": None,
            },
            {"id": "j-2", "kind": "bk_allotment_release", "status": "failed", "result": "not json", "error": "x" * 400},
        ],
    )
    out = run(departure_watch.recent_runs(runs))
    assert out["items"][0]["result"] == {"checked": 2} and out["items"][1]["result"] is None
    assert len(out["items"][1]["error"]) == 300


# ── Routes ───────────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self, result: Any) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))
        return self.result


def client(staff: Staff) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


def test_generate_route(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "aa679000-0000-4000-8000-00000000107a"
    rec = Recorder({"created": []})
    monkeypatch.setattr(departures, "generate_departures", rec)
    url = f"/booking/catalog/trip-plans/{source}/departures:generate"
    body = {"date_from": "2027-03-01", "date_to": "2027-04-01", "pattern": {"kind": "weekly", "weekdays": [6]},
            "capacity": 16}  # fmt: skip
    assert client(SALES).post(url, json=body).status_code == 403
    assert client(ADMIN).post(url, json={**body, "pattern": {"kind": "weekly", "weekdays": [0]}}).status_code == 422
    assert client(ADMIN).post(url, json={**body, "pattern": {"kind": "monthly"}}).status_code == 422
    assert client(ADMIN).post(url, json={**body, "sales_cutoff_days": 91}).status_code == 422
    assert client(CONTRACTING).post(url, json=body).status_code == 200
    (_, src, data), _ = rec.calls[0]
    assert src == source and data["pattern"] == {"kind": "weekly", "weekdays": [6]}
    assert (data["min_to_operate"], data["sales_cutoff_days"], data["decision_days_before"], data["dry_run"]) == (
        0,
        0,
        None,  # S279 L3: no value from the form = the policy default, read by app.b2b.departures
        False,
    )


@pytest.mark.asyncio
async def test_a_departure_without_a_decision_day_gets_the_policy_default() -> None:
    conn = ScriptedConn().on(POLICY_FRAGMENT, policy_rows(departure_decision_days_default="30"))
    assert await departures._rules_of(conn, {"capacity": 10}) == (10, 0, 0, 30)
    assert await departures._rules_of(conn, {"capacity": 10, "decision_days_before": 7}) == (10, 0, 0, 7)
    missing = [r for r in policy_rows() if r["param_key"] != "departure_decision_days_default"]
    with pytest.raises(Unavailable):
        await departures._rules_of(ScriptedConn().on(POLICY_FRAGMENT, missing), {"capacity": 10})


def test_allotment_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    listing, made, approved, ended = Recorder({"items": []}), Recorder({"id": ALLOT}), Recorder({}), Recorder({})
    monkeypatch.setattr(allotments, "list_for_departure", listing)
    monkeypatch.setattr(allotments, "create", made)
    monkeypatch.setattr(allotments, "approve", approved)
    monkeypatch.setattr(allotments, "end", ended)
    url = f"/booking/catalog/departures/{DEP}/allotments"
    assert client(CREATOR).get(url).status_code == 403
    assert client(SALES).get(url).status_code == 200 and listing.calls[0][0] == (DEP,)
    body = {"partner_id": AGENCY, "seats": 4, "release_days_before": 14}
    assert client(CREATOR).post(url, json=body).status_code == 403
    assert client(SALES).post(url, json={**body, "seats": 0}).status_code == 422
    assert client(SALES).post(url, json=body).status_code == 201
    (_, dep, data), _ = made.calls[0]
    assert dep == DEP and data == body
    assert client(ADMIN).post(f"/booking/catalog/allotments/{ALLOT}/approve").status_code == 200
    assert client(ADMIN).post(f"/booking/catalog/allotments/{ALLOT}/end").status_code == 200
    assert approved.calls[0][0] == (ADMIN, ALLOT) and ended.calls[0][0] == (ADMIN, ALLOT)
    assert client(CREATOR).post(f"/booking/catalog/allotments/{ALLOT}/approve").status_code == 403


def test_departure_check_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    queued, runs = Recorder({"jobs": {}}), Recorder({"items": []})
    monkeypatch.setattr(departure_watch, "enqueue_now", queued)
    monkeypatch.setattr(departure_watch, "recent_runs", runs)
    url = "/booking/b2b-desk/departure-checks/runs"
    assert client(CREATOR).post(url, json={}).status_code == 403
    assert client(SALES).post(url, json={"dry_run": True}).status_code == 202
    assert queued.calls[0][1] == {"dry_run": True}
    assert client(SALES).get(url, params={"limit": 5}).status_code == 200 and runs.calls[0][0] == (5,)
