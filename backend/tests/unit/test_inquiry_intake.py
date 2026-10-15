"""Branches of the intake handlers around their SQL (behaviour on Postgres: tests/integration)."""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import pytest

from app.inquiry import intake
from app.inquiry.contract import Contact
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from tests import inquiry_samples as samples
from tests.unit.fakes import ScriptedConn

pytestmark = pytest.mark.asyncio

EVENT_ID = "11111111-1111-1111-1111-111111111111"


def process_job(payload: dict[str, Any] | None = None) -> Job:
    return Job("j", intake.PROCESS_KIND, {"inbox_event_id": EVENT_ID} if payload is None else payload, 1, 5)


def event(payload: Any, status: str = "received") -> dict[str, Any]:
    return {"id": EVENT_ID, "status": status, "payload": json.dumps(payload)}


def conn_for(payload: Any, status: str = "received") -> ScriptedConn:
    return ScriptedConn(
        [
            ("FROM booking.inbox_events WHERE id", event(payload, status)),
            ("FROM booking.trip_cases WHERE idempotency_key", None),
            ("INSERT INTO booking.customers", "lead-1"),
            ("INSERT INTO booking.trip_cases", {"id": "case-1", "code": "TC-000001"}),
            ("INSERT INTO booking.notifications", "note-1"),
        ]
    )


def inbox_status(conn: ScriptedConn) -> tuple[Any, ...]:
    (args,) = conn.sql("UPDATE booking.inbox_events SET status")
    return args


# ── process ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload", [{}, {"inbox_event_id": 7}])
async def test_process_needs_an_event_id(payload: dict) -> None:
    with pytest.raises(PermanentError):
        await intake.process(ScriptedConn(), process_job(payload))


async def test_process_unknown_event_is_permanent() -> None:
    with pytest.raises(PermanentError, match="not found"):
        await intake.process(ScriptedConn(), process_job())


@pytest.mark.parametrize("status", ["processed", "parked", "failed"])
async def test_process_is_a_noop_once_the_event_is_settled(status: str) -> None:
    conn = conn_for(samples.v1_1(), status)
    assert await intake.process(conn, process_job()) == {"outcome": "noop", "status": status}
    assert conn.sql("INSERT INTO booking.trip_cases") == []


async def test_process_marks_invalid_payload_failed() -> None:
    conn = conn_for(samples.with_changes(samples.v1_1(), version=3))
    result = await intake.process(conn, process_job())
    assert result["outcome"] == "failed"
    assert inbox_status(conn)[1] == "failed"


async def test_process_parks_legacy_guest_and_contactless() -> None:
    conn = conn_for(samples.legacy_guest())
    assert (await intake.process(conn, process_job()))["reason"] == "customer_null"
    assert inbox_status(conn)[1] == "parked"

    payload = samples.v1_1()
    payload["customer"]["email"] = None
    conn = conn_for(payload)
    assert (await intake.process(conn, process_job()))["reason"] == "no_contact"


async def test_process_existing_trip_case_only_settles_the_event() -> None:
    conn = conn_for(samples.v1_1()).on("FROM booking.trip_cases WHERE idempotency_key", "case-0")
    assert await intake.process(conn, process_job()) == {"outcome": "noop", "trip_case_id": "case-0"}
    assert inbox_status(conn)[1] == "processed"
    assert conn.sql("INSERT INTO booking.customers") == []


async def test_process_opens_a_trip_case_for_a_new_lead() -> None:
    payload = samples.v1_1()
    conn = conn_for(payload)
    result = await intake.process(conn, process_job())
    assert result["outcome"] == "processed"
    assert result == {
        "outcome": "processed",
        "trip_case_id": "case-1",
        "code": "TC-000001",
        "customer_id": "lead-1",
        "lead": True,
        "needs_merge_review": False,
        "legs": 2,
        "legs_without_tour_key": 2,  # the catalog knows neither tour
        "contract_version": "1.1",
    }
    assert [args[1] for args in conn.sql("INSERT INTO booking.trip_case_legs")] == [1, 2]
    (note,) = conn.sql("INSERT INTO booking.notifications")
    assert note[0] == "New inquiry TC-000001" and "review" not in note[1]
    assert len(conn.sql("INSERT INTO booking.audit_logs")) == 1
    assert inbox_status(conn)[1] == "processed"
    # AA-811: one staff e-mail job per notification, deduplicated by its idempotency key.
    (job,) = conn.sql("INSERT INTO booking.job")
    assert job[0] == "bk_notify_email"
    assert json.loads(job[1]) == {"notification_id": "note-1"}
    assert job[2] == "bk_notify_email:note-1"


async def test_process_queues_no_email_job_for_a_settled_event() -> None:
    conn = conn_for(samples.v1_1(), "processed")
    await intake.process(conn, process_job())
    assert conn.sql("INSERT INTO booking.job") == []


async def test_process_flags_a_lead_matching_an_existing_customer() -> None:
    conn = conn_for(samples.v1_1()).on("SELECT id::text FROM booking.customers WHERE deleted_at", "cust-0")
    result = await intake.process(conn, process_job())
    assert result["needs_merge_review"] is True
    (lead,) = conn.sql("INSERT INTO booking.customers")
    assert lead[4:6] == (True, "cust-0")  # needs_merge_review, merge_candidate_id
    (note,) = conn.sql("INSERT INTO booking.notifications")
    assert "review before merging" in note[1]


async def test_process_without_legs_inserts_none() -> None:
    payload = samples.v1_1()
    payload["itinerary"]["legs"] = []
    conn = conn_for(payload)
    assert (await intake.process(conn, process_job()))["legs"] == 0
    assert conn.sql("INSERT INTO booking.trip_case_legs") == []


# ── catalog tour key per leg (AA-830, bk-011) ────────────────────────────────


def leg_rows(conn: ScriptedConn) -> dict[int, tuple[Any, Any]]:
    """position -> (source_system, source_id) of every inserted leg."""
    return {args[1]: (args[5], args[6]) for args in conn.sql("INSERT INTO booking.trip_case_legs")}


async def test_v1_2_legs_keep_the_payload_key_without_a_catalog_read() -> None:
    payload = samples.v1_2(keys=["src-a", "src-b"])
    conn = conn_for(payload)
    result = await intake.process(conn, process_job())
    assert result["contract_version"] == "1.2" and result["legs_without_tour_key"] == 0
    sent = {leg["position"]: ("cis", leg["source_id"]) for leg in payload["itinerary"]["legs"]}
    assert leg_rows(conn) == sent
    assert conn.sql("FROM catalog.v_tour") == []


async def test_v1_1_legs_are_resolved_against_the_catalog_by_either_key_kind() -> None:
    payload = samples.v1_1()
    by_pos = {leg["position"]: leg["source_tour_id"] for leg in payload["itinerary"]["legs"]}
    catalog = [
        {"source_system": "cis", "source_id": "src-1", "tour_id": by_pos[1]},  # leg 1 sent a version tour_id
        {"source_system": "cis", "source_id": by_pos[2], "tour_id": "t-other"},  # leg 2 sent the source_id
    ]
    conn = conn_for(payload).on("FROM catalog.v_tour", catalog)
    result = await intake.process(conn, process_job())
    assert result["legs_without_tour_key"] == 0
    assert leg_rows(conn) == {1: ("cis", "src-1"), 2: ("cis", by_pos[2])}
    (asked,) = conn.sql("FROM catalog.v_tour")
    assert asked == (sorted(by_pos.values()),)


async def test_unresolved_v1_2_leg_is_resolved_by_booking_and_unknown_stays_null() -> None:
    payload = samples.v1_2(keys=[None, "src-b"])
    (first,) = [leg for leg in payload["itinerary"]["legs"] if leg["source_id"] is None]
    catalog = [{"source_system": "cis", "source_id": "src-late", "tour_id": first["source_tour_id"]}]
    conn = conn_for(payload).on("FROM catalog.v_tour", catalog)
    await intake.process(conn, process_job())
    rows = leg_rows(conn)
    assert rows[first["position"]] == ("cis", "src-late")
    assert conn.sql("FROM catalog.v_tour") == [([first["source_tour_id"]],)]


async def test_unreadable_catalog_opens_the_case_with_null_keys() -> None:
    conn = conn_for(samples.v1_1()).on("FROM catalog.v_tour", asyncpg.InsufficientPrivilegeError("no grant"))
    result = await intake.process(conn, process_job())
    assert result["outcome"] == "processed" and result["legs_without_tour_key"] == 2
    assert set(leg_rows(conn).values()) == {(None, None)}


# ── resolve_customer ─────────────────────────────────────────────────────────


def contact(**kw: Any) -> Contact:
    base = dict(
        name="N", email="a@b.test", phone=None, consent=True, consent_at=None, verified=False, external_ref=None
    )
    base.update(kw)
    return Contact(**base)


async def test_verified_contact_links_a_verified_customer() -> None:
    conn = ScriptedConn([("WHERE verified AND deleted_at IS NULL", "cust-v")])
    assert await intake.resolve_customer(conn, contact(verified=True)) == ("cust-v", False, False)
    assert conn.sql("INSERT INTO booking.customers") == []


async def test_verified_contact_without_match_becomes_a_lead() -> None:
    conn = ScriptedConn([("INSERT INTO booking.customers", "lead-9")])
    assert await intake.resolve_customer(conn, contact(verified=True)) == ("lead-9", True, False)
    (args,) = conn.sql("INSERT INTO booking.customers")
    assert args[3] == intake.LEAD_SOURCE


async def test_unverified_contact_never_takes_the_verified_path() -> None:
    conn = ScriptedConn([("WHERE verified AND deleted_at IS NULL", "cust-v"), ("INSERT INTO booking.customers", "l")])
    assert await intake.resolve_customer(conn, contact()) == ("l", True, False)
    assert conn.sql("WHERE verified AND deleted_at IS NULL") == []


# ── pull ─────────────────────────────────────────────────────────────────────


def pull_conn(rows: list[dict[str, Any]], inserted: list[str | None]) -> ScriptedConn:
    ids = iter(inserted)
    return ScriptedConn(
        [
            ("SELECT safe_id FROM booking.inbox_cursor", 0),
            ("FROM tripplanner.v_inquiry_outbox o WHERE o.id > $1", rows),
            ("INSERT INTO booking.inbox_events", lambda *a: next(ids)),
            ("UPDATE booking.inbox_cursor SET safe_id", 42),
            ("INSERT INTO booking.job", "job-x"),
        ]
    )


async def test_pull_inserts_new_rows_and_queues_process_jobs() -> None:
    rows = [
        {"id": 1, "idempotency_key": "k1", "payload": json.dumps(samples.v1_1())},
        {"id": 2, "idempotency_key": "k2", "payload": json.dumps(samples.v1())},
    ]
    conn = pull_conn(rows, ["e1", None])  # k2 raced in elsewhere: ON CONFLICT DO NOTHING
    result = await intake.pull(conn, Job("p", intake.PULL_KIND, {}, 1, 3))
    assert result == {"scanned": 2, "inserted": 1, "drained": True, "safe_id": 42}
    assert [a[4] for a in conn.sql("INSERT INTO booking.inbox_events")] == ["1.1", "1"]
    (job_args,) = conn.sql("INSERT INTO booking.job")
    assert job_args[0] == intake.PROCESS_KIND and job_args[2] == f"{intake.PROCESS_KIND}:e1"


async def test_pull_keeps_the_cursor_until_the_scan_is_drained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake, "PULL_BATCH", 1)
    rows = [{"id": 1, "idempotency_key": "k1", "payload": json.dumps(samples.v1_1())}]
    conn = pull_conn(rows, ["e1"])
    result = await intake.pull(conn, Job("p", intake.PULL_KIND, {}, 1, 3))
    assert result["drained"] is False and result["safe_id"] == 0
    assert conn.sql("UPDATE booking.inbox_cursor SET safe_id") == []


async def test_safe_window_is_twice_the_producer_transaction_bound() -> None:
    assert intake.SAFE_AFTER_SECONDS == 2 * intake.MAX_PRODUCER_TX_SECONDS
    assert set(intake.HANDLERS) == {intake.PULL_KIND, intake.PROCESS_KIND}
    assert all(kind.startswith("bk_") for kind in intake.HANDLERS)
