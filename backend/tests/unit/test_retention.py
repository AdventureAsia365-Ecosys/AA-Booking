"""AA-825 Booking retention, pure parts: policy windows, inbox redaction, mode decision, archive keys, SQL shape, the
daily schedule hook and the admin request model. The database behaviour is in tests/integration/test_bk012_retention.py.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.auth.staff import PERMISSIONS
from app.jobs.worker import PermanentError
from app.retention import archive, jobs, redact, runner
from app.retention.policy import INBOX_SPEC, JOB_SPEC, KINDS, spec_for
from app.routers.retention import RunRequest
from tests import inquiry_samples as samples

# ── Policy (data standards §7, E3) ───────────────────────────────────────────


def test_windows_match_the_approved_retention_table() -> None:
    assert JOB_SPEC.windows() == {"succeeded": 30, "failed": 90}
    assert [p.archive for p in JOB_SPEC.parts] == [False, True]  # "Failed rows only" are archived
    assert INBOX_SPEC.windows() == {"payload_pii": 90}
    assert INBOX_SPEC.action == "redact" and not any(p.archive for p in INBOX_SPEC.parts)
    assert KINDS == ("bk_retention_job", "bk_retention_inbox_redact")
    with pytest.raises(ValueError, match="unknown retention kind"):
        spec_for("bk_nope")


def test_queued_and_running_jobs_are_never_candidates() -> None:
    wheres = " ".join(p.where for p in JOB_SPEC.parts)
    for status in ("succeeded", "failed", "cancelled"):
        assert f"'{status}'" in wheres
    assert "queued" not in wheres and "running" not in wheres
    # An inbox row not processed yet keeps its payload.
    assert "t.status <> 'received'" in INBOX_SPEC.parts[0].where


def test_kinds_satisfy_the_booking_job_kind_check() -> None:
    import re

    assert all(re.fullmatch(r"bk_[a-z0-9_]+", k) for k in KINDS)


def test_only_booking_admins_run_retention() -> None:
    assert PERMISSIONS["retention.admin"] == frozenset({"admin"})


# ── Redaction (DM-6.6) ───────────────────────────────────────────────────────


def test_redaction_nulls_contact_name_and_notes_and_keeps_the_rest() -> None:
    payload = samples.v1_1("lead@example.test", customer_id="7b0f2a52-3a55-4f3c-9b1e-2c8a3f0d9e11")
    payload["customer"]["phone"] = "+84900000009"
    payload["itinerary"]["custom_days"][0]["note"] = "call me on +84 900 000 009 after 6pm"
    before = json.loads(json.dumps(payload))

    out, paths = redact.redact_payload(payload)

    assert payload == before  # the input is not modified
    assert out["customer"]["name"] is None and out["customer"]["email"] is None and out["customer"]["phone"] is None
    assert out["itinerary"]["custom_days"][0]["note"] is None
    assert sorted(paths) == [
        "customer.email",
        "customer.name",
        "customer.phone",
        "itinerary.custom_days[0].note",
    ]
    # Kept: ids, consent record, idempotency key, itinerary shape.
    for key in ("customer_id", "consent", "consent_at", "verified"):
        assert out["customer"][key] == payload["customer"][key]
    assert out["idempotency_key"] == payload["idempotency_key"]
    assert out["itinerary"]["legs"] == payload["itinerary"]["legs"]
    assert set(out) == set(payload) and set(out["customer"]) == set(payload["customer"])


def test_redaction_of_a_redacted_or_contactless_payload_reports_nothing() -> None:
    out, paths = redact.redact_payload(samples.v1_1("lead@example.test"))
    again, paths_again = redact.redact_payload(out)
    assert paths and paths_again == [] and again == out
    legacy, legacy_paths = redact.redact_payload(samples.legacy_guest())
    assert legacy_paths == [] and legacy["customer"] is None
    for v in ("v1", "v1_2", "v1_1_as_tripplanner_sends"):
        sample = getattr(samples, v)("lead@example.test")
        out, _ = redact.redact_payload(sample)
        # A conforming payload keeps its shape: same keys at every level, PII nulled.
        assert set(out) == set(sample) and set(out["customer"]) == set(sample["customer"])
        assert out["itinerary"]["legs"] == sample["itinerary"]["legs"]
        assert out["itinerary"]["days"] == sample["itinerary"]["days"]


def test_redaction_is_an_allowlist_for_payloads_that_failed_the_contract() -> None:
    """A failed row can hold personal data anywhere; only known non-PII keys survive (review of AA-825)."""
    payload = samples.v1_1("lead@example.test")
    payload["whatsapp"] = "+84900000077"
    payload["contact_backup"] = {"email": "backup@example.test"}
    payload["trip_id"] = {"owner": "owner@example.test"}  # an allowed key with a non-scalar value
    payload["customer"]["full_name"] = "Hoa Pham"
    payload["customer"]["consent_at"] = ["hoa@example.test"]
    payload["itinerary"]["remarks"] = "Hoa, room for 2"
    payload["itinerary"]["days"][0]["guest_email"] = "guest@example.test"
    payload["itinerary"]["legs"].append("leg note from hoa@example.test")
    payload["itinerary"]["custom_days"][0]["note"] = "call Hoa"

    out, paths = redact.redact_payload(payload)

    text = json.dumps(out)
    assert "example.test" not in text and "Hoa" not in text and "+849" not in text
    assert set(out) == {*redact.ENVELOPE_KEYS, "customer", "itinerary"}
    assert out["trip_id"] is None and out["idempotency_key"] == payload["idempotency_key"]
    assert set(out["customer"]) == {*redact.CUSTOMER_KEEP, *redact.CUSTOMER_FIELDS}
    assert out["customer"]["consent_at"] is None and out["customer"]["consent"] is True
    assert set(out["itinerary"]) == {"legs", "days", "custom_days"}
    assert out["itinerary"]["legs"][-1] is None
    assert set(out["itinerary"]["days"][0]) == set(redact.DAY_KEYS)
    assert sorted(paths) == sorted(
        [
            "*",
            "trip_id",
            "customer.name",
            "customer.email",
            "customer.consent_at",
            "customer.*",
            "itinerary.*",
            "itinerary.legs[2]",
            "itinerary.days[0].*",
            "itinerary.custom_days[0].note",
        ]
    )
    # Key names from the payload never reach the report.
    assert not any(k in p for p in paths for k in ("whatsapp", "contact_backup", "full_name", "remarks", "guest"))
    again, paths_again = redact.redact_payload(out)
    assert again == out and paths_again == []


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"customer": "Jane Doe <jane@example.test>"}, "customer"),
        ({"customer": ["jane@example.test"]}, "customer"),
        ({"itinerary": "Jane, jane@example.test"}, "itinerary"),
        ({"itinerary": {"legs": [], "days": [], "custom_days": "jane@example.test"}}, "itinerary.custom_days"),
    ],
)
def test_redaction_nulls_parts_that_are_not_objects(change: dict[str, Any], path: str) -> None:
    out, paths = redact.redact_payload(samples.with_changes(samples.legacy_guest(), **change))
    assert "example.test" not in json.dumps(out) and path in paths


def test_redaction_of_a_payload_that_is_not_an_object() -> None:
    assert redact.redact_payload("call jane@example.test") == (None, ["$"])
    assert redact.redact_payload(["jane@example.test"]) == (None, ["$"])
    assert redact.redact_payload(None) == (None, [])


def test_only_a_failed_rows_error_is_redacted() -> None:
    assert redact.redact_error("failed", "inquiry.submitted v1.1 invalid: 'x@y' is not ...") == redact.REDACTED_ERROR
    assert redact.redact_error("failed", None) is None
    parked = "customer has neither email nor phone"
    assert redact.redact_error("parked", parked) == parked


# ── Mode (DM-7.2) ────────────────────────────────────────────────────────────

WINDOWS = {"succeeded": 30, "failed": 90}


def test_manual_runs_are_dry_unless_explicitly_live_with_max_rows() -> None:
    assert jobs.decide_mode({}, WINDOWS, 10, None)[:2] == ("dry_run", 0)
    assert jobs.decide_mode({"dry_run": True, "max_rows": 5}, WINDOWS, 10, None)[:2] == ("dry_run", 0)
    assert jobs.decide_mode({"dry_run": False, "max_rows": 5}, WINDOWS, 10, None)[:2] == ("live", 5)
    assert jobs.decide_mode({"dry_run": False, "max_rows": 10**9}, WINDOWS, 10, None)[1] == jobs.RUN_MAX_ROWS
    for bad in ({"dry_run": False}, {"dry_run": False, "max_rows": 0}, {"dry_run": False, "max_rows": True}):
        with pytest.raises(PermanentError):
            jobs.decide_mode(bad, WINDOWS, 10, None)
    # Anything but the literal false stays a DRY RUN.
    assert jobs.decide_mode({"dry_run": "false", "max_rows": 5}, WINDOWS, 10, None)[0] == "dry_run"


def test_scheduled_runs_are_dry_until_a_live_run_with_the_same_windows() -> None:
    sched = {"trigger": "schedule"}
    assert jobs.decide_mode(sched, WINDOWS, 3, None)[0] == "dry_run"
    changed = {"windows": {"succeeded": 14, "failed": 90}, "affected_total": 3}
    assert jobs.decide_mode(sched, WINDOWS, 3, changed)[0] == "dry_run"
    prev = {"windows": WINDOWS, "affected_total": 3}
    assert jobs.decide_mode(sched, WINDOWS, 3, prev)[:2] == ("live", 3)
    assert jobs.decide_mode(sched, WINDOWS, jobs.GROWTH_FLOOR_ROWS, prev)[0] == "live"  # floor
    mode, limit, reason = jobs.decide_mode(sched, WINDOWS, jobs.GROWTH_FLOOR_ROWS + 1, prev)
    assert (mode, limit) == ("held", 0) and "needs a manual live run" in reason
    big = {"windows": WINDOWS, "affected_total": 4_000}
    assert jobs.decide_mode(sched, WINDOWS, 8_000, big)[0] == "live"
    assert jobs.decide_mode(sched, WINDOWS, 8_001, big)[0] == "held"


def test_summary_lines() -> None:
    parts = [{"part": "succeeded", "candidates": 4, "affected": 4}, {"part": "failed", "candidates": 2, "affected": 2}]
    base = {"action": "delete", "parts": parts, "candidates_total": 6, "affected_total": 6, "archived_total": 2}
    dry = "dry_run: 6 candidates (succeeded 4, failed 2); nothing written"
    assert jobs.summary_line({**base, "mode": "dry_run"}) == dry
    assert jobs.summary_line({**base, "mode": "live"}) == "delete 6 rows (succeeded 4, failed 2), archived 2"
    assert jobs.summary_line({**base, "mode": "failed", "error": "boom"}) == "failed after 6 rows: boom"


# ── Archive (DM-7.3) ─────────────────────────────────────────────────────────


def test_archive_key_is_partitioned_by_table_and_utc_month_under_the_booking_prefix() -> None:
    key = archive.object_key("job", datetime(2026, 7, 31, 23, 30, tzinfo=UTC), "j1", "run1", "failed", 3)
    assert key == "archive/booking/job/2026/07/j1-run1-failed-b00003.jsonl.gz"


@pytest.mark.asyncio
async def test_archiver_writes_gzipped_json_lines_encrypted() -> None:
    class StubS3:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def put_object(self, **kw: Any) -> None:
            self.calls.append(kw)

    s3 = StubS3()
    key = await archive.S3Archiver(s3, "job-1", run="r").put(
        "job", "failed", 1, datetime(2026, 1, 2, tzinfo=UTC), ['{"a": 1}', '{"a": 2}']
    )
    (call,) = s3.calls
    assert call["Key"] == key and call["Bucket"] == "aa-cis-bronze-005097885195"
    assert call["ServerSideEncryption"] == "AES256" and call["Metadata"]["rows"] == "2"
    lines = gzip.decompress(call["Body"]).decode().splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"a": 2}]


def test_same_month_prefix_stops_at_the_month_boundary() -> None:
    rows = [
        {"ts": datetime(2026, 6, 30, 23, tzinfo=UTC)},
        {"ts": datetime(2026, 6, 30, 23, 59, tzinfo=UTC)},
        {"ts": datetime(2026, 7, 1, tzinfo=UTC)},
    ]
    assert runner.same_month_prefix(rows) == rows[:2]
    assert runner.same_month_prefix([]) == []


# ── SQL shape ────────────────────────────────────────────────────────────────


def test_batches_lock_skip_and_use_the_keyset() -> None:
    succeeded, failed = JOB_SPEC.parts
    refs = [("booking.job", "parent_job_id")]
    sql = runner.batch_sql(JOB_SPEC, failed, refs, after=True)
    assert "FOR UPDATE OF t SKIP LOCKED" in sql
    assert "(t.finished_at, t.id) > ($3, $4::uuid)" in sql and "ORDER BY t.finished_at, t.id" in sql
    assert "to_jsonb(t)::text AS doc" in sql  # archived part carries the whole row
    assert "to_jsonb" not in runner.batch_sql(JOB_SPEC, succeeded, refs, after=False)
    assert 'NOT EXISTS (SELECT 1 FROM booking.job r0 WHERE r0."parent_job_id" = t.id)' in sql
    redact_sql = runner.batch_sql(INBOX_SPEC, INBOX_SPEC.parts[0], refs, after=False)
    assert "NOT EXISTS" not in redact_sql and "t.payload::text AS payload" in redact_sql


# ── Daily schedule ───────────────────────────────────────────────────────────


class FakePool:
    def __init__(self, conflict: bool = False) -> None:
        self.inserts: list[tuple[Any, ...]] = []
        self.conflict = conflict

    async def fetchval(self, sql: str, *args: Any) -> str | None:
        assert "INSERT INTO booking.job" in sql
        self.inserts.append(args)
        return None if self.conflict else f"job-{len(self.inserts)}"


@pytest.mark.asyncio
async def test_daily_schedule_queues_each_kind_once_per_utc_day_after_the_hour() -> None:
    clock = {"now": datetime(2026, 10, 16, jobs.SCHEDULE_UTC_HOUR - 1, 59, tzinfo=UTC)}
    hook = jobs.DailySchedule(now=lambda: clock["now"])
    pool = FakePool()

    assert await hook(pool) == [] and pool.inserts == []  # before the hour
    clock["now"] = datetime(2026, 10, 16, jobs.SCHEDULE_UTC_HOUR, 0, tzinfo=UTC)
    assert await hook(pool) == ["job-1", "job-2"]
    kinds = [a[0] for a in pool.inserts]
    payloads = [json.loads(a[1]) for a in pool.inserts]
    keys = [a[2] for a in pool.inserts]
    assert kinds == list(KINDS) and payloads == [{"trigger": "schedule"}] * 2
    assert keys == [jobs.schedule_key(k, date(2026, 10, 16)) for k in KINDS]
    assert [a[3] for a in pool.inserts] == [jobs.MAX_ATTEMPTS] * 2  # never retried (no progress column yet)
    assert await hook(pool) == [] and len(pool.inserts) == 2  # same day: no further INSERT
    clock["now"] = datetime(2026, 10, 17, jobs.SCHEDULE_UTC_HOUR + 1, tzinfo=UTC)
    await hook(pool)
    assert len(pool.inserts) == 4


@pytest.mark.asyncio
async def test_a_second_worker_finds_the_days_jobs_already_queued() -> None:
    pool = FakePool(conflict=True)
    hook = jobs.DailySchedule(now=lambda: datetime(2026, 10, 16, 23, tzinfo=UTC))
    assert await hook(pool) == [] and len(pool.inserts) == 2


# ── Admin request model ──────────────────────────────────────────────────────


def test_run_request_defaults_to_a_dry_run_and_a_live_run_needs_max_rows() -> None:
    assert RunRequest(kind="bk_retention_job").dry_run is True
    assert RunRequest(kind="bk_retention_inbox_redact", dry_run=False, max_rows=12).max_rows == 12
    for bad in (
        {"kind": "bk_retention_job", "dry_run": False},
        {"kind": "bk_retention_job", "dry_run": False, "max_rows": 0},
        {"kind": "bk_retention_job", "max_rows": jobs.RUN_MAX_ROWS + 1},
        {"kind": "bk_inquiry_pull"},
        {"kind": "bk_retention_job", "extra": 1},
    ):
        with pytest.raises(ValidationError):
            RunRequest(**bad)
    literal = RunRequest.model_fields["kind"].annotation.__args__
    assert set(literal) == set(KINDS)
