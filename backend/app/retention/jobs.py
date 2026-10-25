"""AA-825 — the Booking retention job kinds ``bk_retention_job`` and ``bk_retention_inbox_redact`` (policies in
``app.retention.policy``), their daily schedule and the admin status. Same mode rules as CIS
``services/jobs/retention_job.py`` (DM-7.2):

Payload: ``{"trigger": "schedule" | "manual" | "manual_override", "dry_run": bool, "max_rows": int, "batch_size": int,
"pause_ms": int, "older_than": "YYYY-MM-DD" (manual_override only)}``

- manual, ``dry_run`` absent or true -> DRY RUN: counts only, nothing written but the job result and one audit row.
- manual, ``dry_run`` false -> live: deletes / redacts at most ``max_rows`` (required: the count Nghiệp approved from
  the DRY RUN), oldest first.
- schedule -> DRY RUN until a live run of this kind with the same windows has succeeded; then live, unless the
  candidates exceed 2x the last live run's count (floor :data:`GROWTH_FLOOR_ROWS`): then HELD, nothing written, one
  notification per active Booking admin.
- manual_override -> an admin-only proof run with an explicit cutoff ``older_than`` (a UTC date) in place of every
  part's window, so the archive / delete / redaction path can be exercised on Dev before rows reach 30 / 90 days
  (AA-825 reopened, S251; CIS has the same trigger since S239). Fails closed (:func:`override_gate`): only when the
  raw process environment has ``ENVIRONMENT`` in :data:`OVERRIDE_ENVIRONMENTS` **and** the explicit opt-in
  :data:`OVERRIDE_FLAG` ``=true`` (set only on the Dev task definition by Terraform); an unset or empty value of either
  refuses. Cutoff at least :data:`OVERRIDE_MIN_AGE_DAYS` days old, DRY RUN unless ``dry_run`` is false, and a live run
  needs ``1 <= max_rows <=`` :data:`OVERRIDE_MAX_ROWS` (refused, not capped). It never counts as "the last live run":
  scheduled runs stay DRY RUN and the 2x rule ignores it.

Every run is capped at :data:`RUN_MAX_ROWS`; the rest waits for the next run. Every run (dry_run, live, held, and a live
run that failed half-way) writes one ``booking.audit_logs`` row (``entity_type = 'retention'``, ``entity_id`` = the job
id, ``entity_label`` = the kind, ``after`` = the result). The last live run is read back from the audit log, not from
``booking.job``, because ``bk_retention_job`` itself deletes old succeeded jobs.

Retention jobs are queued with ``max_attempts = 1`` and are not released back to the queue on shutdown
(:data:`NOT_RELEASED`, AA-751): they keep no progress checkpoint, so a second run could not know what an interrupted
one already deleted and might exceed the approved ``max_rows``. A failed run is reported (job ``error`` + audit row)
and the next run starts from a fresh count; a run stopped by a shutdown is failed at once (no audit row for the
batches it committed: the archive objects in S3 and the missing rows are the trace). A session advisory lock per kind
keeps two workers (rolling deploy) from running the same kind at once.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import boto3

from app import audit
from app.jobs import queue
from app.jobs.queue import Job
from app.jobs.worker import PermanentError
from app.notifications.service import notify_staff
from app.retention import runner
from app.retention.archive import S3Archiver
from app.retention.policy import (
    ARCHIVE_BUCKET_DISPLAY,
    ARCHIVE_PREFIX,
    ARCHIVE_REGION,
    KINDS,
    SPECS,
    Spec,
    archive_bucket,
)

log = logging.getLogger(__name__)

RUN_MAX_ROWS = 50_000
GROWTH_FACTOR = 2
GROWTH_FLOOR_ROWS = runner.BATCH_SIZE_MAX
DEFAULT_PAUSE_MS = 200
MAX_ATTEMPTS = 1
# Kinds the worker fails instead of releasing on shutdown (app.jobs.worker no_release): see the module docstring.
NOT_RELEASED: frozenset[str] = frozenset(KINDS)
# The daily scheduled runs are queued on the first worker maintenance pass at or after this UTC hour (02:00 ICT).
SCHEDULE_UTC_HOUR = 19
SCHEDULER = "scheduler:bk-retention"
AUDIT_ENTITY = "retention"
AUDIT_ACTOR = "bk-retention"
HELD_TYPE = "retention.held"

# ── manual override (AA-825 reopened, S251: one real Booking run on Dev without waiting for rows to age) ──
OVERRIDE_TRIGGER = "manual_override"
OVERRIDE_KINDS: tuple[str, ...] = KINDS
# The cutoff is at least a week old: Booking's Dev rows date from S235 (14/10/2026), so the CIS floor (21 days) would
# select nothing yet. A week is far past any in-flight work (an intake job finishes in seconds, the redaction skips
# rows still 'received') and matches queue.prune's 7-day history for the recurring pull kind.
OVERRIDE_MIN_AGE_DAYS = 7
OVERRIDE_MAX_ROWS = 1_000
# The override gate fails closed (review S251): a future prod also runs on acc2 with the same bronze bucket and role
# shape, and ``Settings.environment`` defaults to "dev" when ENVIRONMENT is unset, so neither the account nor that
# default may open it. Both must hold in the RAW process environment (no default):
# - ENVIRONMENT is "dev" (Terraform var.environment on the one ECS deployment), "test" or "local" (CI, a laptop);
# - OVERRIDE_FLAG is "true", an explicit opt-in that only the Dev Booking task definition sets (AA-CIS-Infra
#   accounts/aa365/booking.tf). Unset, empty or any other value refuses.
OVERRIDE_ENVIRONMENTS = frozenset({"dev", "test", "local"})
OVERRIDE_FLAG = "RETENTION_OVERRIDE_ENABLED"

S3Factory = Callable[[], Any]
BucketFactory = Callable[[], str]
# The process environment the override gate reads (``os.environ`` in the app; a dict in tests).
Environ = Mapping[str, str]


def default_s3() -> Any:
    return boto3.client("s3", region_name=ARCHIVE_REGION)


@functools.cache
def default_bucket() -> str:
    """The archive bucket of the account this task runs in (AA-902: no account ID in this public repo).

    STS ``GetCallerIdentity`` needs no IAM permission. Called only when a live run archives rows; a failure raises
    before anything is deleted (the archiver is built before the first batch) and is not cached."""
    sts = boto3.client("sts", region_name=ARCHIVE_REGION)
    return archive_bucket(sts.get_caller_identity()["Account"])


# ── Mode ──────────────────────────────────────────────────────────────────────


class OverrideRefused(ValueError):
    """A manual_override request in a shape or environment that is refused (the job fails at once, the API answers
    422)."""


def override_gate(environ: Environ) -> str:
    """The environment name when this process may run a manual override; raises :class:`OverrideRefused` otherwise.
    Reads the raw ``environ`` with no default, so a missing ENVIRONMENT or opt-in flag refuses (fail closed). Pure."""
    env = (environ.get("ENVIRONMENT") or "").strip().lower()
    if not env:
        raise OverrideRefused("a manual override run is refused: ENVIRONMENT is not set")
    if env not in OVERRIDE_ENVIRONMENTS:
        raise OverrideRefused(f"a manual override run is refused in environment {env!r}")
    if (environ.get(OVERRIDE_FLAG) or "").strip().lower() != "true":
        raise OverrideRefused(f"a manual override run is refused: {OVERRIDE_FLAG} is not 'true' in this deployment")
    return env


def override_enabled(environ: Environ) -> bool:
    try:
        override_gate(environ)
    except OverrideRefused:
        return False
    return True


def override_cutoff(kind: str, payload: dict[str, Any], today: date, environ: Environ) -> datetime | None:
    """The explicit cutoff of a manual_override run (UTC midnight of ``older_than``), None for any other trigger.
    Raises :class:`OverrideRefused` for every refused shape and when :func:`override_gate` refuses. Pure."""
    trigger = payload.get("trigger", "manual")
    older_than = payload.get("older_than")
    if trigger != OVERRIDE_TRIGGER:
        if older_than is not None:
            raise OverrideRefused(f"older_than is only accepted with trigger {OVERRIDE_TRIGGER!r}, not {trigger!r}")
        return None
    override_gate(environ)
    if kind not in OVERRIDE_KINDS:
        raise OverrideRefused(f"{kind} does not accept a manual override; only {list(OVERRIDE_KINDS)}")
    if not isinstance(older_than, str) or len(older_than) != 10:
        raise OverrideRefused("a manual override needs older_than as a UTC date YYYY-MM-DD")
    try:
        day = date.fromisoformat(older_than)
    except ValueError:
        raise OverrideRefused(f"older_than {older_than!r} is not a date YYYY-MM-DD") from None
    latest = today - timedelta(days=OVERRIDE_MIN_AGE_DAYS)
    if day > latest:
        raise OverrideRefused(
            f"older_than {older_than} is too recent: at most {latest.isoformat()} "
            f"({OVERRIDE_MIN_AGE_DAYS} days before today, UTC)"
        )
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def override_max_rows(payload: dict[str, Any]) -> int:
    """The approved row count of a live manual_override run; refused (not capped) outside 1..OVERRIDE_MAX_ROWS."""
    max_rows = payload.get("max_rows")
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or not 1 <= max_rows <= OVERRIDE_MAX_ROWS:
        raise OverrideRefused(f"a live manual override needs 1 <= max_rows <= {OVERRIDE_MAX_ROWS}")
    return max_rows


def decide_mode(
    payload: dict[str, Any], windows: dict[str, int], candidates: int, prev_live: dict[str, Any] | None
) -> tuple[str, int, str]:
    """-> (mode, limit_rows, reason); mode is 'dry_run', 'live' or 'held'. Pure."""
    trigger = payload.get("trigger", "manual")
    if trigger == OVERRIDE_TRIGGER:
        if payload.get("dry_run", True) is not False:
            return "dry_run", 0, "manual override dry run (explicit cutoff)"
        try:
            max_rows = override_max_rows(payload)
        except OverrideRefused as exc:
            raise PermanentError(str(exc)) from None
        return "live", max_rows, "manual override live run (explicit cutoff), approved max_rows"
    if trigger != "schedule":
        if payload.get("dry_run", True) is not False:
            return "dry_run", 0, "manual dry run"
        max_rows = payload.get("max_rows")
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
            raise PermanentError("a live manual run needs max_rows (the approved count from a DRY RUN)")
        return "live", min(max_rows, RUN_MAX_ROWS), "manual live run, approved max_rows"
    if prev_live is None:
        return "dry_run", 0, "no approved live run of this kind yet (first run is a DRY RUN)"
    if (prev_live.get("windows") or {}) != windows:
        return "dry_run", 0, "windows changed since the last live run (DRY RUN again)"
    prev = int(prev_live.get("affected_total") or 0)
    allowed = max(GROWTH_FACTOR * prev, GROWTH_FLOOR_ROWS)
    if candidates > allowed:
        return (
            "held",
            0,
            f"{candidates} candidates > {GROWTH_FACTOR}x the last live run ({prev}, floor {GROWTH_FLOOR_ROWS}); "
            "needs a manual live run",
        )
    return "live", min(candidates, RUN_MAX_ROWS), "scheduled live run within the growth limit"


# ── Audit log (the record of every run) ───────────────────────────────────────


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# A manual_override run is never "the last live run" (its cutoff is not the policy's).
_LAST_LIVE_SQL = """
    SELECT after::text AS after FROM booking.audit_logs
     WHERE entity_type = 'retention' AND action = 'retention.live' AND entity_label = $1
       AND coalesce(after ->> 'trigger', 'manual') <> 'manual_override'
     ORDER BY created_at DESC
     LIMIT 1
"""


async def last_live(conn: asyncpg.Connection | asyncpg.Pool, kind: str) -> dict[str, Any] | None:
    raw = await conn.fetchval(_LAST_LIVE_SQL, kind)
    return _loads(raw) if raw else None


def summary_line(result: dict[str, Any]) -> str:
    key = "affected" if result["mode"] == "live" else "candidates"
    parts = ", ".join(f"{p['part']} {p[key]}" for p in result["parts"])
    override = result.get("override")
    prefix = f"override older_than {override['older_than']}: " if override else ""
    if result["mode"] == "live":
        text = f"{prefix}{result['action']} {result['affected_total']} rows ({parts})"
        if result.get("archived_total"):
            text += f", archived {result['archived_total']}"
        return text
    if result["mode"] == "failed":
        return f"{prefix}failed after {result['affected_total']} rows: {result.get('error', '')}"
    return f"{prefix}{result['mode']}: {result['candidates_total']} candidates ({parts}); nothing written"


async def _audit(conn: asyncpg.Connection, job: Job, result: dict[str, Any]) -> None:
    await audit.record(
        conn,
        actor_type="system",
        actor_ref=AUDIT_ACTOR,
        action=f"retention.{result['mode']}",
        entity_type=AUDIT_ENTITY,
        entity_id=job.id,
        after=result,
        entity_label=result["kind"],
        summary=summary_line(result),
    )


async def _notify_held(conn: asyncpg.Connection, job: Job, result: dict[str, Any]) -> int:
    """One notification per active Booking admin (the shared staff queue would reach sales too)."""
    admins = await conn.fetch("SELECT cis_user_id FROM booking.staff_users WHERE role = 'admin' AND active")
    for a in admins:
        await notify_staff(
            conn,
            type=HELD_TYPE,
            title=f"Retention held: {result['table']}",
            message=result["reason"],
            entity_type=AUDIT_ENTITY,
            entity_id=job.id,
            data={"kind": result["kind"], "candidates": result["candidates_total"]},
            recipient_ref=a["cis_user_id"],
        )
    return len(admins)


# ── Run ───────────────────────────────────────────────────────────────────────

_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtextextended($1, 0))"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended($1, 0))"


async def run_spec(
    pool: asyncpg.Pool,
    job: Job,
    spec: Spec,
    *,
    s3_factory: S3Factory = default_s3,
    bucket_factory: BucketFactory = default_bucket,
    environ: Environ | None = None,
) -> dict[str, Any]:
    payload = job.payload or {}
    trigger = payload.get("trigger", "manual")
    pause_s = max(0, int(payload.get("pause_ms", DEFAULT_PAUSE_MS))) / 1000.0
    batch_size = int(payload.get("batch_size") or runner.BATCH_SIZE_MAX)
    async with pool.acquire() as conn:
        if not await conn.fetchval(_LOCK_SQL, spec.kind):
            raise PermanentError(f"another {spec.kind} run holds the lock; this one did nothing")
        try:
            return await _run_locked(
                conn,
                job,
                spec,
                payload,
                trigger,
                pause_s,
                batch_size,
                s3_factory,
                bucket_factory,
                environ if environ is not None else os.environ,
            )
        finally:
            await conn.fetchval(_UNLOCK_SQL, spec.kind)


async def _run_locked(
    conn: asyncpg.Connection,
    job: Job,
    spec: Spec,
    payload: dict[str, Any],
    trigger: str,
    pause_s: float,
    batch_size: int,
    s3_factory: S3Factory,
    bucket_factory: BucketFactory,
    environ: Environ,
) -> dict[str, Any]:
    windows = spec.windows()
    today = await conn.fetchval("SELECT (now() AT TIME ZONE 'UTC')::date")
    try:
        forced = override_cutoff(spec.kind, payload, today, environ)
    except OverrideRefused as exc:
        raise PermanentError(str(exc)) from None
    refs = await runner.fk_refs(conn, spec.table) if spec.action == "delete" else []
    cuts, policy_cuts, parts = {}, {}, []
    for part in spec.parts:
        policy_cuts[part.name] = await runner.cutoff(conn, part.window_days)
        cuts[part.name] = forced or policy_cuts[part.name]
        parts.append(await runner.count_part(conn, spec, part, refs, cuts[part.name]))
    candidates = sum(p.candidates for p in parts)
    prev = await last_live(conn, spec.kind)
    mode, limit_rows, reason = decide_mode(payload, windows, candidates, prev)
    result: dict[str, Any] = {
        "kind": spec.kind,
        "table": spec.table,
        "action": spec.action,
        "mode": mode,
        "reason": reason,
        "trigger": trigger,
        "windows": windows,
        "candidates_total": candidates,
        "limit_rows": limit_rows,
        "previous_live_affected": (prev or {}).get("affected_total"),
        "fk_protected_by": [f"{t}.{c}" for t, c in refs],
        "affected_total": 0,
        "archived_total": 0,
        "parts": [p.as_dict() for p in parts],
    }
    if forced is not None:
        result["override"] = {
            "older_than": forced.date().isoformat(),
            "cutoff": forced.isoformat(),
            "policy_cutoffs": {k: v.isoformat() for k, v in policy_cuts.items()},
            "min_age_days": OVERRIDE_MIN_AGE_DAYS,
            "max_rows_cap": OVERRIDE_MAX_ROWS,
            "environment": override_gate(environ),
            "opt_in": OVERRIDE_FLAG,
        }
    if spec.action == "redact":
        result["skipped_unprocessed"] = await conn.fetchval(
            "SELECT count(*) FROM booking.inbox_events WHERE received_at < $1 AND status = 'received'",
            cuts[spec.parts[0].name],
        )
    if any(p.archive for p in spec.parts):
        result["archive"] = f"s3://{ARCHIVE_BUCKET_DISPLAY}/{ARCHIVE_PREFIX}/{spec.archive_name}/yyyy/mm/"

    if mode == "held":
        async with conn.transaction():
            result["notified_admins"] = await _notify_held(conn, job, result)
            await _audit(conn, job, result)
        log.error("retention %s held: %s", spec.kind, reason)
        return result
    if mode == "dry_run":
        await _audit(conn, job, result)
        log.info("retention %s dry run: %d candidates (%s)", spec.kind, candidates, reason)
        return result

    remaining = limit_rows
    try:
        archiver = None
        if any(p.archive and r.candidates for p, r in zip(spec.parts, parts, strict=True)):
            archiver = S3Archiver(s3_factory(), job.id, bucket_factory())
        for part, res in zip(spec.parts, parts, strict=True):
            if remaining <= 0:
                break
            if res.candidates == 0:
                continue
            await runner.process_part(
                conn,
                spec,
                part,
                refs,
                cuts[part.name],
                res,
                limit_rows=remaining,
                batch_size=batch_size,
                archiver=archiver,
                pause_s=pause_s,
            )
            remaining -= res.affected
    except Exception as exc:
        # Batches already committed stay done: record what they did, then fail the job.
        failed = {
            **result,
            "mode": "failed",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "affected_total": sum(p.affected for p in parts),
            "archived_total": sum(p.archived_rows for p in parts),
            "parts": [p.as_dict() for p in parts],
        }
        await _audit(conn, job, failed)
        raise
    result["parts"] = [p.as_dict() for p in parts]
    result["affected_total"] = sum(p.affected for p in parts)
    result["archived_total"] = sum(p.archived_rows for p in parts)
    await _audit(conn, job, result)
    log.info(
        "retention %s live: %s %d rows, archived %d",
        spec.kind,
        spec.action,
        result["affected_total"],
        result["archived_total"],
    )
    return result


def build_handlers(
    s3_factory: S3Factory = default_s3,
    bucket_factory: BucketFactory = default_bucket,
    environ: Environ | None = None,
) -> dict[str, Any]:
    """``environ`` is what the override gate reads (None: the process's ``os.environ`` at run time, never
    ``Settings.environment``, whose "dev" default would open the gate)."""

    def make(spec: Spec) -> Any:
        async def handler(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
            return await run_spec(
                pool, job, spec, s3_factory=s3_factory, bucket_factory=bucket_factory, environ=environ
            )

        return handler

    return {kind: make(spec) for kind, spec in SPECS.items()}


# ── Enqueue: manual (admin API) and the daily schedule ────────────────────────


async def enqueue_manual(
    conn: asyncpg.Connection,
    kind: str,
    *,
    dry_run: bool,
    max_rows: int | None,
    requested_by: str,
    older_than: date | None = None,
) -> str:
    """Queue one manual run, or a manual_override run when ``older_than`` is given (the caller validates it and writes
    the audit row of the request in the same transaction)."""
    payload: dict[str, Any] = {"trigger": "manual", "dry_run": dry_run}
    if older_than is not None:
        payload["trigger"] = OVERRIDE_TRIGGER
        payload["older_than"] = older_than.isoformat()
    if max_rows is not None:
        payload["max_rows"] = max_rows
    job_id = await queue.enqueue(conn, kind, payload, max_attempts=MAX_ATTEMPTS, created_by=requested_by)
    if job_id is None:  # no idempotency key: only a unique-index conflict could refuse it
        raise RuntimeError(f"could not queue {kind}")
    return job_id


def schedule_key(kind: str, day: date) -> str:
    return f"{kind}:schedule:{day.isoformat()}"


class DailySchedule:
    """Worker maintenance hook: queues one ``trigger=schedule`` job per retention kind per UTC day, on the first pass
    at or after :data:`SCHEDULE_UTC_HOUR`. Idempotent across workers and restarts (idempotency key per kind and day);
    the in-memory day only saves the INSERT on later passes."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._done_day: date | None = None

    async def __call__(self, pool: asyncpg.Pool) -> list[str]:
        now = self._now()
        today = now.date()
        if now.hour < SCHEDULE_UTC_HOUR or self._done_day == today:
            return []
        queued = []
        for kind in KINDS:
            job_id = await queue.enqueue(
                pool,
                kind,
                {"trigger": "schedule"},
                idempotency_key=schedule_key(kind, today),
                max_attempts=MAX_ATTEMPTS,
                created_by=SCHEDULER,
            )
            if job_id:
                queued.append(job_id)
        self._done_day = today
        if queued:
            log.info("retention: queued %d scheduled runs for %s", len(queued), today)
        return queued


# ── Admin status ──────────────────────────────────────────────────────────────

_STATUS_JOBS_SQL = """
    SELECT DISTINCT ON (kind, bucket) kind, bucket, id::text AS id, status, created_by, created_at, finished_at,
           result::text AS result, error
      FROM (SELECT j.*, CASE WHEN j.status IN ('queued', 'running') THEN 'active'
                             WHEN j.status = 'succeeded' AND j.result ->> 'trigger' = 'manual_override'
                                  THEN 'override'
                             WHEN j.status = 'succeeded' THEN coalesce(j.result ->> 'mode', 'unknown')
                             ELSE 'failed' END AS bucket
              FROM booking.job j
             WHERE j.kind = ANY($1::text[])) x
     ORDER BY kind, bucket, created_at DESC
"""

LAST_LIVE_KEYS = ("affected_total", "archived_total", "windows")
_RESULT_KEYS = (
    "mode",
    "reason",
    "trigger",
    "candidates_total",
    "affected_total",
    "archived_total",
    "limit_rows",
    "skipped_unprocessed",
    "override",
)


def job_view(row: Any) -> dict[str, Any]:
    res = _loads(row["result"]) or {}
    return {
        "job_id": row["id"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        **{k: res[k] for k in _RESULT_KEYS if k in res},
    }


async def status(conn: asyncpg.Connection, environ: Environ | None = None) -> dict[str, Any]:
    """Per kind: the policy, the latest active / live / dry_run / held / override / failed job, and what the next
    scheduled run will do before counting (``scheduled_mode``; a manual_override run never makes it live).
    ``override.enabled`` says whether this deployment's gate is open (``environ``, default ``os.environ``)."""
    rows = await conn.fetch(_STATUS_JOBS_SQL, list(KINDS))
    by_kind: dict[str, dict[str, Any]] = {k: {} for k in KINDS}
    for r in rows:
        by_kind[r["kind"]][r["bucket"]] = job_view(r)
    out = []
    for kind, spec in SPECS.items():
        live = await last_live(conn, kind)
        out.append(
            {
                "kind": kind,
                "table": spec.table,
                "action": spec.action,
                "parts": [{"part": p.name, "window_days": p.window_days, "archive": p.archive} for p in spec.parts],
                "scheduled_mode": "live" if live and live.get("windows") == spec.windows() else "dry_run",
                "last_live": {k: live.get(k) for k in LAST_LIVE_KEYS} if live else None,
                "override_allowed": kind in OVERRIDE_KINDS,
                "last": by_kind[kind],
            }
        )
    return {
        "kinds": out,
        "run_max_rows": RUN_MAX_ROWS,
        "growth_factor": GROWTH_FACTOR,
        "growth_floor_rows": GROWTH_FLOOR_ROWS,
        "schedule_utc_hour": SCHEDULE_UTC_HOUR,
        "archive": f"s3://{ARCHIVE_BUCKET_DISPLAY}/{ARCHIVE_PREFIX}/<table>/yyyy/mm/",
        "override": {
            "trigger": OVERRIDE_TRIGGER,
            "kinds": list(OVERRIDE_KINDS),
            "min_age_days": OVERRIDE_MIN_AGE_DAYS,
            "max_rows": OVERRIDE_MAX_ROWS,
            "environments": sorted(OVERRIDE_ENVIRONMENTS),
            "opt_in": OVERRIDE_FLAG,
            "enabled": override_enabled(environ if environ is not None else os.environ),
        },
    }


async def job_detail(conn: asyncpg.Connection, job_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT id::text AS id, kind, status, payload::text AS payload, attempt, created_by, created_at, started_at,"
        " finished_at, result::text AS result, error FROM booking.job WHERE id = $1::uuid AND kind = ANY($2::text[])",
        job_id,
        list(KINDS),
    )
    if row is None:
        return None
    return {
        "job_id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "payload": _loads(row["payload"]),
        "attempt": row["attempt"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "result": _loads(row["result"]),
        "error": row["error"],
    }
