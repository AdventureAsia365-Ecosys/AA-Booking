"""bk-002 + the inquiry intake jobs on a real PostgreSQL, as role ``booking``.

The TripPlanner side is the CI stub (stub_tripplanner.sql: tp-008 outbox/view + the tp-009 grant).
Jobs are run through the real Worker (claim → handler → complete/fail) one tick at a time.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from app.inquiry import intake
from app.jobs import queue
from app.jobs.worker import Recurring, Worker
from app.notifications import staff_email
from app.settings import Settings
from tests import inquiry_samples as samples

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

INTAKE_TABLES = (
    "booking.notifications, booking.trip_case_legs, booking.trip_cases, booking.inbox_cursor, "
    "booking.inbox_events, booking.job, booking.audit_logs, booking.customer_otps, "
    "booking.customer_sessions, booking.customers"
)


@pytest_asyncio.fixture
async def admin(migrated_admin_dsn: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(migrated_admin_dsn)
    await conn.execute(f"TRUNCATE {INTAKE_TABLES} CASCADE")
    await conn.execute("TRUNCATE tripplanner.outbox RESTART IDENTITY")
    await conn.execute("TRUNCATE catalog.v_tour, catalog.v_tour_day, catalog.v_tour_photo")
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pool(migrated_booking_dsn: str, admin: asyncpg.Connection) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(migrated_booking_dsn, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


async def publish(
    conn: asyncpg.Connection, payload: dict[str, Any], *, age_seconds: int = 0, outbox_id: int | None = None
) -> int:
    """What TripPlanner handoff.py does (one outbox row per hand-off)."""
    return await conn.fetchval(
        """
        INSERT INTO tripplanner.outbox (id, event, schema_version, idempotency_key, aggregate_id, payload, created_at)
        VALUES (coalesce($1, nextval('tripplanner.outbox_id_seq')), 'inquiry.submitted', 1, $2, $3::uuid,
                $4::jsonb, now() - make_interval(secs => $5))
        RETURNING id
        """,
        outbox_id,
        payload["idempotency_key"],
        payload["trip_id"],
        json.dumps(payload),
        age_seconds,
    )


class StubSes:
    """Stands in for the boto3 sesv2 client (AA-811): records every send_email call."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"MessageId": f"msg-{len(self.sent)}"}


EMAIL_SETTINGS = Settings(
    database_url=None,
    environment="test",
    staff_notify_email="staff@example.test",
    ses_from_address="no-reply@lumiguides.it.com",
    ses_configuration_set="aa-email-dev-transactional",
)


async def drain(pool: asyncpg.Pool, *, pull: bool = True, ses: StubSes | None = None) -> None:
    """Queue one pull (if asked) and run due jobs until none is left (the staff e-mail job against a stub SES)."""

    async def provide() -> asyncpg.Pool:
        return pool

    stub = ses or StubSes()
    handlers = {**intake.HANDLERS, staff_email.KIND: staff_email.build_handler(EMAIL_SETTINGS, lambda _r: stub)}
    w = Worker(provide, handlers, maintenance_every=10_000)
    w._ticks = 1  # skip maintenance: tests schedule the pull explicitly
    if pull:
        await queue.enqueue(pool, intake.PULL_KIND, {})
    for _ in range(50):
        if not await w.run_once():
            return
    raise AssertionError("jobs did not drain")


async def trip_cases(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch("SELECT * FROM booking.trip_cases ORDER BY created_at")


async def inbox(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch("SELECT * FROM booking.inbox_events ORDER BY source_position")


# ── Happy path: v1, v1.1, and what TripPlanner sends today ───────────────────


async def test_v1_and_v1_1_payloads_open_one_trip_case_each(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    payloads = [samples.v1(), samples.v1_1(), samples.v1_1_as_tripplanner_sends()]
    for p in payloads:
        await publish(admin, p)

    await drain(pool)

    events = await inbox(admin)
    assert [e["status"] for e in events] == ["processed"] * 3
    assert [e["contract_version"] for e in events] == ["1", "1.1", "1"]
    cases = await trip_cases(admin)
    assert len(cases) == 3
    assert {c["idempotency_key"] for c in cases} == {p["idempotency_key"] for p in payloads}
    for case in cases:
        assert case["status"] == "new_inquiry"
        assert case["source_system"] == "tripplanner"
        assert case["code"].startswith("TC-") and len(case["code"]) == 9
        legs = await admin.fetch(
            "SELECT position, day_from, day_to FROM booking.trip_case_legs WHERE trip_case_id = $1 ORDER BY position",
            case["id"],
        )
        assert [(r["position"], r["day_from"], r["day_to"]) for r in legs] == [(1, 2, 4), (2, 1, 3)]
        snapshot = json.loads(case["inquiry_snapshot"])
        assert set(snapshot) == {"days", "custom_days", "contract_version"}

    customers = await admin.fetch("SELECT * FROM booking.customers")
    assert len(customers) == 3
    assert {(c["verified"], c["source"], c["needs_merge_review"]) for c in customers} == {
        (False, "tripplanner_lead", False)
    }
    v1_customer = next(c for c in customers if c["phone"] == "+84900000001")
    assert v1_customer["external_ref"] == payloads[0]["customer"]["customer_id"]
    assert v1_customer["contact_consent"] is True and v1_customer["contact_consent_at"] is not None

    notes = await admin.fetch("SELECT * FROM booking.notifications")
    assert len(notes) == 3
    assert {(n["recipient_type"], n["recipient_id"], n["type"]) for n in notes} == {
        ("staff", None, "trip_case.new_inquiry")
    }
    assert await admin.fetchval("SELECT count(*) FROM booking.audit_logs WHERE entity_type = 'trip_case'") == 3
    jobs = await admin.fetch("SELECT kind, status FROM booking.job")
    assert (
        sorted((j["kind"], j["status"]) for j in jobs)
        == [("bk_inquiry_process", "succeeded")] * 3
        + [("bk_inquiry_pull", "succeeded")]
        + [("bk_notify_email", "succeeded")] * 3
    )


# ── Idempotency ──────────────────────────────────────────────────────────────


async def test_pull_and_process_are_idempotent(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await publish(admin, samples.v1_1())
    await drain(pool)
    await drain(pool)  # a second pull finds nothing new

    events = await inbox(admin)
    assert len(events) == 1
    # A duplicate process job for the same inbox row (e.g. re-queued by hand) is a no-op.
    await queue.enqueue(pool, intake.PROCESS_KIND, {"inbox_event_id": str(events[0]["id"])})
    await drain(pool, pull=False)
    # Even with the inbox row reset, the trip case key (idempotency_key UNIQUE) prevents a second case.
    await admin.execute("UPDATE booking.inbox_events SET status = 'received'")
    await queue.enqueue(pool, intake.PROCESS_KIND, {"inbox_event_id": str(events[0]["id"])})
    await drain(pool, pull=False)

    assert len(await trip_cases(admin)) == 1
    assert await admin.fetchval("SELECT count(*) FROM booking.trip_case_legs") == 2
    assert await admin.fetchval("SELECT count(*) FROM booking.customers") == 1
    assert await admin.fetchval("SELECT count(*) FROM booking.notifications") == 1
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE kind = 'bk_notify_email'") == 1
    assert (await inbox(admin))[0]["status"] == "processed"


# ── Staff e-mail (AA-811) ────────────────────────────────────────────────────


async def test_new_inquiry_sends_one_staff_email(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    payload = samples.v1_1()
    first_leg = next(leg for leg in payload["itinerary"]["legs"] if leg["position"] == 1)
    second_leg = next(leg for leg in payload["itinerary"]["legs"] if leg["position"] == 2)
    await admin.execute(
        "INSERT INTO catalog.v_tour (source_id, tour_id, source_version, title, country)"
        " VALUES ('src-1', $1::uuid, 1, 'Hanoi and Halong Bay', 'Vietnam')",
        first_leg["source_tour_id"],
    )
    await publish(admin, payload)
    ses = StubSes()
    await drain(pool, ses=ses)
    await drain(pool, ses=ses)  # nothing new: still one e-mail

    (sent,) = ses.sent
    code = (await trip_cases(admin))[0]["code"]
    assert sent["FromEmailAddress"] == "no-reply@lumiguides.it.com"
    assert sent["Destination"] == {"ToAddresses": ["staff@example.test"]}
    assert sent["ConfigurationSetName"] == "aa-email-dev-transactional"
    simple = sent["Content"]["Simple"]
    assert simple["Subject"]["Data"] == f"[Test] New inquiry {code}: Minh Nguyen"
    text = simple["Body"]["Text"]["Data"]
    assert payload["customer"]["email"] in text
    assert "Hanoi and Halong Bay (Vietnam), days 2-4" in text  # catalog title (live read)
    # Not in the catalog and no named snapshot day: the generic title (legs in day order).
    assert text.index(f"1. AA tour {second_leg['source_tour_id']}, days 1-3") < text.index("2. Hanoi and Halong Bay")
    assert f"https://aa-booking-admin.vercel.app/cases/{code}" in text
    result = await admin.fetchval("SELECT result::text FROM booking.job WHERE kind = 'bk_notify_email'")
    assert json.loads(result)["message_id"] == "msg-1"


async def test_pull_queues_one_process_job_per_new_event(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await publish(admin, samples.v1_1())
    await queue.enqueue(pool, intake.PULL_KIND, {})
    job = await queue.claim(pool, "t", [intake.PULL_KIND], 60)
    assert job is not None
    first = await intake.pull(pool, job)
    second = await intake.pull(pool, job)
    assert (first["inserted"], second["inserted"]) == (1, 0)
    assert await admin.fetchval("SELECT count(*) FROM booking.job WHERE kind = 'bk_inquiry_process'") == 1


# ── Cursor (contract rule 4) ─────────────────────────────────────────────────


async def test_late_commit_with_lower_id_is_not_skipped(
    admin: asyncpg.Connection, pool: asyncpg.Pool, migrated_admin_dsn: str
) -> None:
    late = await asyncpg.connect(migrated_admin_dsn)
    try:
        tx = late.transaction()
        await tx.start()
        slow = samples.v1_1()
        slow_id = await publish(late, slow)  # draws the lower id, commits later
        fast_id = await publish(admin, samples.v1_1())
        assert slow_id < fast_id

        await drain(pool)
        assert [e["source_position"] for e in await inbox(admin)] == [fast_id]
        cursor = await admin.fetchval("SELECT safe_id FROM booking.inbox_cursor")
        assert cursor < slow_id  # recent rows never move the cursor

        await tx.commit()
        await drain(pool)
    finally:
        await late.close()
    assert [e["source_position"] for e in await inbox(admin)] == [slow_id, fast_id]
    assert len(await trip_cases(admin)) == 2


async def test_cursor_advances_only_over_old_rows(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    old_id = await publish(admin, samples.v1_1(), age_seconds=intake.SAFE_AFTER_SECONDS + 60)
    recent_id = await publish(admin, samples.v1_1())
    await drain(pool)
    assert await admin.fetchval("SELECT safe_id FROM booking.inbox_cursor") == old_id
    assert {e["source_position"] for e in await inbox(admin)} == {old_id, recent_id}
    # Rows above the cursor are still found through the inbox anti-join (never double-inserted).
    await drain(pool)
    assert len(await inbox(admin)) == 2


# ── Parked and failed events ─────────────────────────────────────────────────


async def test_legacy_null_customer_is_parked_not_a_trip_case(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await publish(admin, samples.legacy_guest())
    await drain(pool)
    (event,) = await inbox(admin)
    assert event["status"] == "parked"
    assert "customer is null" in event["error"]
    assert await trip_cases(admin) == []
    assert await admin.fetchval("SELECT count(*) FROM booking.customers") == 0


async def test_customer_without_email_or_phone_is_parked(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    payload = samples.v1_1()
    payload["customer"]["email"] = None
    await publish(admin, payload)
    await drain(pool)
    (event,) = await inbox(admin)
    assert event["status"] == "parked"
    assert await trip_cases(admin) == []


@pytest.mark.parametrize(
    "change",
    [{"itinerary": {"legs": []}}, {"version": 2}, {"contract": "trip.created"}],
    ids=["missing-days", "unknown-major", "wrong-contract"],
)
async def test_invalid_payload_is_failed_with_reason(
    admin: asyncpg.Connection, pool: asyncpg.Pool, change: dict[str, Any]
) -> None:
    await publish(admin, samples.with_changes(samples.v1_1(), **change))
    await drain(pool)
    (event,) = await inbox(admin)
    assert event["status"] == "failed"
    assert event["error"]
    assert await trip_cases(admin) == []
    assert await admin.fetchval("SELECT status FROM booking.job WHERE kind = 'bk_inquiry_process'") == "succeeded"


# ── Customers: never merge an unverified contact (contract v1.1, O6) ─────────


async def test_unverified_contact_matching_a_customer_is_a_separate_lead(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    email = f"{uuid.uuid4().hex[:8]}@example.test"
    existing = await admin.fetchrow(
        "INSERT INTO booking.customers (name, email, verified, source) VALUES ('Real Owner', $1, true, 'otp')"
        " RETURNING id, updated_at",
        email,
    )
    await publish(admin, samples.v1_1(email))
    await publish(admin, samples.v1_1_as_tripplanner_sends(email))
    await drain(pool)

    rows = await admin.fetch("SELECT * FROM booking.customers WHERE lower(email) = $1 ORDER BY created_at", email)
    assert len(rows) == 3
    owner, *leads = rows
    assert owner["id"] == existing["id"]
    assert owner["name"] == "Real Owner" and owner["updated_at"] == existing["updated_at"]  # untouched
    for lead in leads:
        assert lead["verified"] is False
        assert lead["source"] == "tripplanner_lead"
        assert lead["needs_merge_review"] is True
        assert lead["merge_candidate_id"] == existing["id"]  # the verified match is preferred
    cases = await trip_cases(admin)
    assert {c["customer_id"] for c in cases} == {lead["id"] for lead in leads}
    notes = await admin.fetch("SELECT message, data FROM booking.notifications")
    assert all("review before merging" in n["message"] for n in notes)
    assert all(json.loads(n["data"])["needs_merge_review"] is True for n in notes)


async def test_verified_contact_links_the_verified_customer(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    email = f"{uuid.uuid4().hex[:8]}@example.test"
    existing = await admin.fetchval(
        "INSERT INTO booking.customers (email, verified, source) VALUES ($1, true, 'otp') RETURNING id", email
    )
    await publish(admin, samples.v1_1(email, verified=True))
    await drain(pool)
    (case,) = await trip_cases(admin)
    assert case["customer_id"] == existing
    assert await admin.fetchval("SELECT count(*) FROM booking.customers") == 1


async def test_verified_flag_without_a_verified_customer_still_creates_an_unverified_lead(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    await publish(admin, samples.v1_1(verified=True))
    await drain(pool)
    (customer,) = await admin.fetch("SELECT verified, source FROM booking.customers")
    assert (customer["verified"], customer["source"]) == (False, "tripplanner_lead")


async def test_leads_may_share_an_email_but_verified_customers_may_not(admin: asyncpg.Connection) -> None:
    email = f"{uuid.uuid4().hex[:8]}@example.test"
    for _ in range(2):
        await admin.execute("INSERT INTO booking.customers (email, source) VALUES ($1, 'tripplanner_lead')", email)
    await admin.execute("INSERT INTO booking.customers (email, verified) VALUES ($1, true)", email)
    with pytest.raises(asyncpg.UniqueViolationError):
        await admin.execute("INSERT INTO booking.customers (email, verified) VALUES ($1, true)", email.upper())


# ── Job table and worker ─────────────────────────────────────────────────────


async def test_job_kinds_must_be_bk_prefixed(pool: asyncpg.Pool) -> None:
    assert await queue.enqueue(pool, "bk_probe", {})
    with pytest.raises(asyncpg.CheckViolationError):
        await queue.enqueue(pool, "aab_probe", {})


async def test_recurring_pull_is_a_singleton(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    async def provide() -> asyncpg.Pool:
        return pool

    w = Worker(provide, intake.HANDLERS, recurring=(Recurring(intake.PULL_KIND, 60),))
    await w.maintenance(pool)
    await w.maintenance(pool)
    rows = await admin.fetch(
        "SELECT status, run_after > now() AS later FROM booking.job WHERE kind = 'bk_inquiry_pull'"
    )
    assert [(r["status"], r["later"]) for r in rows] == [("queued", True)]


async def test_reaper_requeues_an_expired_lease_then_fails_without_attempts(
    admin: asyncpg.Connection, pool: asyncpg.Pool
) -> None:
    job_id = await queue.enqueue(pool, "bk_probe", {}, max_attempts=2)
    await admin.execute(
        "UPDATE booking.job SET status = 'running', attempt = 1, locked_by = 'dead',"
        " locked_until = now() - interval '1 second' WHERE id = $1::uuid",
        job_id,
    )
    assert await queue.reap(pool) == {"requeued": 1, "failed": 0}
    await admin.execute(
        "UPDATE booking.job SET status = 'running', attempt = 2, locked_by = 'dead',"
        " locked_until = now() - interval '1 second' WHERE id = $1::uuid",
        job_id,
    )
    assert await queue.reap(pool) == {"requeued": 0, "failed": 1}
    assert await admin.fetchval("SELECT status FROM booking.job WHERE id = $1::uuid", job_id) == "failed"


async def test_pull_without_the_tp009_grant_is_retried(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await admin.execute("REVOKE SELECT ON tripplanner.v_inquiry_outbox FROM booking")
    try:
        await drain(pool)
        row = await admin.fetchrow("SELECT status, attempt, error FROM booking.job WHERE kind = 'bk_inquiry_pull'")
        assert row["status"] == "queued" and row["attempt"] == 1
        assert "InsufficientPrivilege" in row["error"]
    finally:
        await admin.execute("GRANT SELECT ON tripplanner.v_inquiry_outbox TO booking")


async def test_prune_deletes_only_old_finished_runs(admin: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await admin.execute(
        "INSERT INTO booking.job (kind, status, finished_at) VALUES"
        " ('bk_inquiry_pull', 'succeeded', now() - interval '8 days'),"
        " ('bk_inquiry_pull', 'succeeded', now() - interval '1 day'),"
        " ('bk_inquiry_pull', 'failed', now() - interval '8 days')"
    )
    assert await queue.prune(pool, intake.PULL_KIND, 7) == 1
    assert await admin.fetchval("SELECT count(*) FROM booking.job") == 2


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM tripplanner.outbox",
        "INSERT INTO tripplanner.outbox (event, schema_version, idempotency_key, aggregate_id, payload)"
        " VALUES ('inquiry.submitted', 1, 'k', gen_random_uuid(), '{}')",
        "CREATE TABLE booking.app_made (id int)",
    ],
)
async def test_booking_role_reads_only_the_outbox_view(pool: asyncpg.Pool, statement: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await pool.execute(statement)
