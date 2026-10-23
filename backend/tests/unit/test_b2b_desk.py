"""AA-975 desk, feed and outbox services with a scripted connection: the SLA badge and policy fallbacks, opening
requests at confirm, the on-request list (labels, pages), answering (idempotent, 409, 404, 422), the summary, the
departure list, incidents (flag / clear rules), the staff routes and their permissions, the outbox stamper and the
B2B event feed (cursor rules). The SQL itself (triggers, grants, commit order) runs against Postgres in
tests/integration/test_bk022_*.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import outbox
from app.auth.staff import Staff, current_staff, get_pool
from app.b2b import desk, events
from app.b2b.errors import B2BError
from app.errors import Conflict, Invalid, NotFound
from app.main import create_app
from app.settings import Settings
from tests.staff_tokens import OTHER_M2M_CLIENT, LocalM2M
from tests.unit.fakes import ScriptedConn
from tests.unit.test_b2b_api import PARTNER_ID, _client, _conn, _headers

NOW = datetime(2027, 2, 1, 9, 0, tzinfo=UTC)
HOLD = "9b000000-0000-4000-8000-000000000975"
CASE = "9c000000-0000-4000-8000-000000000975"
DEP = "9f000000-0000-4000-8000-000000000975"
INCIDENT = "9e000000-0000-4000-8000-000000000975"
ROOM = "8e000000-0000-4000-8000-000000000a02"
SERVICE = "8e000000-0000-4000-8000-000000000a03"
PLAN = "8e000000-0000-4000-8000-000000000a01"
SALES = Staff("u-1", None, "Sales One", "sales", True, "reviewer")
CREATOR = Staff("u-2", None, "Writer", "content_creator", True, "reviewer")


def line_row(**kw: Any) -> dict[str, Any]:
    row = {
        "hold_id": HOLD,
        "line_no": 1,
        "line_ref": "room",
        "status": "on_request",
        "seats": 0,
        "departure_id": None,
        "answer_due_at": NOW + timedelta(hours=10),
        "answered_at": None,
        "decline_code": None,
        "answered_by": None,
        "answered_by_name": None,
        "confirmed_at": NOW - timedelta(hours=1),
        "trip_case_id": CASE,
        "trip_case_code": "TC-000975",
        "proposal_ref": "P-1",
        "partner_id": PARTNER_ID,
        "agency_name": "Agency One",
        "tenant_id": "t-1",
        "component_id": f"rt:{ROOM}",
        "component_kind": "hotel_room",
        "service_date": date(2027, 3, 1),
        "nights": 1,
        "pax_adult": 2,
        "pax_child": 0,
        "pax_infant": 0,
        "departure_start": None,
    }
    return row | kw


def incident_row(**kw: Any) -> dict[str, Any]:
    row = {
        "id": INCIDENT,
        "departure_id": DEP,
        "incident_code": "road",
        "flagged_at": NOW,
        "flagged_by": "u-1",
        "flagged_by_name": "Sales One",
        "cleared_at": None,
        "cleared_by": None,
        "cleared_by_name": None,
    }
    return row | kw


def departure_row(**kw: Any) -> dict[str, Any]:
    row = {
        "id": DEP,
        "trip_plan_id": PLAN,
        "start_date": date(2027, 3, 1),
        "capacity": 6,
        "status": "scheduled",
        "auto_confirm": True,
        "created_at": NOW,
        "updated_at": NOW,
        "source_id": "src-1",
    }
    return row | kw


def run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


# ── SLA and policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "due", "state"),
    [
        ("confirmed", NOW, "answered"),
        ("declined", None, "answered"),
        ("cancelled", NOW, "closed"),
        ("on_request", None, "on_time"),
        ("on_request", NOW, "overdue"),
        ("on_request", NOW - timedelta(minutes=1), "overdue"),
        ("on_request", NOW + timedelta(hours=4), "due_soon"),
        ("on_request", NOW + timedelta(hours=4, minutes=1), "on_time"),
    ],
)
def test_sla_state(status: str, due: datetime | None, state: str) -> None:
    assert desk.sla_state(status, due, NOW) == state


def test_answer_policy_reads_settings_and_falls_back() -> None:
    conn = ScriptedConn().on("key = $1", lambda key: {desk.DAYS_SETTING: "2", desk.TZ_SETTING: '"Asia/Bangkok"'}[key])
    assert run(desk.answer_policy(conn)) == (2, "Asia/Bangkok")
    bad = ScriptedConn().on("key = $1", lambda key: {desk.DAYS_SETTING: "99", desk.TZ_SETTING: "7"}[key])
    assert run(desk.answer_policy(bad)) == (desk.DEFAULT_DAYS, desk.DEFAULT_TZ)
    unknown = (
        ScriptedConn()
        .on("key = $1", lambda key: {desk.DAYS_SETTING: "true", desk.TZ_SETTING: '"Mars/Base"'}[key])
        .on("AT TIME ZONE", asyncpg.InvalidParameterValueError("time zone not recognized"))
    )
    assert run(desk.answer_policy(unknown)) == (desk.DEFAULT_DAYS, desk.DEFAULT_TZ)
    broken = ScriptedConn().on("key = $1", "{not json")
    assert run(desk.answer_policy(broken)) == (desk.DEFAULT_DAYS, desk.DEFAULT_TZ)


def test_open_requests_sets_deadlines_and_notifies_once() -> None:
    none = ScriptedConn()
    assert run(desk.open_requests(none, hold_id=HOLD, case_id=CASE, case_code="TC-1")) == 0
    assert none.sql("INSERT INTO booking.notifications") == []
    due = NOW + timedelta(days=1)
    conn = ScriptedConn().on(
        "SET answer_due_at",
        [{"line_ref": "b", "answer_due_at": due + timedelta(hours=1)}, {"line_ref": "a", "answer_due_at": due}],
    )
    assert run(desk.open_requests(conn, hold_id=HOLD, case_id=CASE, case_code="TC-1")) == 2
    [(days_args)] = conn.sql("SET answer_due_at")
    assert days_args == (HOLD, desk.DEFAULT_DAYS, desk.DEFAULT_TZ)
    [note] = conn.sql("INSERT INTO booking.notifications")
    assert note[1] == desk.NOTIFICATION_TYPE and note[5] == CASE and "2 line(s)" in note[3]
    assert '"line_refs": ["a", "b"]' in note[6]


# ── On-request list and answers ──────────────────────────────────────────────


def _labelled(conn: ScriptedConn) -> ScriptedConn:
    conn.on(
        "FROM booking.hotel_room_types rt",
        [{"id": ROOM, "label": "Hotel · Deluxe", "supplier_id": "s-1", "supplier": "Red River"}],
    )
    conn.on("FROM booking.services v", [{"id": SERVICE, "label": "Pick-up", "supplier_id": "s-2", "supplier": "Cars"}])
    conn.on("FROM booking.trip_plans WHERE id = ANY", [{"id": PLAN, "source_id": "src-1"}])
    conn.on("FROM catalog.v_tour", [{"source_id": "src-1", "title": "Hanoi 1D"}])
    return conn


def test_list_on_request_labels_every_component_kind() -> None:
    rows = [
        line_row(),
        line_row(line_ref="svc", line_no=2, component_id=f"sv:{SERVICE}", component_kind="transfer"),
        line_row(
            line_ref="tour",
            line_no=3,
            component_id=f"tp:{PLAN}",
            component_kind="trip_plan",
            departure_id=DEP,
            departure_start=date(2027, 3, 1),
            answer_due_at=NOW - timedelta(hours=1),
        ),
        line_row(line_ref="odd", line_no=4, component_id="xx:1"),
    ]
    conn = _labelled(ScriptedConn().on("SELECT count(*)", 4).on("ORDER BY l.answer_due_at", rows))
    out = run(desk.list_on_request(conn, now=NOW))
    assert out["total"] == 4 and out["state"] == "open"
    by_ref = {i["line_ref"]: i for i in out["items"]}
    assert by_ref["room"]["component"] == {
        "id": f"rt:{ROOM}",
        "kind": "hotel_room",
        "label": "Hotel · Deluxe",
        "supplier": {"id": "s-1", "name": "Red River"},
    }
    assert by_ref["svc"]["component"]["label"] == "Pick-up"
    assert by_ref["tour"]["component"]["label"] == "Hanoi 1D" and by_ref["tour"]["component"]["supplier"] is None
    assert by_ref["tour"]["sla"] == "overdue" and by_ref["tour"]["departure"] == {
        "id": DEP,
        "start_date": date(2027, 3, 1),
    }
    assert by_ref["odd"]["component"]["label"] is None and by_ref["room"]["sla"] == "on_time"
    assert by_ref["room"]["pax"] == {"adult": 2, "child": 0, "infant": 0}


def test_list_on_request_states_pages_and_catalog_outage() -> None:
    with pytest.raises(Invalid):
        run(desk.list_on_request(ScriptedConn(), state="closed"))
    with pytest.raises(Invalid):
        run(desk.list_on_request(ScriptedConn(), limit=0))
    with pytest.raises(Invalid):
        run(desk.list_on_request(ScriptedConn(), offset=-1))
    answered = line_row(
        component_id=f"tp:{PLAN}", status="declined", decline_code="other", answered_at=NOW, answered_by="u-1",
        answered_by_name="Sales One",
    )  # fmt: skip
    conn = (
        ScriptedConn()
        .on("SELECT count(*)", 1)
        .on("ORDER BY l.answered_at DESC", [answered])
        .on("FROM booking.trip_plans WHERE id = ANY", [{"id": PLAN, "source_id": "src-12345678"}])
        .on("FROM catalog.v_tour", asyncpg.UndefinedTableError("no catalog"))
    )
    out = run(desk.list_on_request(conn, state="answered", limit=10, offset=5, now=NOW))
    [item] = out["items"]
    assert item["sla"] == "answered" and item["answered_by"] == {"id": "u-1", "name": "Sales One"}
    assert item["component"]["label"] == "Tour src-1234"
    assert conn.sql("LIMIT $1 OFFSET $2") == [(10, 5)]
    every = ScriptedConn().on("SELECT count(*)", None)
    assert run(desk.list_on_request(every, state="all", now=NOW))["total"] == 0


def _answering(status: str = "on_request", hold_status: str = "confirmed", **line: Any) -> ScriptedConn:
    conn = _labelled(ScriptedConn())
    conn.on("FROM booking.b2b_holds WHERE id = $1::uuid FOR UPDATE", {"status": hold_status})
    conn.on(
        "SELECT held_as, status, decline_code FROM booking.b2b_hold_lines",
        {"held_as": "on_request", "status": status, "decline_code": None} | line,
    )
    conn.on("WHERE l.hold_id = $1::uuid AND l.line_ref = $2", line_row(status="confirmed", answered_at=NOW))
    return conn


def test_answer_line_accept_and_decline_write_and_audit() -> None:
    conn = _answering()
    out = run(desk.answer_line(conn, SALES, HOLD, "room", accept=True))
    assert out["status"] == "confirmed"
    [update] = conn.sql("UPDATE booking.b2b_hold_lines SET status = $3")
    assert update == (HOLD, "room", "confirmed", None, "u-1")
    [audit_row] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit_row[3] == "b2b_line.accepted"

    conn = _answering()
    run(desk.answer_line(conn, SALES, HOLD, "room", accept=False, decline_code="sold_out"))
    [update] = conn.sql("UPDATE booking.b2b_hold_lines SET status = $3")
    assert update[2:4] == ("declined", "sold_out")
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "b2b_line.declined"


def test_answer_line_refusals_and_replays() -> None:
    with pytest.raises(NotFound):
        run(desk.answer_line(ScriptedConn(), SALES, "not-a-uuid", "room", accept=True))
    with pytest.raises(Invalid):
        run(desk.answer_line(_answering(), SALES, HOLD, "room", accept=False, decline_code="because"))
    with pytest.raises(NotFound):  # no such line
        run(desk.answer_line(ScriptedConn(), SALES, HOLD, "room", accept=True))
    with pytest.raises(NotFound):  # held available: not an on-request line
        run(desk.answer_line(_answering(held_as="available"), SALES, HOLD, "room", accept=True))
    with pytest.raises(NotFound):  # the hold is not a confirmed booking
        run(desk.answer_line(_answering(hold_status="active"), SALES, HOLD, "room", accept=True))
    with pytest.raises(Conflict):
        run(desk.answer_line(_answering(status="declined", decline_code="other"), SALES, HOLD, "room", accept=True))
    with pytest.raises(Conflict):  # another decline code on a declined line
        run(
            desk.answer_line(
                _answering(status="declined", decline_code="other"), SALES, HOLD, "room", accept=False,
                decline_code="sold_out",
            )
        )  # fmt: skip
    replay = _answering(status="confirmed")
    assert run(desk.answer_line(replay, SALES, HOLD, "room", accept=True))["status"] == "confirmed"
    assert replay.sql("UPDATE booking.b2b_hold_lines") == [] and replay.sql("INSERT INTO booking.audit_logs") == []
    same = _answering(status="declined", decline_code="other")
    run(desk.answer_line(same, SALES, HOLD, "room", accept=False, decline_code="other"))
    assert same.sql("UPDATE booking.b2b_hold_lines") == []
    gone = _answering().on("WHERE l.hold_id = $1::uuid AND l.line_ref = $2", None)
    with pytest.raises(NotFound):
        run(desk.answer_line(gone, SALES, HOLD, "room", accept=True))


def test_summary_counts() -> None:
    conn = ScriptedConn().on("FILTER", {"open": 3, "overdue": 1, "due_soon": 1}).on("departure_incidents", 2)
    assert run(desk.summary(conn, now=NOW)) == {
        "on_request": {"open": 3, "overdue": 1, "due_soon": 1},
        "open_incidents": 2,
        "due_soon_hours": desk.DUE_SOON_HOURS,
    }
    [args] = conn.sql("FILTER")
    assert args == (NOW, NOW + timedelta(hours=desk.DUE_SOON_HOURS))
    assert (
        run(desk.summary(ScriptedConn().on("FILTER", {"open": 0, "overdue": 0, "due_soon": 0})))["open_incidents"] == 0
    )


# ── Departures and incidents ─────────────────────────────────────────────────


def test_list_departures_merges_seats_partners_incident_and_title() -> None:
    with pytest.raises(Invalid):
        run(desk.list_departures(ScriptedConn(), start=date(2027, 3, 2), end=date(2027, 3, 1)))
    with pytest.raises(Invalid):
        run(desk.list_departures(ScriptedConn(), start=date(2027, 1, 1), end=date(2028, 6, 1)))
    conn = (
        ScriptedConn()
        .on("FROM booking.departures d JOIN booking.trip_plans", [departure_row(), departure_row(id="d-2")])
        .on("FROM booking.b2b_hold_lines l JOIN booking.b2b_holds h", [{"departure_id": DEP, "held": 1, "sold": 2}])
        .on("count(DISTINCT h.partner_id)", [{"departure_id": DEP, "partners": 2}])
        .on("i.cleared_at IS NULL", [incident_row()])
        .on("FROM catalog.v_tour", [{"source_id": "src-1", "title": "Hanoi 1D"}])
    )
    out = run(desk.list_departures(conn, today=date(2027, 2, 1)))
    assert out["from"] == date(2027, 1, 18) and out["to"] == date(2027, 5, 2)
    first, second = out["items"]
    assert (first["held"], first["sold"], first["available"], first["partners"]) == (1, 2, 3, 2)
    assert first["open_incident"]["id"] == INCIDENT and first["tour_title"] == "Hanoi 1D"
    assert second["open_incident"] is None and second["partners"] == 0
    empty = run(desk.list_departures(ScriptedConn(), start=date(2027, 3, 1), end=date(2027, 3, 1)))
    assert empty["items"] == []


def test_list_incidents() -> None:
    with pytest.raises(NotFound):
        run(desk.list_incidents(ScriptedConn(), "nope"))
    with pytest.raises(NotFound):
        run(desk.list_incidents(ScriptedConn(), DEP))
    conn = ScriptedConn().on("SELECT 1 FROM booking.departures", 1).on("ORDER BY i.flagged_at DESC", [incident_row()])
    [item] = run(desk.list_incidents(conn, DEP))["items"]
    assert item["flagged_by"] == {"id": "u-1", "name": "Sales One"} and item["cleared_by"] is None


def _flagging(status: str = "scheduled", open_id: str | None = None) -> ScriptedConn:
    conn = ScriptedConn()
    conn.on("ORDER BY d.id FOR UPDATE", [departure_row(status=status)])
    conn.on("cleared_at IS NULL", open_id)
    conn.on("count(DISTINCT h.partner_id)", [{"departure_id": DEP, "partners": 3}])
    conn.on("INSERT INTO booking.departure_incidents", INCIDENT)
    conn.on("WHERE i.id = $1::uuid", incident_row())
    return conn


def test_flag_incident_rules() -> None:
    with pytest.raises(Invalid):
        run(desk.flag_incident(_flagging(), SALES, DEP, "a traveller fell ill"))
    with pytest.raises(NotFound):
        run(desk.flag_incident(ScriptedConn(), SALES, DEP, "road"))
    with pytest.raises(NotFound):
        run(desk.flag_incident(ScriptedConn(), SALES, "bad-id", "road"))
    with pytest.raises(Conflict) as cancelled:
        run(desk.flag_incident(_flagging(status="cancelled"), SALES, DEP, "road"))
    assert cancelled.value.code == "departure_cancelled"
    with pytest.raises(Conflict) as already:
        run(desk.flag_incident(_flagging(open_id=INCIDENT), SALES, DEP, "road"))
    assert already.value.code == "incident_open" and already.value.extra == {"incident_id": INCIDENT}
    conn = _flagging()
    out = run(desk.flag_incident(conn, SALES, DEP, "road"))
    assert out["partners_notified"] == 3 and out["incident_code"] == "road"
    assert conn.sql("INSERT INTO booking.departure_incidents") == [(DEP, "road", "u-1")]
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "departure.incident_flagged"


def test_clear_incident_once() -> None:
    with pytest.raises(NotFound):
        run(desk.clear_incident(ScriptedConn(), SALES, "bad"))
    with pytest.raises(NotFound):
        run(desk.clear_incident(ScriptedConn(), SALES, INCIDENT))
    conn = (
        ScriptedConn()
        .on("FROM booking.departure_incidents WHERE id = $1::uuid FOR UPDATE", incident_row())
        .on("count(DISTINCT h.partner_id)", [])
        .on("WHERE i.id = $1::uuid", incident_row(cleared_at=NOW, cleared_by="u-1", cleared_by_name="Sales One"))
    )
    out = run(desk.clear_incident(conn, SALES, INCIDENT))
    assert out["cleared_by"] == {"id": "u-1", "name": "Sales One"}
    assert conn.sql("SET cleared_at") == [(INCIDENT, "u-1")]
    assert conn.sql("INSERT INTO booking.audit_logs")[0][3] == "departure.incident_cleared"
    again = (
        ScriptedConn()
        .on("FOR UPDATE", incident_row(cleared_at=NOW))
        .on("WHERE i.id = $1::uuid", incident_row(cleared_at=NOW, cleared_by="u-1"))
    )
    run(desk.clear_incident(again, SALES, INCIDENT))
    assert again.sql("SET cleared_at") == [] and again.sql("INSERT INTO booking.audit_logs") == []


# ── Staff routes ─────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self, result: Any) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = result

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args[1:], kwargs))
        return self.result


def staff_client(staff: Staff) -> TestClient:
    app = create_app(Settings(database_url=None, environment="test"))
    app.dependency_overrides[current_staff] = lambda: staff
    app.dependency_overrides[get_pool] = lambda: ScriptedConn()
    return TestClient(app)


def test_desk_routes_and_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = {
        name: Recorder({"ok": name})
        for name in (
            "summary",
            "list_on_request",
            "answer_line",
            "list_departures",
            "list_incidents",
            "flag_incident",
            "clear_incident",
        )
    }
    for name, fn in rec.items():
        monkeypatch.setattr(desk, name, fn)
    sales, creator = staff_client(SALES), staff_client(CREATOR)
    base = "/booking/b2b-desk"
    assert sales.get(f"{base}/summary").json() == {"ok": "summary"}
    assert sales.get(f"{base}/on-request", params={"state": "answered", "limit": 5}).status_code == 200
    assert rec["list_on_request"].calls[-1][1] == {"state": "answered", "limit": 5, "offset": 0}
    assert sales.get(f"{base}/on-request", params={"state": "closed"}).status_code == 422
    assert sales.post(f"{base}/on-request/{HOLD}/lines/room/accept").status_code == 200
    assert rec["answer_line"].calls[-1] == ((SALES, HOLD, "room"), {"accept": True})
    decline = sales.post(f"{base}/on-request/{HOLD}/lines/room/decline", json={"decline_code": "sold_out"})
    assert decline.status_code == 200
    assert rec["answer_line"].calls[-1][1] == {"accept": False, "decline_code": "sold_out"}
    assert sales.post(f"{base}/on-request/{HOLD}/lines/room/decline", json={"decline_code": "x"}).status_code == 422
    assert sales.get(f"{base}/departures", params={"from": "2027-03-01", "to": "2027-04-01"}).status_code == 200
    assert rec["list_departures"].calls[-1][1] == {"start": date(2027, 3, 1), "end": date(2027, 4, 1)}
    assert sales.get(f"{base}/departures/{DEP}/incidents").status_code == 200
    flagged = sales.post(f"{base}/departures/{DEP}/incidents", json={"incident_code": "weather"})
    assert flagged.status_code == 201 and rec["flag_incident"].calls[-1][0] == (SALES, DEP, "weather")
    free_text = sales.post(f"{base}/departures/{DEP}/incidents", json={"incident_code": "road", "note": "Mr X"})
    assert free_text.status_code == 422
    assert sales.post(f"{base}/incidents/{INCIDENT}/clear").status_code == 200
    for method, url in (
        ("get", f"{base}/summary"),
        ("get", f"{base}/on-request"),
        ("post", f"{base}/on-request/{HOLD}/lines/room/accept"),
        ("get", f"{base}/departures"),
        ("post", f"{base}/incidents/{INCIDENT}/clear"),
    ):
        assert getattr(creator, method)(url).status_code == 403, url


# ── Outbox stamper ───────────────────────────────────────────────────────────


def test_cursor_round_trip_and_refusals() -> None:
    assert outbox.decode_cursor(None) == 0 and outbox.decode_cursor("") == 0
    assert outbox.decode_cursor(outbox.encode_cursor(42)) == 42 == outbox.decode_cursor(" bk1.42 ")
    for bad in ("42", "bk2.4", "bk1.-1", "bk1.x", "bk1." + "9" * 19):
        with pytest.raises(outbox.CursorError):
            outbox.decode_cursor(bad)


def test_sequence_pending_stamps_oldest_first_in_batches() -> None:
    batches = [[{"id": 5}, {"id": 3}], [{"id": 9}]]
    conn = (
        ScriptedConn()
        .on("pg_try_advisory_xact_lock", True)
        .on("WHERE feed_seq IS NULL ORDER BY id", lambda batch: batches.pop(0) if batches else [])
        .on("nextval('booking.outbox_feed_seq')", lambda n: [{"n": 12}, {"n": 11}][:n] if n == 2 else [{"n": 13}])
    )
    assert run(outbox.sequence_pending(conn, batch=2)) == 3
    stamps = conn.sql("SET feed_seq = s.n")
    assert stamps == [([5, 3], [11, 12]), ([9], [13])]
    idle = ScriptedConn().on("pg_try_advisory_xact_lock", True)
    assert run(outbox.sequence_pending(idle)) == 0
    busy = ScriptedConn().on("pg_try_advisory_xact_lock", False)
    assert run(outbox.sequence_pending(busy)) is None and busy.sql("feed_seq IS NULL ORDER BY id") == []
    full = [[{"id": 1}], [{"id": 2}]]
    late = (
        ScriptedConn()
        .on("pg_try_advisory_xact_lock", lambda key: bool(full))
        .on("WHERE feed_seq IS NULL ORDER BY id", lambda batch: full.pop(0))
        .on("nextval", [{"n": 1}])
    )
    assert run(outbox.sequence_pending(late, batch=1)) == 2  # the lock was lost after two full batches


def test_outbox_sequencer_hook() -> None:
    conn = (
        ScriptedConn()
        .on("pg_try_advisory_xact_lock", True)
        .on("WHERE feed_seq IS NULL ORDER BY id", [{"id": 1}])
        .on("nextval", [{"n": 7}])
    )
    assert run(outbox.OutboxSequencer()(conn)) is None
    assert conn.sql("SET feed_seq = s.n") == [([1], [7])]
    run(outbox.OutboxSequencer()(ScriptedConn().on("pg_try_advisory_xact_lock", True)))


# ── Event feed ───────────────────────────────────────────────────────────────


def event_row(seq: int, **kw: Any) -> dict[str, Any]:
    row = {
        "event_id": f"00000000-0000-4000-8000-{seq:012d}",
        "feed_seq": seq,
        "event_type": "departure.incident",
        "occurred_at": NOW,
        "hold_id": None,
        "booking_id": None,
        "line_ref": None,
        "departure_id": DEP,
        "incident_code": "road",
        "decline_code": None,
    }
    return row | kw


def test_list_events_cursor_rules() -> None:
    with pytest.raises(B2BError) as bad:
        run(events.list_events(ScriptedConn(), partner_id=PARTNER_ID, after="nope", limit=10))
    assert bad.value.error == "invalid"
    with pytest.raises(B2BError):
        run(events.list_events(ScriptedConn(), partner_id=PARTNER_ID, after=None, limit=0))
    ahead = ScriptedConn().on("pg_try_advisory_xact_lock", True).on("coalesce(max(feed_seq), 0)", 5)
    with pytest.raises(B2BError):
        run(events.list_events(ahead, partner_id=PARTNER_ID, after="bk1.6", limit=10))
    conn = (
        ScriptedConn()
        .on("pg_try_advisory_xact_lock", True)
        .on("coalesce(max(feed_seq), 0)", 9)
        .on("ORDER BY feed_seq", [event_row(7), event_row(9, event_type="line.declined", decline_code="other")])
    )
    page = run(events.list_events(conn, partner_id=PARTNER_ID, after="bk1.5", limit=2))
    assert [e["cursor"] for e in page["items"]] == ["bk1.7", "bk1.9"] and page["next_cursor"] == "bk1.9"
    assert page["items"][0]["new_start_time"] is None and page["items"][1]["decline_code"] == "other"
    assert conn.sql("ORDER BY feed_seq") == [(PARTNER_ID, 5, 2)]
    empty = ScriptedConn().on("pg_try_advisory_xact_lock", True).on("coalesce(max(feed_seq), 0)", 5)
    assert run(events.list_events(empty, partner_id=PARTNER_ID, after="bk1.5", limit=2)) == {
        "items": [],
        "next_cursor": "bk1.5",
    }


def test_events_route_auth_and_shape() -> None:
    m2m = LocalM2M()
    conn = (
        _conn()
        .on("pg_try_advisory_xact_lock", True)
        .on("coalesce(max(feed_seq), 0)", 7)
        .on("ORDER BY feed_seq", [event_row(7)])
    )
    client = _client(conn, m2m)
    ok = client.get("/booking/b2b/v1/events", headers=_headers(m2m.token()))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["next_cursor"] == "bk1.7" and body["items"][0]["type"] == "departure.incident"
    # Linear AA-975 R: a non-CRM client is 403; a token without the read scope too; a bad cursor is a Problem.
    refused = client.get("/booking/b2b/v1/events", headers=_headers(m2m.token(client_id=OTHER_M2M_CLIENT)))
    assert refused.status_code == 403 and refused.json()["error"] == "forbidden"
    scoped = client.get("/booking/b2b/v1/events", headers=_headers(m2m.token(scope="booking.b2b/hold")))
    assert scoped.status_code == 403
    bad = client.get("/booking/b2b/v1/events?after=12", headers=_headers(m2m.token()))
    assert bad.status_code == 422 and bad.json()["error"] == "invalid"
    big = client.get("/booking/b2b/v1/events?limit=501", headers=_headers(m2m.token()))
    assert big.status_code == 422 and big.json()["error"] == "invalid"
