"""AA-986 L5 (root ADR 0015 decision 5): the served state of a catalog key as Booking reads it, the B2B refusal
``tour_not_served``, the withdrawn products of the delta feed, the lifecycle outbox pull (staff notifications), the
trip case flags and the orphaned trip plans. Python branches only; the SQL against real views is in
tests/integration/test_aa986_tour_liveness.py."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from app.b2b import served
from app.b2b.errors import B2BError
from app.catalog import lifecycle, liveness, trip_plans
from app.jobs.queue import Job
from app.trip_cases import service
from tests.staff_tokens import LocalM2M
from tests.unit.fakes import (
    PLAN_KEYS_FRAGMENT,
    STATES_FRAGMENT,
    VIEW_READY_FRAGMENT,
    ScriptedConn,
    served_plan,
    state_row,
)
from tests.unit.test_b2b_api import _client, _conn, _headers, _quote_body
from tests.unit.test_trip_cases_service import case_row, transitions
from tests.unit.test_trip_plans import SALES

PLAN = "8e000000-0000-4000-8000-0000000000e1"
KEY = "5a000000-0000-4000-8000-000000000001"
WHEN = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)


# ── The rule ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("row", "state", "reason"),
    [
        (state_row(KEY), liveness.SERVED, None),
        (state_row(KEY, in_catalog=False), liveness.NOT_IN_CATALOG, "not_in_catalog"),
        (
            state_row(KEY, in_catalog=False, tomb_state="retired", tomb_reason="source_deleted"),
            "retired",
            "source_deleted",
        ),
        (
            state_row(KEY, in_catalog=False, tomb_state="paused", tomb_reason="master_inactive"),
            "paused",
            "master_inactive",
        ),
        # CIS 274 keeps a row with state 'restored' once a key is served again: not a tombstone, v_tour decides
        (state_row(KEY, tomb_state="restored", tomb_reason="restore"), liveness.SERVED, "restore"),
        (state_row(KEY, in_catalog=False, tomb_state="restored", tomb_reason="restore"), "not_in_catalog", None),
        # a state this module does not know is no tombstone either (never sold blindly: absent from v_tour = refused)
        (state_row(KEY, tomb_state="superseded", tomb_reason="superseded_pending"), liveness.SERVED, None),
        (state_row(KEY, in_catalog=False, tomb_state="superseded"), "not_in_catalog", "not_in_catalog"),
        # fail closed: a tombstone wins over v_tour (the nightly probe reports the disagreement)
        (state_row(KEY, tomb_state="retired", tomb_reason="master_trashed"), "retired", "master_trashed"),
    ],
)
def test_key_state_rule(row: dict[str, Any], state: str, reason: str | None) -> None:
    got = liveness.key_state(row)
    assert got.state == state and got.served == (state == liveness.SERVED)
    if reason is not None:
        assert got.reason == reason


@pytest.mark.asyncio
async def test_without_cis_274_the_read_uses_v_tour_alone() -> None:
    conn = ScriptedConn().on(VIEW_READY_FRAGMENT, None).on(STATES_FRAGMENT, [state_row(KEY, in_catalog=False)])
    got = await liveness.states(conn, [KEY, KEY, None, ""])
    assert got[KEY].state == liveness.NOT_IN_CATALOG
    [(flat, args)] = [c for c in conn.calls if STATES_FRAGMENT in c[0]]
    assert "v_tour_retired" not in flat and args == ([KEY],)  # distinct, non-empty keys


@pytest.mark.asyncio
async def test_with_cis_274_the_tombstone_is_joined_and_detection_is_cached() -> None:
    conn = ScriptedConn().on(VIEW_READY_FRAGMENT, True)
    conn.on(STATES_FRAGMENT, [state_row(KEY, in_catalog=False, tomb_state="retired", tomb_reason="source_deleted")])
    first = await liveness.states(conn, [KEY])
    await liveness.states(conn, [KEY])
    assert first[KEY].state == liveness.RETIRED
    assert len(conn.sql(VIEW_READY_FRAGMENT)) == 1  # cached for CACHE_SECONDS
    assert all("catalog.v_tour_retired" in flat for flat, _ in conn.calls if STATES_FRAGMENT in flat)
    ready_args = conn.sql(VIEW_READY_FRAGMENT)[0]
    assert ready_args[:2] == ("catalog", "v_tour_retired") and "changed_at" in ready_args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make",
    [
        lambda: asyncpg.UndefinedTableError("relation catalog.v_tour does not exist"),
        lambda: asyncpg.QueryCanceledError("canceling statement due to statement timeout"),
        TimeoutError,  # the pool's command_timeout
    ],
)
async def test_soft_read_survives_a_catalog_error_and_strict_raises(make: Any) -> None:
    def boom(*_: Any) -> Exception:
        return make()

    conn = ScriptedConn().on(VIEW_READY_FRAGMENT, True).on(STATES_FRAGMENT, boom)
    assert await liveness.states(conn, [KEY]) == {}
    with pytest.raises(type(make())):
        await liveness.states(conn, [KEY], strict=True)
    assert await liveness.states(conn, []) == {}


# ── B2B refusal ──────────────────────────────────────────────────────────────


def _plan_line(ref: str = "T") -> dict[str, Any]:
    return {"line_ref": ref, "component_id": f"tp:{PLAN}", "service_date": "2027-03-01", "pax": {"adult": 2}}


@pytest.mark.parametrize(
    ("state", "tour_state", "reason", "words"),
    [
        ({"in_catalog": False, "tomb_state": "retired", "tomb_reason": "source_deleted"}, "retired", "source_deleted",
         "no longer offered"),
        ({"in_catalog": False, "tomb_state": "paused", "tomb_reason": "master_inactive"}, "paused", "master_inactive",
         "temporarily unavailable"),
        ({"in_catalog": False}, "not_in_catalog", "not_in_catalog", "not in the AA catalog"),
    ],
)  # fmt: skip
def test_quote_on_a_tour_that_is_not_served_is_409(state: dict, tour_state: str, reason: str, words: str) -> None:
    m2m = LocalM2M()
    conn = served_plan(_conn(), PLAN, KEY, **state)
    resp = _client(conn, m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(lines=[_plan_line()]), headers=_headers(m2m.token())
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "tour_not_served" and body["tour_state"] == tour_state and body["reason"] == reason
    assert body["tour"] == {"source_system": "cis", "source_id": KEY}
    assert body["message"].startswith("line T:") and words in body["message"]
    assert not conn.sql("FROM booking.supplier_rates x")  # refused before any rate is read
    assert not conn.sql("INSERT INTO booking.b2b_quotes")


def test_room_and_service_lines_carry_no_tour_and_are_not_checked() -> None:
    m2m, conn = LocalM2M(), _conn()
    resp = _client(conn, m2m).post("/booking/b2b/v1/quotes", json=_quote_body(), headers=_headers(m2m.token()))
    assert resp.status_code == 201, resp.text
    assert not conn.sql(PLAN_KEYS_FRAGMENT) and not conn.sql(STATES_FRAGMENT)


def test_quote_when_the_catalog_cannot_be_read_is_503() -> None:
    m2m = LocalM2M()
    conn = _conn().on(PLAN_KEYS_FRAGMENT, [{"id": PLAN, "source_id": KEY}])
    conn.on(STATES_FRAGMENT, lambda *_: asyncpg.InsufficientPrivilegeError("permission denied for schema catalog"))
    resp = _client(conn, m2m).post(
        "/booking/b2b/v1/quotes", json=_quote_body(lines=[_plan_line()]), headers=_headers(m2m.token())
    )
    assert resp.status_code == 503 and resp.json()["error"] == "unavailable", resp.text


@pytest.mark.asyncio
async def test_require_served_names_the_first_refused_line_in_request_order() -> None:
    other = "8e000000-0000-4000-8000-0000000000e2"
    conn = ScriptedConn().on(PLAN_KEYS_FRAGMENT, [{"id": PLAN, "source_id": KEY}, {"id": other, "source_id": "k2"}])
    conn.on(STATES_FRAGMENT, [state_row(KEY), state_row("k2", in_catalog=False)])
    lines = [("A", "rt:9f000000-0000-4000-8000-000000000c01"), ("B", f"tp:{PLAN}"), ("C", f"tp:{other}"), ("D", "x")]
    with pytest.raises(B2BError) as exc:
        await served.require_served(conn, lines)
    assert exc.value.error == "tour_not_served" and exc.value.status_code == 409
    assert exc.value.message.startswith("line C:") and exc.value.extra["tour"]["source_id"] == "k2"
    # an unknown plan id is left to the pricing check
    await served.require_served(ScriptedConn(), [("Z", f"tp:{PLAN}")])


# ── Products delta: withdrawn ────────────────────────────────────────────────


def _products(conn: ScriptedConn, m2m: LocalM2M, **params: Any) -> Any:
    return _client(conn, m2m).get(
        "/booking/b2b/v1/products", params=params, headers=_headers(m2m.token("booking.b2b/read"))
    )


def test_delta_lists_retired_tours_as_withdrawn() -> None:
    m2m = LocalM2M()
    conn = _conn().on(VIEW_READY_FRAGMENT, True)
    conn.on(
        "WHERE (p.sellable OR $2::timestamptz IS NOT NULL)",
        [
            {"trip_plan_id": PLAN, "source_system": "cis", "source_id": KEY, "sellable": False,
             "has_fixed_departures": False, "updated_at": WHEN, "withdrawn": True,
             "withdrawn_reason": "source_deleted"},
        ],
    )  # fmt: skip
    resp = _products(conn, m2m, updated_since=(WHEN - timedelta(hours=1)).isoformat())
    assert resp.status_code == 200, resp.text
    [item] = resp.json()["items"]
    assert item["withdrawn"] is True and item["withdrawn_reason"] == "source_deleted"
    assert item["sellable"] is False and item["components"] == []
    [(flat, _)] = [c for c in conn.calls if "WHERE (p.sellable" in c[0]]
    assert "UNION ALL" in flat and "catalog.v_tour_retired" in flat and "r.state = 'retired'" in flat
    # only plans that were offered to agencies are told withdrawn (never-offered plans stay internal)
    assert "(p.sellable OR EXISTS (SELECT 1 FROM booking.trip_plan_components c" in flat.split("UNION ALL")[1]


def test_full_list_and_missing_view_list_no_withdrawn() -> None:
    m2m = LocalM2M()
    full = _conn().on(VIEW_READY_FRAGMENT, True)
    assert _products(full, m2m).status_code == 200
    assert not full.sql(VIEW_READY_FRAGMENT)  # no delta, no detection
    assert all("UNION ALL" not in flat for flat, _ in full.calls)
    liveness.reset_cache()
    missing = _conn().on(VIEW_READY_FRAGMENT, None)
    assert _products(missing, m2m, updated_since=WHEN.isoformat()).status_code == 200
    assert all("UNION ALL" not in flat for flat, _ in missing.calls)
    live = [flat for flat, _ in missing.calls if "WHERE (p.sellable" in flat]
    assert live and "greatest(p.updated_at, t.updated_at" in live[0]  # a restored tour moves updated_at


# ── Lifecycle outbox pull ────────────────────────────────────────────────────


def _event(event: str = "retired", reason: str = "source_deleted", **kw: Any) -> dict[str, Any]:
    return {
        "event_id": kw.get("event_id", 7),
        "idempotency_key": kw.get("key", f"tle:{event}:7"),
        "source_system": "cis",
        "source_id": KEY,
        "event": event,
        "reason": reason,
        "occurred_at": WHEN,
    }


CASE = {"id": "0c000000-0000-4000-8000-000000000001", "code": "TC-000042", "status": "confirmed", "channel": "b2b",
        "via_leg": False, "via_case": False, "via_item": True}  # fmt: skip
HOLD_LIST = '[{"hold_id": "4b000000-0000-4000-8000-000000000001", "expires_at": "2026-10-24T08:00:00+00:00", "agency_name": "Agency A"}]'  # noqa: E501
PLAN_ROW = {"id": PLAN, "sellable": True, "confirmed_holds": 2, "active_holds": 1, "active_hold_list": HOLD_LIST}


def _pull_conn(events: list[dict[str, Any]], *, inserted: Any = "1b000000-0000-4000-8000-000000000001") -> ScriptedConn:
    conn = ScriptedConn().on(VIEW_READY_FRAGMENT, True)
    conn.on("SELECT safe_id FROM booking.inbox_cursor", 0)
    conn.on("FROM catalog.v_tour_lifecycle_outbox o WHERE o.event_id > $1", events)
    conn.on("INSERT INTO booking.inbox_events", inserted)
    conn.on("FROM booking.trip_cases tc WHERE tc.deleted_at IS NULL", [CASE])
    conn.on("FROM booking.trip_plans p WHERE p.source_system = 'cis' AND p.source_id = $1", PLAN_ROW)
    conn.on("UPDATE booking.inbox_cursor", 0)
    return conn


def _notifications(conn: ScriptedConn) -> list[tuple[Any, ...]]:
    return conn.sql("INSERT INTO booking.notifications")


@pytest.mark.asyncio
async def test_pull_is_skipped_until_the_outbox_view_exists() -> None:
    conn = ScriptedConn().on(VIEW_READY_FRAGMENT, None)
    out = await lifecycle.pull(conn, Job("j", lifecycle.KIND, {}, 1, 5))
    assert out["outcome"] == "skipped" and not conn.sql("booking.inbox_cursor")


@pytest.mark.asyncio
async def test_retired_event_notifies_staff_per_case_and_plan_and_changes_nothing() -> None:
    conn = _pull_conn([_event()])
    out = await lifecycle.pull(conn, Job("j", lifecycle.KIND, {}, 1, 5), today=date(2026, 10, 23))
    assert out["applied"] == {"notified": 1} and out["drained"] is True
    rows = _notifications(conn)
    assert [(r[1], r[4], r[5]) for r in rows] == [
        ("tour.retired", "trip_case", CASE["id"]),
        ("tour.retired", "trip_plan", PLAN),
    ]
    case_data = json.loads(rows[0][6])
    assert case_data["source_id"] == KEY and case_data["via"] == ["item"] and case_data["code"] == "TC-000042"
    assert "agency" in rows[0][3]
    # the plan text never says the active holds are safe: they can no longer be confirmed (409) and will expire
    assert "2 confirmed bookings on future departures are kept" in rows[1][3]
    assert "1 active B2B hold can no longer be confirmed and will expire: contact the agency now" in rows[1][3]
    assert "holds" not in rows[1][3].split("kept")[0]
    plan_data = json.loads(rows[1][6])
    assert plan_data["active_hold_list"][0]["agency_name"] == "Agency A"
    [inbox] = conn.sql("INSERT INTO booking.inbox_events")
    assert inbox[:3] == ("cis", "catalog.tour", "tle:retired:7") and json.loads(inbox[3])["event"] == "retired"
    assert conn.sql("SET status = 'processed'")
    # kept: no status move, no cancel, no hold write
    assert not any(k in flat for flat, _ in conn.calls for k in ("UPDATE booking.trip_cases", "b2b_holds SET"))
    [audit] = conn.sql("INSERT INTO booking.audit_logs")
    assert audit[3] == "catalog.tour.retired"


@pytest.mark.parametrize(
    ("confirmed", "active", "has", "lacks"),
    [
        (0, 0, ["0 confirmed bookings on future departures are kept."], ["active B2B hold"]),
        (1, 0, ["1 confirmed booking on future departures is kept."], ["active B2B hold"]),
        (3, 2, ["2 active B2B holds can no longer be confirmed and will expire: contact the agencies now."], []),
    ],
)
def test_plan_retired_message_never_calls_active_holds_safe(
    confirmed: int, active: int, has: list[str], lacks: list[str]
) -> None:
    text = lifecycle.plan_retired_message(KEY, "source_deleted", confirmed, active)
    assert all(h in text for h in has) and not any(word in text for word in lacks)
    assert "holds on future departures are kept" not in text and "Orphaned" in text


def test_hold_list_accepts_json_text_a_list_or_nothing() -> None:
    assert lifecycle._hold_list({"active_hold_list": HOLD_LIST})[0]["agency_name"] == "Agency A"
    assert lifecycle._hold_list({"active_hold_list": [{"hold_id": "h"}]}) == [{"hold_id": "h"}]
    assert lifecycle._hold_list({"active_hold_list": None}) == [] and lifecycle._hold_list({}) == []


@pytest.mark.asyncio
async def test_an_event_already_in_the_inbox_is_not_applied_twice() -> None:
    conn = _pull_conn([_event()], inserted=None)
    out = await lifecycle.pull(conn, Job("j", lifecycle.KIND, {}, 1, 5))
    assert out["applied"] == {} and not _notifications(conn)


@pytest.mark.parametrize("event", ["paused", "superseded"])
@pytest.mark.asyncio
async def test_paused_and_superseded_notify_nobody(event: str) -> None:
    conn = _pull_conn([_event(event, "master_inactive")])
    out = await lifecycle.pull(conn, Job("j", lifecycle.KIND, {}, 1, 5))
    assert out["applied"] == {"ignored": 1} and not _notifications(conn)
    assert conn.sql("SET status = 'processed'")


@pytest.mark.asyncio
async def test_restored_notifies_only_after_a_notified_retirement() -> None:
    never = _pull_conn([_event("restored", "restore")]).on("data->>'source_id' = $2", False)
    assert (await lifecycle.pull(never, Job("j", lifecycle.KIND, {}, 1, 5)))["applied"] == {"ignored": 1}
    told = _pull_conn([_event("restored", "restore")]).on("data->>'source_id' = $2", True)
    await lifecycle.pull(told, Job("j", lifecycle.KIND, {}, 1, 5))
    assert {r[1] for r in _notifications(told)} == {"tour.reinstated"}


@pytest.mark.asyncio
async def test_nothing_affected_writes_no_notification() -> None:
    conn = _pull_conn([_event()]).on("FROM booking.trip_cases tc WHERE tc.deleted_at IS NULL", [])
    conn.on("FROM booking.trip_plans p WHERE p.source_system = 'cis' AND p.source_id = $1", None)
    out = await lifecycle.pull(conn, Job("j", lifecycle.KIND, {}, 1, 5))
    assert out["applied"] == {"nothing_affected": 1} and not _notifications(conn)


@pytest.mark.asyncio
async def test_schedule_queues_only_when_the_outbox_has_news() -> None:
    at = datetime(2026, 10, 23, 8, 3, 30, tzinfo=UTC)
    schedule = lifecycle.PullSchedule(now=lambda: at)
    assert await schedule(ScriptedConn().on(VIEW_READY_FRAGMENT, None)) is None
    liveness.reset_cache()
    quiet = ScriptedConn().on(VIEW_READY_FRAGMENT, True).on("SELECT EXISTS ( SELECT 1 FROM catalog", False)
    assert await schedule(quiet) is None and not quiet.sql("INSERT INTO booking.job")
    busy = ScriptedConn().on(VIEW_READY_FRAGMENT, True).on("SELECT EXISTS ( SELECT 1 FROM catalog", True)
    busy.on("INSERT INTO booking.job", "job-1")
    assert await schedule(busy) == "job-1"
    [args] = busy.sql("INSERT INTO booking.job")
    assert args[0] == lifecycle.KIND and args[2] == "bk_tour_lifecycle_pull:20261023T0803"


# ── Staff reads ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_case_detail_flags_legs_and_lists_tour_alerts() -> None:
    detail = {**case_row(), "assignees": "[]", "inquiry_snapshot": json.dumps({"days": []}), "inbox_event_id": None}
    legs = [
        {"position": 1, "source_tour_id": "t1", "day_from": 1, "day_to": 2, "source_system": "cis", "source_id": KEY},
        {"position": 2, "source_tour_id": "t2", "day_from": 3, "day_to": 4, "source_system": "cis", "source_id": "k2"},
        {"position": 3, "source_tour_id": "t3", "day_from": 5, "day_to": 5, "source_system": None, "source_id": None},
    ]
    conn = (
        ScriptedConn()
        .on("tc.inbox_event_id", detail)
        .on("FROM booking.trip_case_legs", legs)
        .on("FROM booking.trip_case_versions v", [])
        .on("FROM booking.trip_case_status_transitions", transitions("cancelled"))
        .on("FROM booking.trip_case_version_itinerary i", [{"source_id": "k3"}])
        .on(VIEW_READY_FRAGMENT, True)
        .on(
            STATES_FRAGMENT,
            [
                state_row(KEY, in_catalog=False, tomb_state="retired", tomb_reason="source_deleted",
                          tomb_changed_at=WHEN),
                state_row("k2", in_catalog=False, tomb_state="paused", tomb_reason="master_inactive"),
                state_row("k3"),
            ],
        )
    )  # fmt: skip
    case = await service.get_case(conn, "TC-000001")
    assert [leg["catalog"]["state"] if leg["catalog"] else None for leg in case["legs"]] == ["retired", "paused", None]
    [alert] = case["tour_alerts"]  # paused raises no alert (ADR 0015), served none
    assert alert["source_id"] == KEY and alert["reason"] == "source_deleted" and alert["used_by"] == ["leg"]
    [(_, args)] = [c for c in conn.calls if STATES_FRAGMENT in c[0]]
    assert args == ([KEY, "k2", "k3"],)


@pytest.mark.asyncio
async def test_case_detail_without_catalog_shows_no_flags() -> None:
    detail = {**case_row(), "assignees": "[]", "inquiry_snapshot": None, "inbox_event_id": None}
    legs = [
        {"position": 1, "source_tour_id": "t", "day_from": 1, "day_to": 1, "source_system": "cis", "source_id": KEY}
    ]
    conn = ScriptedConn().on("tc.inbox_event_id", detail).on("FROM booking.trip_case_legs", legs)
    conn.on(STATES_FRAGMENT, lambda *_: asyncpg.InvalidSchemaNameError("schema catalog does not exist"))
    case = await service.get_case(conn, "TC-000001")
    assert case["legs"][0]["catalog"] is None and case["tour_alerts"] == []


@pytest.mark.asyncio
async def test_orphaned_plans_carry_reason_and_what_is_attached() -> None:
    row = {
        "source_system": "cis", "source_id": KEY, "updated_at": WHEN, "plan_id": PLAN, "sellable": True,
        "trip_style": None, "default_channel": "b2b", "price_hint_amount": "100.00", "price_hint_currency": "USD",
        "price_hint_basis": "per_person", "open_cases": 1, "future_departures": 3, "confirmed_holds": 2,
        "tomb_state": "retired", "tomb_reason": "master_trashed", "tomb_changed_at": WHEN,
    }  # fmt: skip
    conn = ScriptedConn().on(VIEW_READY_FRAGMENT, True).on("SELECT count(*) FROM booking.trip_plans p", 1)
    conn.on("FROM booking.trip_plans p LEFT JOIN LATERAL", [row])
    out = await trip_plans.list_orphaned(conn, SALES, q=f" {KEY} ")
    [item] = out["items"]
    assert out["total"] == 1 and item["orphaned"] is True and item["title"] is None
    assert item["catalog"]["state"] == "retired" and item["catalog"]["reason"] == "master_trashed"
    assert (item["open_cases"], item["future_departures"], item["confirmed_holds"]) == (1, 3, 2)
    assert "tomb_state" not in item
    [(flat, args)] = [c for c in conn.calls if "LEFT JOIN LATERAL" in c[0]]
    assert "catalog.v_tour_retired" in flat and args[0] == KEY
    # a paused key is no orphan (ADR 0015 decision 5: no staff flag for a pause), in the page and in its total
    paused_out = "pr.state = 'paused'"
    [(total_sql, _)] = [c for c in conn.calls if "SELECT count(*) FROM booking.trip_plans" in c[0]]
    assert paused_out in flat and paused_out in total_sql
    liveness.reset_cache()
    degraded = ScriptedConn().on(VIEW_READY_FRAGMENT, None).on("FROM booking.trip_plans p LEFT JOIN LATERAL", [])
    await trip_plans.list_orphaned(degraded, SALES)
    assert all("v_tour_retired" not in flat for flat, _ in degraded.calls)


@pytest.mark.asyncio
async def test_orphaned_facet_leaves_paused_keys_out_when_the_tombstone_is_readable() -> None:
    conn = ScriptedConn().on(VIEW_READY_FRAGMENT, True).on("FROM booking.trip_plans p WHERE p.source_system", 1)
    assert (await trip_plans.facets(conn))["orphaned"] == 1
    [(flat, _)] = [c for c in conn.calls if "FROM booking.trip_plans p WHERE p.source_system" in c[0]]
    assert "pr.state = 'paused'" in flat
    liveness.reset_cache()
    degraded = ScriptedConn().on(VIEW_READY_FRAGMENT, None)
    await trip_plans.facets(degraded)
    assert all("v_tour_retired" not in flat for flat, _ in degraded.calls)
