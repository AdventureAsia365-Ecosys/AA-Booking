"""bk-012 + the AA-825 retention kinds on a real PostgreSQL, as role ``booking`` (data standards DM-6.6, DM-7).

Jobs run through the real Worker (claim -> handler -> complete/fail), S3 is a stub. Ages are set by the admin
connection (``finished_at`` / ``received_at`` in the past), the way Dev data will look once it is old enough.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.db import Database
from app.jobs import queue
from app.jobs.worker import Worker
from app.main import create_app
from app.retention import jobs, redact
from app.retention.policy import INBOX_KIND, JOB_KIND
from app.settings import Settings
from tests import inquiry_samples as samples
from tests.integration.test_bk002_intake import INTAKE_TABLES, drain, publish
from tests.staff_tokens import LocalCis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ADMIN_ID, SALES_ID = "it-retention-admin", "it-retention-sales"
R = "/booking/retention"


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(f"TRUNCATE {INTAKE_TABLES} CASCADE")
    await conn.execute("TRUNCATE tripplanner.outbox RESTART IDENTITY")
    await conn.execute("TRUNCATE catalog.v_tour, catalog.v_tour_day, catalog.v_tour_photo")
    for staff_id, role in ((ADMIN_ID, "admin"), (SALES_ID, "sales")):
        await conn.execute(
            "INSERT INTO booking.staff_users (cis_user_id, role, active) VALUES ($1, $2, true)"
            " ON CONFLICT (cis_user_id) DO UPDATE SET role = EXCLUDED.role, active = true",
            staff_id,
            role,
        )
    try:
        yield conn
    finally:
        await conn.execute("DELETE FROM booking.notification_reads")
        await conn.execute("DELETE FROM booking.notifications")
        await conn.close()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


TEST_BUCKET = "aa-cis-bronze-test"
# The worker's process environment with the override gate open (Dev sets both in Terraform; CI sets neither flag).
OVERRIDE_ON = {"ENVIRONMENT": "test", jobs.OVERRIDE_FLAG: "true"}


class StubS3:
    def __init__(self, fail: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = fail

    def put_object(self, **kw: Any) -> None:
        if self.fail:
            raise OSError("AccessDenied (stub)")
        assert kw["Bucket"] == TEST_BUCKET
        self.objects[kw["Key"]] = kw["Body"]

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for body in self.objects.values():
            out += [json.loads(line) for line in gzip.decompress(body).decode().splitlines()]
        return out


async def run_jobs(pool: asyncpg.Pool, s3: StubS3, environ: dict[str, str] | None = None) -> None:
    async def provide() -> asyncpg.Pool:
        return pool

    handlers = jobs.build_handlers(lambda: s3, lambda: TEST_BUCKET, environ=OVERRIDE_ON if environ is None else environ)
    w = Worker(provide, handlers, maintenance_every=10_000)
    w._ticks = 1  # no maintenance (no schedule hook here)
    for _ in range(20):
        if not await w.run_once():
            return
    raise AssertionError("jobs did not drain")


async def run(pool: asyncpg.Pool, s3: StubS3, kind: str, **payload: Any) -> asyncpg.Record:
    job_id = await queue.enqueue(pool, kind, payload, max_attempts=jobs.MAX_ATTEMPTS, created_by="test")
    await run_jobs(pool, s3)
    return await pool.fetchrow(
        "SELECT status, result::text AS result, error FROM booking.job WHERE id = $1::uuid", job_id
    )


def result(row: asyncpg.Record) -> dict[str, Any]:
    return json.loads(row["result"])


async def seed_job(admin: asyncpg.Connection, status: str, days: int, kind: str = "bk_inquiry_process") -> str:
    """A job of ``kind`` that finished ``days`` days ago (running/queued rows get no finished_at unless asked)."""
    lease = status == "running"
    return await admin.fetchval(
        """
        INSERT INTO booking.job (kind, status, payload, created_at, finished_at, locked_by, locked_until, error)
        VALUES ($1, $2, '{"inbox_event_id": "x"}'::jsonb, now() - make_interval(days => $3 + 1),
                CASE WHEN $2 IN ('succeeded', 'failed', 'cancelled') THEN now() - make_interval(days => $3) END,
                CASE WHEN $4 THEN 'w1' END, CASE WHEN $4 THEN now() + interval '5 minutes' END,
                CASE WHEN $2 = 'failed' THEN 'boom' END)
        RETURNING id::text
        """,
        kind,
        status,
        days,
        lease,
    )


async def audit_rows(admin: asyncpg.Connection, action: str | None = None) -> list[asyncpg.Record]:
    sql = "SELECT * FROM booking.audit_logs WHERE entity_type = 'retention'"
    if action:
        return await admin.fetch(sql + " AND action = $1 ORDER BY created_at", action)
    return await admin.fetch(sql + " ORDER BY created_at")


# ── bk_retention_job ─────────────────────────────────────────────────────────


async def test_job_retention_dry_run_then_live_archives_every_deleted_job_and_never_touches_active_jobs(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    keep = {
        await seed_job(admin, "succeeded", 29),
        await seed_job(admin, "failed", 60),
        await seed_job(admin, "queued", 400),
        await seed_job(admin, "running", 400),
    }
    # A queued job that once finished (re-queued by hand) keeps its old finished_at: still never a candidate.
    requeued = await seed_job(admin, "failed", 300)
    await admin.execute("UPDATE booking.job SET status = 'queued' WHERE id = $1::uuid", requeued)
    keep.add(requeued)
    old_succeeded = await seed_job(admin, "succeeded", 31)
    old_failed = {await seed_job(admin, "failed", 91), await seed_job(admin, "cancelled", 150)}
    s3 = StubS3()

    dry = await run(pool, s3, JOB_KIND)
    assert dry["status"] == "succeeded"
    res = result(dry)
    assert res["mode"] == "dry_run" and res["reason"] == "manual dry run"
    assert {p["part"]: p["candidates"] for p in res["parts"]} == {"succeeded": 1, "failed": 2}
    assert res["affected_total"] == 0 and s3.objects == {}
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE id = ANY($1::uuid[])", [*old_failed]) == 2
    (dry_audit,) = await audit_rows(admin, "retention.dry_run")
    assert dry_audit["actor_type"] == "system" and dry_audit["entity_label"] == JOB_KIND
    assert json.loads(dry_audit["after"])["candidates_total"] == 3

    live = await run(pool, s3, JOB_KIND, dry_run=False, max_rows=3)
    res = result(live)
    assert (res["mode"], res["affected_total"], res["archived_total"]) == ("live", 3, 3)
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM booking.job WHERE id = ANY($1::uuid[])", [old_succeeded, *old_failed]
        )
        == 0
    )
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE id = ANY($1::uuid[])", list(keep)) == len(keep)
    # Archive: exactly the deleted rows (AA-825 "archive each batch before delete": succeeded too), whole rows, one
    # object per part, batch and UTC month.
    archived = s3.rows()
    assert {r["id"] for r in archived} == {old_succeeded, *old_failed}
    assert {r["status"] for r in archived} == {"succeeded", "failed", "cancelled"} and all(
        "payload" in r for r in archived
    )
    by_part = {p["part"]: p for p in res["parts"]}
    assert (by_part["succeeded"]["archived_rows"], by_part["failed"]["archived_rows"]) == (1, 2)
    failed_months = {r["finished_at"][:7] for r in archived if r["status"] != "succeeded"}  # 'YYYY-MM', UTC
    assert by_part["failed"]["objects"] == len(failed_months) and by_part["succeeded"]["objects"] == 1
    assert by_part["succeeded"]["objects"] + by_part["failed"]["objects"] == len(s3.objects)
    months = {r["finished_at"][:7] for r in archived}
    assert {"/".join(k.split("/")[3:5]) for k in s3.objects} == {m.replace("-", "/") for m in months}
    for part in by_part.values():
        assert all(k.startswith("archive/booking/job/") and f"-{part['part']}-b" in k for k in part["object_keys"])
    (live_audit,) = await audit_rows(admin, "retention.live")
    assert json.loads(live_audit["after"])["affected_total"] == 3

    # The next scheduled run sees the live run (same windows) and runs live itself, here with nothing to do.
    sched = result(await run(pool, s3, JOB_KIND, trigger="schedule"))
    assert (sched["mode"], sched["candidates_total"], sched["previous_live_affected"]) == ("live", 0, 3)


async def test_a_live_run_stops_at_max_rows_oldest_first(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    oldest = await seed_job(admin, "succeeded", 50)
    newer = await seed_job(admin, "succeeded", 40)
    s3 = StubS3()
    res = result(await run(pool, s3, JOB_KIND, dry_run=False, max_rows=1))
    assert (res["candidates_total"], res["affected_total"], res["archived_total"]) == (2, 1, 1)
    remaining = {r["id"] for r in await admin.fetch("SELECT id::text AS id FROM booking.job")}
    assert oldest not in remaining and newer in remaining
    assert [r["id"] for r in s3.rows()] == [oldest]


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_an_archive_failure_deletes_nothing_it_could_not_archive(
    admin: asyncpg.Connection, pool: asyncpg.Pool, status: str
) -> None:
    old = await seed_job(admin, status, 100)
    row = await run(pool, StubS3(fail=True), JOB_KIND, dry_run=False, max_rows=10)
    assert row["status"] == "failed" and "AccessDenied" in row["error"]
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE id = $1::uuid", old) == 1
    (audit_row,) = await audit_rows(admin, "retention.failed")
    after = json.loads(audit_row["after"])
    assert after["affected_total"] == 0 and "AccessDenied" in after["error"]
    assert await audit_rows(admin, "retention.live") == []


async def test_scheduled_runs_stay_dry_until_a_live_run_then_hold_on_growth(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    await admin.execute(
        "INSERT INTO booking.job (kind, status, created_at, finished_at)"
        " SELECT 'bk_inquiry_process', 'succeeded', now() - interval '41 days', now() - interval '40 days'"
        " FROM generate_series(1, $1)",
        jobs.GROWTH_FLOOR_ROWS + 2,
    )
    s3 = StubS3()
    first = result(await run(pool, s3, JOB_KIND, trigger="schedule"))
    assert first["mode"] == "dry_run" and "first run is a DRY RUN" in first["reason"]

    await run(pool, s3, JOB_KIND, dry_run=False, max_rows=1)  # the approved live run (1 row)
    held = result(await run(pool, s3, JOB_KIND, trigger="schedule"))
    assert held["mode"] == "held" and held["candidates_total"] == jobs.GROWTH_FLOOR_ROWS + 1
    admins = {
        r["cis_user_id"]
        for r in await admin.fetch("SELECT cis_user_id FROM booking.staff_users WHERE role = 'admin' AND active")
    }
    assert ADMIN_ID in admins and held["notified_admins"] == len(admins)
    notes = await admin.fetch("SELECT * FROM booking.notifications WHERE type = 'retention.held'")
    assert {n["recipient_ref"] for n in notes} == admins and {n["entity_type"] for n in notes} == {"retention"}
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE kind = 'bk_inquiry_process'") == (
        jobs.GROWTH_FLOOR_ROWS + 1
    )
    assert len(await audit_rows(admin, "retention.held")) == 1


# ── bk_retention_inbox_redact ────────────────────────────────────────────────


async def test_inbox_redaction_removes_pii_keeps_dedupe_and_skips_unprocessed_rows(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    with_note = samples.v1("old-lead@example.test")
    with_note["itinerary"]["custom_days"][0]["note"] = "Linh says call +84900000001"
    old = [with_note, samples.v1_1("old-lead2@example.test"), samples.legacy_guest()]
    old.append(samples.with_changes(samples.v1_1("bad@example.test"), version=2))  # failed
    # Failed rows that do not keep the contract's shape: PII under an unknown key, and a customer that is a string.
    extra = samples.v1_1("extra@example.test")
    extra["whatsapp"] = "wa-lead@example.test"
    extra["customer"]["full_name"] = "Hoa Pham"
    old.append(extra)
    old.append(samples.with_changes(samples.v1_1(), customer="Jane Doe <jane@example.test>"))
    for p in old:
        await publish(admin, p)
    await drain(pool)
    await admin.execute("UPDATE booking.inbox_events SET received_at = now() - interval '91 days'")
    # A failed row whose payload is not an object at all.
    not_object_key = "not-object-key"
    await admin.execute(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, received_at, status, error)"
        " VALUES ('tripplanner', 'inquiry.submitted', $1, $2::jsonb, now() - interval '100 days', 'failed', 'bad')",
        not_object_key,
        json.dumps("call jane@example.test"),
    )
    # A row still 'received' (its process job never ran) and a recent processed row.
    stuck_key = "stuck-key"
    await admin.execute(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, received_at, status)"
        " VALUES ('tripplanner', 'inquiry.submitted', $1, $2::jsonb, now() - interval '120 days', 'received')",
        stuck_key,
        json.dumps(samples.v1_1("stuck@example.test")),
    )
    await publish(admin, samples.v1_1("recent@example.test"))
    await drain(pool)
    before = {r["idempotency_key"]: r for r in await admin.fetch("SELECT * FROM booking.inbox_events")}
    assert {r["status"] for r in before.values()} == {"processed", "parked", "failed", "received"}
    s3 = StubS3()

    dry = result(await run(pool, s3, INBOX_KIND))
    assert (dry["mode"], dry["candidates_total"], dry["skipped_unprocessed"]) == ("dry_run", 7, 1)
    assert await admin.fetchval("SELECT count(*) FROM booking.inbox_events WHERE redacted_at IS NOT NULL") == 0

    live = result(await run(pool, s3, INBOX_KIND, dry_run=False, max_rows=7))
    assert (live["mode"], live["action"], live["affected_total"], live["archived_total"]) == ("live", "redact", 7, 0)
    assert live["parts"][0]["fields_redacted"] == {
        "customer.name": 4,
        "customer.email": 4,
        "customer.phone": 1,
        "customer.*": 1,
        "customer": 1,
        "*": 1,
        "$": 1,
        "itinerary.custom_days[].note": 1,
    }
    assert s3.objects == {}
    after = {r["idempotency_key"]: r for r in await admin.fetch("SELECT * FROM booking.inbox_events")}
    old_keys = {p["idempotency_key"] for p in old}
    assert after[not_object_key]["redacted_at"] is not None and json.loads(after[not_object_key]["payload"]) is None
    for key in old_keys:
        row, was = after[key], before[key]
        assert row["redacted_at"] is not None
        assert (row["id"], row["contract_version"], row["status"], row["source_position"]) == (
            was["id"],
            was["contract_version"],
            was["status"],
            was["source_position"],
        )
        payload = json.loads(row["payload"])
        text = row["payload"]
        assert "example.test" not in text and "+849" not in text
        assert not any(name in text for name in ("Linh", "Minh", "Hoa", "Jane", "whatsapp", "full_name"))
        assert payload["idempotency_key"] == key
        if payload.get("customer"):
            assert payload["customer"]["consent"] is True
    failed_rows = [r for r in after.values() if r["status"] == "failed"]
    assert len(failed_rows) == 4 and all(r["error"] == redact.REDACTED_ERROR for r in failed_rows)
    parked_row = next(r for r in after.values() if r["status"] == "parked")
    assert parked_row["error"] == before[parked_row["idempotency_key"]]["error"]
    for untouched in (stuck_key, next(iter(set(after) - old_keys - {stuck_key, not_object_key}))):
        assert after[untouched]["redacted_at"] is None and after[untouched]["payload"] == before[untouched]["payload"]

    # Dedupe still works: a full re-scan of the outbox inserts nothing and opens no second trip case.
    cases = await admin.fetchval("SELECT count(*) FROM booking.trip_cases")
    await admin.execute("UPDATE booking.inbox_cursor SET safe_id = 0")
    await drain(pool)
    assert await admin.fetchval("SELECT count(*) FROM booking.inbox_events") == len(after)
    assert await admin.fetchval("SELECT count(*) FROM booking.trip_cases") == cases

    # Idempotent: nothing left to redact.
    again = result(await run(pool, s3, INBOX_KIND, dry_run=False, max_rows=10))
    assert (again["candidates_total"], again["affected_total"]) == (0, 0)
    assert [r["action"] for r in await audit_rows(admin)] == ["retention.dry_run", "retention.live", "retention.live"]


# ── Manual override (AA-825 reopened, S251) ──────────────────────────────────


def _older_than(days: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


async def test_override_run_deletes_young_jobs_by_an_explicit_cutoff_and_never_becomes_the_last_live_run(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    """The Dev proof run: rows 8-20 days old (inside the 30 / 90-day windows) go through the real delete + archive path
    under an explicit cutoff; the next scheduled run still knows no live run and stays a DRY RUN."""
    young_succeeded = await seed_job(admin, "succeeded", 12)
    young_failed = {await seed_job(admin, "failed", 15), await seed_job(admin, "cancelled", 20)}
    keep = {
        await seed_job(admin, "succeeded", 5),  # newer than the override cutoff
        await seed_job(admin, "queued", 400),
        await seed_job(admin, "running", 400),
    }
    s3 = StubS3()
    cutoff = _older_than(8)

    policy = result(await run(pool, s3, JOB_KIND))
    assert policy["candidates_total"] == 0  # under the policy windows nothing is old enough yet

    dry = result(await run(pool, s3, JOB_KIND, trigger="manual_override", older_than=cutoff))
    assert (dry["mode"], dry["trigger"], dry["candidates_total"]) == ("dry_run", "manual_override", 3)
    assert dry["override"]["older_than"] == cutoff and dry["override"]["environment"] == "test"
    assert dry["override"]["opt_in"] == jobs.OVERRIDE_FLAG
    assert set(dry["override"]["policy_cutoffs"]) == {"succeeded", "failed"}
    assert {p["cutoff"][:10] for p in dry["parts"]} == {cutoff}
    assert dry["affected_total"] == 0 and s3.objects == {}

    live_row = await run(pool, s3, JOB_KIND, trigger="manual_override", older_than=cutoff, dry_run=False, max_rows=3)
    assert live_row["status"] == "succeeded"
    live = result(live_row)
    assert (live["mode"], live["affected_total"], live["archived_total"]) == ("live", 3, 3)
    gone = [young_succeeded, *young_failed]
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE id = ANY($1::uuid[])", gone) == 0
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE id = ANY($1::uuid[])", list(keep)) == len(keep)
    assert {r["id"] for r in s3.rows()} == set(gone)
    (live_audit,) = await audit_rows(admin, "retention.live")
    assert live_audit["summary"].startswith(f"override older_than {cutoff}: delete 3 rows")
    assert json.loads(live_audit["after"])["trigger"] == "manual_override"

    # Not "the last live run": the scheduled run stays a DRY RUN, and the status shows it as an override.
    assert await jobs.last_live(pool, JOB_KIND) is None
    sched = result(await run(pool, s3, JOB_KIND, trigger="schedule"))
    assert sched["mode"] == "dry_run" and "first run is a DRY RUN" in sched["reason"]
    async with pool.acquire() as conn:
        state = await jobs.status(conn, OVERRIDE_ON)
        closed = await jobs.status(conn, {"ENVIRONMENT": "dev"})
    assert state["override"]["enabled"] is True and closed["override"]["enabled"] is False
    job_state = next(k for k in state["kinds"] if k["kind"] == JOB_KIND)
    assert job_state["scheduled_mode"] == "dry_run" and job_state["last_live"] is None
    assert job_state["last"]["override"]["override"]["older_than"] == cutoff
    assert job_state["override_allowed"] is True and state["override"]["min_age_days"] == jobs.OVERRIDE_MIN_AGE_DAYS


async def test_override_redacts_young_processed_inbox_rows_only(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    for p in (samples.v1_1("override-a@example.test"), samples.v1_1("override-b@example.test")):
        await publish(admin, p)
    await drain(pool)
    await admin.execute("UPDATE booking.inbox_events SET received_at = now() - interval '10 days'")
    await publish(admin, samples.v1_1("override-recent@example.test"))  # received now: newer than the cutoff
    await drain(pool)
    stuck_key = "override-stuck"
    await admin.execute(
        "INSERT INTO booking.inbox_events (source, contract, idempotency_key, payload, received_at, status)"
        " VALUES ('tripplanner', 'inquiry.submitted', $1, $2::jsonb, now() - interval '12 days', 'received')",
        stuck_key,
        json.dumps(samples.v1_1("override-stuck@example.test")),
    )
    s3 = StubS3()
    cutoff = _older_than(7)

    dry = result(await run(pool, s3, INBOX_KIND, trigger="manual_override", older_than=cutoff))
    assert (dry["mode"], dry["candidates_total"], dry["skipped_unprocessed"]) == ("dry_run", 2, 1)

    live = result(
        await run(pool, s3, INBOX_KIND, trigger="manual_override", older_than=cutoff, dry_run=False, max_rows=2)
    )
    assert (live["mode"], live["action"], live["affected_total"], live["archived_total"]) == ("live", "redact", 2, 0)
    rows = {r["idempotency_key"]: r for r in await admin.fetch("SELECT * FROM booking.inbox_events")}
    redacted = [r for r in rows.values() if r["redacted_at"] is not None]
    assert len(redacted) == 2 and all("example.test" not in r["payload"] for r in redacted)
    assert rows[stuck_key]["redacted_at"] is None
    recent = [r for r in rows.values() if "override-recent@example.test" in r["payload"]]
    assert len(recent) == 1 and recent[0]["redacted_at"] is None
    assert await jobs.last_live(pool, INBOX_KIND) is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"older_than": "too-new"}, "too recent"),
        ({"dry_run": False, "max_rows": jobs.OVERRIDE_MAX_ROWS + 1}, "max_rows"),
        ({"dry_run": False}, "max_rows"),
    ],
)
async def test_refused_override_runs_fail_at_once_and_touch_nothing(
    admin: asyncpg.Connection, pool: asyncpg.Pool, payload: dict[str, Any], message: str
) -> None:
    old = await seed_job(admin, "succeeded", 40)
    body = {"trigger": "manual_override", "older_than": _older_than(10), **payload}
    if body["older_than"] == "too-new":
        body["older_than"] = _older_than(jobs.OVERRIDE_MIN_AGE_DAYS - 1)
    row = await run(pool, StubS3(), JOB_KIND, **body)
    assert row["status"] == "failed" and message in row["error"]
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE id = $1::uuid", old) == 1
    assert await audit_rows(admin, "retention.live") == []


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        # Fail closed (review S251): no ENVIRONMENT default, and a known Dev / test environment still needs the opt-in.
        ({}, "ENVIRONMENT is not set"),
        ({"ENVIRONMENT": "", jobs.OVERRIDE_FLAG: "true"}, "ENVIRONMENT is not set"),
        ({"ENVIRONMENT": "prod", jobs.OVERRIDE_FLAG: "true"}, "refused in environment 'prod'"),
        ({"ENVIRONMENT": "test"}, jobs.OVERRIDE_FLAG),
        ({"ENVIRONMENT": "dev", jobs.OVERRIDE_FLAG: "false"}, jobs.OVERRIDE_FLAG),
    ],
)
async def test_override_fails_closed_without_a_known_environment_and_the_opt_in(
    admin: asyncpg.Connection, pool: asyncpg.Pool, environ: dict[str, str], message: str
) -> None:
    old = await seed_job(admin, "succeeded", 40)
    s3 = StubS3()
    job_id = await queue.enqueue(
        pool,
        JOB_KIND,
        {"trigger": "manual_override", "older_than": _older_than(10), "dry_run": False, "max_rows": 5},
        max_attempts=jobs.MAX_ATTEMPTS,
        created_by="test",
    )
    await run_jobs(pool, s3, environ)
    row = await pool.fetchrow("SELECT status, error FROM booking.job WHERE id = $1::uuid", job_id)
    assert row["status"] == "failed" and message in row["error"]
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE id = $1::uuid", old) == 1
    assert s3.objects == {} and await audit_rows(admin, "retention.live") == []


# ── Admin API ────────────────────────────────────────────────────────────────


async def test_admin_api_queues_runs_and_reports_status(
    admin: asyncpg.Connection, pool: asyncpg.Pool, migrated_booking_dsn: str
) -> None:
    await seed_job(admin, "succeeded", 45)
    settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
    db, cis = Database(settings), LocalCis()
    app = create_app(settings, db, cis.verifier())
    as_admin = {"Authorization": f"Bearer {cis.token(sub=ADMIN_ID, cis_role='admin')}"}
    as_sales = {"Authorization": f"Bearer {cis.token(sub=SALES_ID, cis_role='reviewer')}"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://booking") as client:
            assert (await client.get(R, headers=as_sales)).status_code == 403
            assert (await client.post(f"{R}/runs", json={"kind": JOB_KIND}, headers=as_sales)).status_code == 403
            live_without_count = await client.post(
                f"{R}/runs", json={"kind": JOB_KIND, "dry_run": False}, headers=as_admin
            )
            assert live_without_count.status_code == 422
            queued = await client.post(f"{R}/runs", json={"kind": JOB_KIND}, headers=as_admin)
            assert queued.status_code == 202
            body = queued.json()
            assert (body["status"], body["dry_run"], body["max_rows"]) == ("queued", True, None)
            await run_jobs(pool, StubS3())
            detail = (await client.get(f"{R}/runs/{body['job_id']}", headers=as_admin)).json()
            assert detail["status"] == "succeeded" and detail["result"]["mode"] == "dry_run"
            assert detail["result"]["candidates_total"] == 1 and detail["created_by"] == f"staff:{ADMIN_ID}"
            missing = await client.get(f"{R}/runs/00000000-0000-0000-0000-000000000000", headers=as_admin)
            assert missing.status_code == 404
            state = (await client.get(R, headers=as_admin)).json()
    finally:
        await db.close()
    by_kind = {k["kind"]: k for k in state["kinds"]}
    assert set(by_kind) == set(jobs.KINDS)
    job_kind = by_kind[JOB_KIND]
    assert job_kind["scheduled_mode"] == "dry_run" and job_kind["last_live"] is None
    assert job_kind["last"]["dry_run"]["candidates_total"] == 1
    requested = await audit_rows(admin, "retention.requested")
    assert len(requested) == 1 and requested[0]["actor_ref"] == ADMIN_ID
    assert json.loads(requested[0]["after"]) == {"kind": JOB_KIND, "dry_run": True, "max_rows": None}


async def test_admin_api_queues_an_override_run_and_refuses_bad_ones(
    admin: asyncpg.Connection, pool: asyncpg.Pool, migrated_booking_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    young = await seed_job(admin, "succeeded", 10)
    as_admin_token = LocalCis()
    cutoff = _older_than(jobs.OVERRIDE_MIN_AGE_DAYS)

    async def post(body: dict[str, Any]) -> httpx.Response:
        settings = Settings(database_url=migrated_booking_dsn, db_ssl="disable", environment="test", git_sha="ci")
        db = Database(settings)
        app = create_app(settings, db, as_admin_token.verifier())
        headers = {"Authorization": f"Bearer {as_admin_token.token(sub=ADMIN_ID, cis_role='admin')}"}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://booking") as c:
                return await c.post(f"{R}/runs", json=body, headers=headers)
        finally:
            await db.close()

    # The API reads the raw process environment (not Settings.environment): closed until both are set.
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv(jobs.OVERRIDE_FLAG, raising=False)
    unset = await post({"kind": JOB_KIND, "older_than": cutoff})
    assert unset.status_code == 422 and "ENVIRONMENT is not set" in unset.text
    monkeypatch.setenv("ENVIRONMENT", "test")
    no_opt_in = await post({"kind": JOB_KIND, "older_than": cutoff})
    assert no_opt_in.status_code == 422 and jobs.OVERRIDE_FLAG in no_opt_in.text
    monkeypatch.setenv(jobs.OVERRIDE_FLAG, "true")

    too_new = await post({"kind": JOB_KIND, "older_than": _older_than(jobs.OVERRIDE_MIN_AGE_DAYS - 1)})
    assert too_new.status_code == 422 and too_new.json()["detail"] == "retention_override_refused"
    over_cap = await post(
        {"kind": JOB_KIND, "older_than": cutoff, "dry_run": False, "max_rows": jobs.OVERRIDE_MAX_ROWS + 1}
    )
    assert over_cap.status_code == 422
    assert (await post({"kind": JOB_KIND, "older_than": "not-a-date"})).status_code == 422
    # (ENVIRONMENT prod with the flag: unit test of _check_override; the job checks too.)
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE kind = $1", JOB_KIND) == 0

    queued = await post({"kind": JOB_KIND, "older_than": cutoff, "dry_run": False, "max_rows": 1})
    assert queued.status_code == 202
    body = queued.json()
    assert (body["dry_run"], body["max_rows"], body["older_than"]) == (False, 1, cutoff)
    payload = json.loads(
        await admin.fetchval("SELECT payload::text FROM booking.job WHERE id = $1::uuid", body["job_id"])
    )
    assert payload == {"trigger": "manual_override", "older_than": cutoff, "dry_run": False, "max_rows": 1}
    await run_jobs(pool, StubS3())
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE id = $1::uuid", young) == 0
    (requested,) = await audit_rows(admin, "retention.requested")
    assert json.loads(requested["after"])["older_than"] == cutoff
    assert requested["summary"].endswith(f"override older_than {cutoff}")
