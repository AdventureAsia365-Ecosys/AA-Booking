"""``bk_tenant_erase`` (AA-1093 lane L3; root plan docs/audits/2026-11-13-S279-session/aa1093-plan.md Part 2, tech lead
decisions D3 and D8): the Booking step of the tenant offboarding erasure, contract tenant.erasure.v1.

**Booking anonymises, it does not delete (D3).** Booking holds no traveller identity (DM-6.8); what it holds of an
agency is the agency name on its partner row, the free text on its B2B trip cases, notifications and the before / after
of audit rows. One apply, in one transaction, for one tenant:

- ``trip_cases``: ``notes`` and ``inquiry_snapshot`` of the agency's B2B cases -> NULL;
- ``trip_case_status_history``: the status notes of those cases (a cancel reason lands here) -> NULL (bk-057 guard);
- ``supplier_bookings``: the ``notes`` / ``voucher_note`` of those cases -> NULL, a ``cancel_reason`` -> ``Erased``
  (the bk-026 CHECK needs a reason on a cancelled booking);
- ``trip_case_checks``: emergency contact name / phone and the note -> NULL, ``cleared_at`` stamped (as the
  ``bk_case_lifecycle`` clearing does; a cleared row never takes them again);
- ``notifications`` about those cases, their checks and supplier bookings: ``message`` -> NULL, ``data`` -> only its
  ``reminder_key`` (so a daily reminder is not sent again);
- ``b2b_partners``: ``agency_name`` -> ``Erased agency <first 8 characters of the tenant id>``, status ``suspended``;
- ``audit_logs`` of those entities and of the partner: ``before`` / ``after`` -> ``{"redacted": "tenant_erased"}``,
  the partner rows' ``entity_label`` (the agency name) -> NULL (bk-057 guard);
- **kept** (accounting exception): quotes, holds, agency invoices with their bill-to snapshot, agency payments. They
  are counted under ``kept`` and never written.

One ``booking.audit_logs`` row ``tenant.erased`` per apply (entity ``tenant``, the plan hash and the counts) and one
``booking.tenant_erasure_log`` row per request and mode (bk-057). An applied row makes ``bk_tenant_pull`` skip the
agency, so the CIS name is never written back while CIS still holds the tenant (CIS erases last).

**Plan and hash.** :func:`plan` is read-only: per step its action, the number of rows it would change and
``md5(string_agg(id || updated time))`` of those rows (no row value). ``plan_hash`` = SHA-256 of the canonical JSON of
the tenant id and the steps. An apply recomputes the plan inside its transaction and writes nothing when the hash is
not the approved one (``plan_changed``): the DRY RUN a person (or the saga's combined fingerprint) approved is exactly
what is written. A second plan after an apply finds 0 rows in every step.

**Guards (D8), fail closed.** Read from ``catalog.v_tenant_account_v1`` (CIS 357 / 401): the pilot tenant is always
refused; outside ``ENVIRONMENT=prod`` only a synthetic tenant (``is_synthetic``) is applied; a tenant the view does not
hold, or a view that cannot be read, is refused. A DRY RUN is never refused (it reports ``refused`` instead).

**Requests.** When ``catalog.v_tenant_erasure_v1`` (the CIS saga, contract tenant.erasure.v1; feature-detected like
every catalog view) is readable, :class:`EraseSchedule` queues a DRY RUN for a request in phase ``plan`` and an apply
for a request in phase ``apply`` with its approved Booking hash (``approved_plans->>'booking'``), the apply only while
the kill switch ``TENANT_ERASE_APPLY_ENABLED`` is on (default off; root thresholds audit section 4 item 7: a destructive
kill switch stays in Terraform). Without the view: the CLI only.

CLI, in the Booking API container (``DATABASE_URL`` = role ``booking``) through ECS Exec on ``aa-booking-dev-api``::

    python -m app.b2b.tenant_erase --tenant <uuid>                                  # DRY RUN: counts + plan_hash
    python -m app.b2b.tenant_erase --tenant <uuid> --apply --expect-plan <plan_hash>  # write exactly that plan

Output: one JSON object (counts, plan hash, guards). No names, no row values.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app import audit
from app.catalog import liveness
from app.jobs import queue
from app.jobs.queue import Job

log = logging.getLogger(__name__)

KIND = "bk_tenant_erase"
ACTOR = "system:bk_tenant_erase"
SCHEDULER = "system:tenant_erase"
MAX_ATTEMPTS = 3  # technical-constant: job runner mechanics (a transactional, idempotent run); follow-up: job settings
SLOT_SECONDS = 300  # technical-constant: schedule cadence of the request read, like bk_tenant_pull
REQUIRED_KEY = "bk-057"
ERASE_SETTING = "booking.tenant_erase"  # the bk-057 guards let the redaction through only while this is set
REDACTED = {"redacted": "tenant_erased"}
ERASED_REASON = "Erased"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Namespace of the request id of a CLI run (one per tenant), so a CLI apply is recorded like a saga request.
CLI_NAMESPACE = uuid.UUID("6f1c2b5e-2a49-5d0e-9c1a-0b9310930000")

ACCOUNT_VIEW = liveness.View("catalog", "v_tenant_account_v1", ("tenant_id", "is_pilot", "is_synthetic"))
ACCOUNT_SQL = f"SELECT is_pilot, is_synthetic FROM {ACCOUNT_VIEW.qualified} WHERE tenant_id = $1"  # noqa: S608
# The CIS request feed of contract tenant.erasure.v1 (the saga's view). Booking selects these columns only; a view
# without one of them is treated as missing (the CLI stays the only way in).
REQUEST_VIEW = liveness.View("catalog", "v_tenant_erasure_v1", ("request_id", "tenant_id", "phase", "approved_plans"))
REQUESTS_SQL = f"""
SELECT request_id, tenant_id, phase, approved_plans->>'booking' AS approved
  FROM {REQUEST_VIEW.qualified} WHERE phase IN ('plan', 'apply') ORDER BY request_id
"""  # noqa: S608 — fixed name
LOG_TABLE_SQL = "SELECT to_regclass('booking.tenant_erasure_log') IS NOT NULL AS ok"
KEY_SQL = "SELECT EXISTS (SELECT 1 FROM booking.v_schema_versions WHERE version = $1) AS ok"

# The agency's B2B cases and the rows hanging off them ($1 = tenant id).
CASES = "SELECT id FROM booking.trip_cases WHERE channel = 'b2b' AND party_tenant_id = $1"
CHECKS = f"SELECT id FROM booking.trip_case_checks WHERE trip_case_id IN ({CASES})"  # noqa: S608
BOOKINGS = f"SELECT id FROM booking.supplier_bookings WHERE trip_case_id IN ({CASES})"  # noqa: S608
VERSIONS = f"SELECT id FROM booking.trip_case_versions WHERE trip_case_id IN ({CASES})"  # noqa: S608
PARTNER = "SELECT id FROM booking.b2b_partners WHERE tenant_id = $1"
NOTIFIED = f"({CASES} UNION ALL {CHECKS} UNION ALL {BOOKINGS})"
AUDITED = f"({CASES} UNION ALL {CHECKS} UNION ALL {BOOKINGS} UNION ALL {VERSIONS} UNION ALL {PARTNER})"
REDACTED_SQL = '\'{"redacted": "tenant_erased"}\'::jsonb'


@dataclass(frozen=True)
class Step:
    """One table's part of the erasure: the rows it changes (``rows``: ``id`` and ``ts``) and the UPDATE. ``$1`` is the
    tenant id in both; the UPDATE's WHERE is the same as ``rows``, so an applied step plans 0 rows afterwards."""

    name: str
    action: str
    rows: str
    update: str


STEPS: tuple[Step, ...] = (
    Step(
        "trip_cases",
        "redact notes, inquiry_snapshot",
        "SELECT id, updated_at AS ts FROM booking.trip_cases WHERE channel = 'b2b' AND party_tenant_id = $1"
        " AND (notes IS NOT NULL OR inquiry_snapshot IS NOT NULL)",
        "UPDATE booking.trip_cases SET notes = NULL, inquiry_snapshot = NULL WHERE channel = 'b2b'"
        " AND party_tenant_id = $1 AND (notes IS NOT NULL OR inquiry_snapshot IS NOT NULL)",
    ),
    Step(
        "trip_case_status_history",
        "redact note",
        f"SELECT id, changed_at AS ts FROM booking.trip_case_status_history WHERE trip_case_id IN ({CASES})"  # noqa: S608
        " AND note IS NOT NULL",
        f"UPDATE booking.trip_case_status_history SET note = NULL WHERE trip_case_id IN ({CASES})"  # noqa: S608
        " AND note IS NOT NULL",
    ),
    Step(
        "supplier_bookings",
        "redact notes, voucher_note, cancel_reason",
        f"SELECT id, updated_at AS ts FROM booking.supplier_bookings WHERE trip_case_id IN ({CASES})"  # noqa: S608
        f" AND (notes IS NOT NULL OR voucher_note IS NOT NULL OR cancel_reason <> '{ERASED_REASON}')",
        f"UPDATE booking.supplier_bookings SET notes = NULL, voucher_note = NULL,"  # noqa: S608
        f" cancel_reason = CASE WHEN cancel_reason IS NULL THEN NULL ELSE '{ERASED_REASON}' END"
        f" WHERE trip_case_id IN ({CASES})"
        f" AND (notes IS NOT NULL OR voucher_note IS NOT NULL OR cancel_reason <> '{ERASED_REASON}')",
    ),
    Step(
        "trip_case_checks",
        "null emergency contact and note, stamp cleared_at",
        f"SELECT id, updated_at AS ts FROM booking.trip_case_checks WHERE trip_case_id IN ({CASES})"  # noqa: S608
        " AND (note IS NOT NULL OR emergency_contact_name IS NOT NULL OR emergency_contact_phone IS NOT NULL)",
        "UPDATE booking.trip_case_checks SET emergency_contact_name = NULL, emergency_contact_phone = NULL,"  # noqa: S608
        f" note = NULL, cleared_at = coalesce(cleared_at, now()) WHERE trip_case_id IN ({CASES})"  # noqa: S608
        " AND (note IS NOT NULL OR emergency_contact_name IS NOT NULL OR emergency_contact_phone IS NOT NULL)",
    ),
    Step(
        "notifications",
        "redact message, data (reminder_key kept)",
        f"SELECT id, created_at AS ts FROM booking.notifications WHERE entity_id IN {NOTIFIED}"  # noqa: S608
        " AND (message IS NOT NULL OR data - 'reminder_key' <> '{}'::jsonb)",
        "UPDATE booking.notifications SET message = NULL, data = CASE WHEN data ? 'reminder_key'"  # noqa: S608
        " THEN jsonb_build_object('reminder_key', data->'reminder_key') ELSE '{}'::jsonb END"
        f" WHERE entity_id IN {NOTIFIED}"  # noqa: S608
        " AND (message IS NOT NULL OR data - 'reminder_key' <> '{}'::jsonb)",
    ),
    Step(
        "b2b_partners",
        "replace agency_name, suspend",
        "SELECT id, updated_at AS ts FROM booking.b2b_partners WHERE tenant_id = $1"
        " AND (agency_name <> 'Erased agency ' || left($1::text, 8) OR status <> 'suspended')",
        "UPDATE booking.b2b_partners SET agency_name = 'Erased agency ' || left($1::text, 8), status = 'suspended'"
        " WHERE tenant_id = $1 AND (agency_name <> 'Erased agency ' || left($1::text, 8) OR status <> 'suspended')",
    ),
    # Last: the redaction of the audit rows of everything above (the steps write no audit row of their own).
    Step(
        "audit_logs",
        "redact before, after (partner entity_label)",
        f"SELECT id, created_at AS ts FROM booking.audit_logs WHERE entity_id IN {AUDITED}"  # noqa: S608
        f" AND ((before IS NOT NULL AND before <> {REDACTED_SQL}) OR (after IS NOT NULL AND after <> {REDACTED_SQL})"
        " OR (entity_type = 'b2b_partner' AND entity_label IS NOT NULL))",
        f"UPDATE booking.audit_logs SET before = CASE WHEN before IS NULL THEN NULL ELSE {REDACTED_SQL} END,"  # noqa: S608
        f" after = CASE WHEN after IS NULL THEN NULL ELSE {REDACTED_SQL} END,"
        " entity_label = CASE WHEN entity_type = 'b2b_partner' THEN NULL ELSE entity_label END"
        f" WHERE entity_id IN {AUDITED}"
        f" AND ((before IS NOT NULL AND before <> {REDACTED_SQL}) OR (after IS NOT NULL AND after <> {REDACTED_SQL})"
        " OR (entity_type = 'b2b_partner' AND entity_label IS NOT NULL))",
    ),
)

# Kept under the accounting exception (D3): counted for the report, never written, not part of the hash.
KEPT: tuple[tuple[str, str], ...] = (
    ("b2b_quotes", f"SELECT count(*) FROM booking.b2b_quotes WHERE partner_id IN ({PARTNER})"),  # noqa: S608
    ("b2b_holds", f"SELECT count(*) FROM booking.b2b_holds WHERE partner_id IN ({PARTNER})"),  # noqa: S608
    ("agency_invoice", "SELECT count(*) FROM booking.agency_invoice WHERE party_tenant_id = $1"),
    (
        "agency_payment",
        "SELECT count(*) FROM booking.agency_payment WHERE invoice_id IN"
        " (SELECT id FROM booking.agency_invoice WHERE party_tenant_id = $1)",
    ),
)


def fingerprint_sql(step: Step) -> str:
    """Count and md5 of the step's rows: id and the microsecond epoch of their time (no session time zone involved)."""
    return (
        "SELECT count(*)::int AS n, md5(coalesce(string_agg(id::text || '|'"  # noqa: S608
        " || coalesce(((extract(epoch FROM ts) * 1000000)::bigint)::text, ''), ',' ORDER BY id), '')) AS fp"
        f" FROM ({step.rows}) s"  # noqa: S608 — constant step SQL
    )


class Refused(RuntimeError):
    """The tenant may not be erased here (pilot, not synthetic outside prod, unknown): nothing written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlanChanged(RuntimeError):
    """The plan computed at apply time is not the approved one (nothing written)."""

    code = "plan_changed"


def erased_name(tenant_id: uuid.UUID | str) -> str:
    return f"Erased agency {str(tenant_id)[:8]}"


def cli_request_id(tenant_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(CLI_NAMESPACE, f"booking-tenant-erase:{tenant_id}")


def plan_hash(tenant_id: uuid.UUID, steps: list[dict[str, Any]]) -> str:
    """SHA-256 of the tenant id and the steps (name, action, count, fingerprint) in canonical JSON."""
    canonical = json.dumps({"tenant_id": str(tenant_id), "steps": steps}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_prod(environment: str | None) -> bool:
    """Only an explicit ``prod`` lifts the synthetic-only rule (an unset or other value counts as Dev: fail closed)."""
    return (environment or "").strip().lower() == "prod"


async def plan(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> dict[str, Any]:
    """DRY RUN: per step the rows it would change and their fingerprint, the kept counts and the plan hash."""
    steps: list[dict[str, Any]] = []
    for step in STEPS:
        row = await conn.fetchrow(fingerprint_sql(step), tenant_id)
        steps.append({"step": step.name, "action": step.action, "count": row["n"], "fingerprint": row["fp"]})
    kept = {name: await conn.fetchval(sql, tenant_id) for name, sql in KEPT}
    return {
        "tenant_id": str(tenant_id),
        "steps": steps,
        "rows": sum(s["count"] for s in steps),
        "kept": kept,
        "plan_hash": plan_hash(tenant_id, steps),
    }


def counts(planned: dict[str, Any]) -> dict[str, Any]:
    return {**{s["step"]: s["count"] for s in planned["steps"]}, "kept": planned["kept"]}


async def guard(conn: asyncpg.Connection, tenant_id: uuid.UUID, *, prod: bool) -> None:
    """Raise :class:`Refused` unless this tenant may be erased here (D8; fail closed)."""
    try:
        ready = await liveness.view_ready(conn, ACCOUNT_VIEW)
        account = await conn.fetchrow(ACCOUNT_SQL, tenant_id) if ready else None
    except (asyncpg.PostgresError, OSError) as exc:
        raise Refused("account_unknown", f"{ACCOUNT_VIEW.qualified} unreadable ({type(exc).__name__})") from exc
    if not ready:
        raise Refused("account_unknown", f"{ACCOUNT_VIEW.qualified} not readable (CIS 357 / 401): pilot unknown")
    if account is None:
        raise Refused("tenant_unknown", "the tenant is not in the CIS tenant account view (Booking erases before CIS)")
    if account["is_pilot"]:
        raise Refused("pilot_tenant", "the pilot tenant is never erased (root ADR 0019 d16, AA-1093 D8)")
    if not prod and account["is_synthetic"] is not True:
        raise Refused("not_synthetic", "outside prod only a synthetic tenant is erased (AA-1093 D8)")


async def guard_result(conn: asyncpg.Connection, tenant_id: uuid.UUID, *, prod: bool) -> dict[str, Any]:
    try:
        await guard(conn, tenant_id, prod=prod)
    except Refused as exc:
        return {"apply_allowed": False, "refused": exc.code}
    return {"apply_allowed": True, "refused": None}


UPSERT_DRY_SQL = """
INSERT INTO booking.tenant_erasure_log (request_id, tenant_id, mode, plan_hash, counts, status, source, job_id)
VALUES ($1, $2, 'dry_run', $3, $4::jsonb, 'planned', $5, $6::uuid)
ON CONFLICT (request_id, mode) DO UPDATE
   SET plan_hash = EXCLUDED.plan_hash, counts = EXCLUDED.counts, status = 'planned', detail = NULL,
       job_id = EXCLUDED.job_id
"""
UPSERT_APPLY_SQL = """
INSERT INTO booking.tenant_erasure_log
    (request_id, tenant_id, mode, plan_hash, counts, status, detail, source, job_id, finished_at)
VALUES ($1, $2, 'apply', $3, $4::jsonb, $5, $6, $7, $8::uuid, now())
ON CONFLICT (request_id, mode) DO UPDATE
   SET plan_hash = EXCLUDED.plan_hash, counts = EXCLUDED.counts, status = EXCLUDED.status, detail = EXCLUDED.detail,
       job_id = EXCLUDED.job_id, finished_at = now()
 WHERE booking.tenant_erasure_log.status <> 'applied'
"""
STALE_SQL = """
UPDATE booking.tenant_erasure_log SET status = 'stale', detail = $2
 WHERE request_id = $1 AND mode = 'dry_run' AND status = 'planned'
"""
APPLIED_SQL = """
SELECT plan_hash FROM booking.tenant_erasure_log WHERE request_id = $1 AND mode = 'apply' AND status = 'applied'
"""
ERASED_TENANTS_SQL = """
SELECT DISTINCT tenant_id FROM booking.tenant_erasure_log WHERE mode = 'apply' AND status = 'applied'
"""


async def log_ready(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval(LOG_TABLE_SQL))


async def erased_tenants(conn: asyncpg.Connection) -> set[uuid.UUID]:
    """The tenants Booking erased (an applied log row); empty before bk-057."""
    if not await log_ready(conn):
        return set()
    return {r["tenant_id"] for r in await conn.fetch(ERASED_TENANTS_SQL)}


async def record_plan(
    conn: asyncpg.Connection,
    request_id: uuid.UUID,
    planned: dict[str, Any],
    *,
    source: str,
    job_id: str | None = None,
) -> None:
    await conn.execute(
        UPSERT_DRY_SQL,
        request_id,
        uuid.UUID(planned["tenant_id"]),
        planned["plan_hash"],
        json.dumps(counts(planned)),
        source,
        job_id,
    )


async def _record_refusal(
    conn: asyncpg.Connection,
    request_id: uuid.UUID,
    tenant_id: uuid.UUID,
    digest: str,
    code: str,
    *,
    source: str,
    job_id: str | None,
    planned: dict[str, Any] | None = None,
) -> None:
    async with conn.transaction():
        await conn.execute(
            UPSERT_APPLY_SQL,
            request_id,
            tenant_id,
            digest if HASH_RE.match(digest or "") else "0" * 64,
            json.dumps(counts(planned) if planned else {}),
            "refused",
            code,
            source,
            job_id,
        )
        if code == PlanChanged.code:
            await conn.execute(STALE_SQL, request_id, code)


async def apply(
    conn: asyncpg.Connection,
    tenant_id: uuid.UUID,
    expected_hash: str,
    *,
    request_id: uuid.UUID,
    prod: bool,
    source: str,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Erase exactly the approved plan in one transaction (``expected_hash`` = the DRY RUN's ``plan_hash``).

    Raises :class:`Refused` (guards) or :class:`PlanChanged`; both write nothing but an apply log row ``refused``
    with the reason code (and mark the DRY RUN ``stale`` on a changed plan). A request already applied returns
    ``already_applied`` and writes nothing."""
    if not HASH_RE.match(expected_hash or ""):
        raise PlanChanged("--expect-plan must be the DRY RUN's plan_hash (64 lower-case hex characters)")
    if not await conn.fetchval(KEY_SQL, REQUIRED_KEY):
        raise RuntimeError(f"{REQUIRED_KEY} is not applied: no erasure log and no erase guards yet")
    done = await conn.fetchval(APPLIED_SQL, request_id)
    if done is not None:
        return {"outcome": "already_applied", "tenant_id": str(tenant_id), "plan_hash": done}
    try:
        await guard(conn, tenant_id, prod=prod)
    except Refused as exc:
        await _record_refusal(conn, request_id, tenant_id, expected_hash, exc.code, source=source, job_id=job_id)
        raise
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{KIND}:{tenant_id}")
        await conn.execute("SELECT set_config($1, $2, true)", ERASE_SETTING, str(request_id))
        planned = await plan(conn, tenant_id)
        changed = planned["plan_hash"] != expected_hash
        if not changed:
            for step in STEPS:
                await conn.execute(step.update, tenant_id)
            await audit.record(
                conn,
                actor_type="system",
                actor_ref=ACTOR,
                action="tenant.erased",
                entity_type="tenant",
                entity_id=str(tenant_id),
                after={"request_id": str(request_id), "plan_hash": expected_hash, "counts": counts(planned)},
                entity_label=erased_name(tenant_id),
                summary="Tenant erased in Booking: free text redacted, agency name replaced; money rows and the "
                "invoice bill-to kept (accounting exception)",
            )
            await conn.execute(
                UPSERT_APPLY_SQL,
                request_id,
                tenant_id,
                expected_hash,
                json.dumps(counts(planned)),
                "applied",
                None,
                source,
                job_id,
            )
    if changed:
        await _record_refusal(
            conn, request_id, tenant_id, expected_hash, PlanChanged.code, source=source, job_id=job_id, planned=planned
        )
        raise PlanChanged(
            f"the plan changed since the DRY RUN (now {planned['plan_hash']}, {planned['rows']} rows): nothing "
            "written; run the DRY RUN again and have it approved"
        )
    log.info("bk_tenant_erase_applied tenant=%s request=%s rows=%d", tenant_id, request_id, planned["rows"])
    return {"outcome": "applied", **planned}


# ── The job and its schedule ─────────────────────────────────────────────────────────────────────────────────────


def build_handler(*, apply_enabled: bool, environment: str) -> Callable[[asyncpg.Pool, Job], Any]:
    async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        return await run_job(pool, job, apply_enabled=apply_enabled, environment=environment)

    return handle


async def run_job(pool: asyncpg.Pool, job: Job, *, apply_enabled: bool, environment: str) -> dict[str, Any]:
    """Payload ``{request_id, tenant_id, mode: dry_run | apply, expect_plan?}``. A refusal is a result, not a retry."""
    p = job.payload
    try:
        request_id, tenant_id = uuid.UUID(str(p["request_id"])), uuid.UUID(str(p["tenant_id"]))
    except (KeyError, ValueError):
        return {"outcome": "invalid_payload"}
    mode = p.get("mode")
    prod = is_prod(environment)
    async with pool.acquire() as conn:
        if not await log_ready(conn):
            return {"outcome": "skipped", "reason": f"{REQUIRED_KEY} not applied"}
        if mode == "dry_run":
            planned = await plan(conn, tenant_id)
            async with conn.transaction():
                await record_plan(conn, request_id, planned, source="cis", job_id=job.id)
            return {"outcome": "planned", "mode": mode, **planned, **await guard_result(conn, tenant_id, prod=prod)}
        if mode != "apply":
            return {"outcome": "invalid_payload", "mode": mode}
        if not apply_enabled:
            return {"outcome": "disabled", "reason": "TENANT_ERASE_APPLY_ENABLED is off"}
        try:
            return await apply(conn, tenant_id, str(p.get("expect_plan") or ""), request_id=request_id, prod=prod,
                               source="cis", job_id=job.id)  # fmt: skip
        except (Refused, PlanChanged) as exc:
            return {"outcome": "refused", "reason": getattr(exc, "code", "refused"), "detail": str(exc)}


def schedule_key(at: datetime) -> int:
    return int(at.timestamp()) // SLOT_SECONDS


class EraseSchedule:
    """Worker maintenance hook: once per :data:`SLOT_SECONDS` slot, while ``catalog.v_tenant_erasure_v1`` is readable
    and bk-057 is applied, queue a DRY RUN for every request in phase ``plan`` without a current plan and an apply for
    every request in phase ``apply`` with an approved Booking hash and no applied row (only with the kill switch on)."""

    def __init__(self, *, apply_enabled: bool, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._apply_enabled = apply_enabled
        self._now = now
        self._done_slot: int | None = None

    async def __call__(self, pool: asyncpg.Pool) -> list[str] | None:
        slot = schedule_key(self._now())
        if self._done_slot == slot:
            return None
        self._done_slot = slot
        async with pool.acquire() as conn:
            if not await liveness.view_ready(conn, REQUEST_VIEW) or not await log_ready(conn):
                return None
            requests = await conn.fetch(REQUESTS_SQL)
            logged = {
                (r["request_id"], r["mode"]): r["status"]
                for r in await conn.fetch(
                    "SELECT request_id, mode, status FROM booking.tenant_erasure_log"
                    " WHERE request_id = ANY($1::uuid[])",
                    [r["request_id"] for r in requests],
                )
            }
        queued: list[str] = []
        for r in requests:
            payload: dict[str, Any] = {"request_id": str(r["request_id"]), "tenant_id": str(r["tenant_id"])}
            if r["phase"] == "plan" and logged.get((r["request_id"], "dry_run")) != "planned":
                job_id = await queue.enqueue(pool, KIND, {**payload, "mode": "dry_run"}, max_attempts=MAX_ATTEMPTS,
                                             idempotency_key=f"{KIND}:{r['request_id']}:dry_run:{slot}",
                                             created_by=SCHEDULER)  # fmt: skip
            elif (
                r["phase"] == "apply"
                and self._apply_enabled
                and HASH_RE.match(r["approved"] or "")
                and logged.get((r["request_id"], "apply")) != "applied"
            ):
                key = f"{KIND}:{r['request_id']}:apply:{r['approved']}"
                body = {**payload, "mode": "apply", "expect_plan": r["approved"]}
                job_id = await queue.enqueue(
                    pool, KIND, body, max_attempts=MAX_ATTEMPTS, created_by=SCHEDULER, idempotency_key=key
                )
            else:
                continue
            if job_id is not None:
                queued.append(job_id)
        return queued


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def _report(result: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in result.items() if k != "steps"}
    out["steps"] = {s["step"]: {"action": s["action"], "count": s["count"]} for s in result.get("steps", [])}
    return out


async def _main(tenant: str, apply_changes: bool, expected_hash: str | None, request: str | None) -> int:
    try:
        tenant_id = uuid.UUID(tenant)
        request_id = uuid.UUID(request) if request else cli_request_id(tenant_id)
    except ValueError:
        print(json.dumps({"error": "--tenant and --request-id must be UUIDs"}))
        return 2
    if apply_changes and not expected_hash:
        print(json.dumps({"error": "--apply needs --expect-plan <plan_hash of the DRY RUN>"}))
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(json.dumps({"error": "DATABASE_URL not set"}))
        return 2
    prod = is_prod(os.environ.get("ENVIRONMENT"))
    conn = await asyncpg.connect(
        dsn, ssl=os.environ.get("DB_SSL", "require"), timeout=10,
        server_settings={"application_name": "booking-tenant-erase"},
    )  # fmt: skip
    try:
        if apply_changes:
            result = await apply(conn, tenant_id, expected_hash or "", request_id=request_id, prod=prod, source="cli")
        else:
            result = {**await plan(conn, tenant_id), **await guard_result(conn, tenant_id, prod=prod)}
            if request and await log_ready(conn):  # a saga request: record the plan the saga collects
                async with conn.transaction():
                    await record_plan(conn, request_id, result, source="cli")
    except (Refused, PlanChanged) as exc:
        print(json.dumps({"error": str(exc), "reason": getattr(exc, "code", "refused")}))
        return 3
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 3
    finally:
        await conn.close()
    mode = "APPLY" if apply_changes else "DRY RUN"
    print(json.dumps({"mode": mode, "request_id": str(request_id), **_report(result)}, indent=1, default=str))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anonymise one agency tenant in Booking (DRY RUN by default).")
    parser.add_argument("--tenant", required=True, help="the CIS tenant id of the agency")
    parser.add_argument("--apply", action="store_true", help="write the erasure (default: DRY RUN, counts only)")
    parser.add_argument("--expect-plan", default=None, help="with --apply: the DRY RUN's plan_hash (required)")
    parser.add_argument("--request-id", default=None, help="the CIS erasure request id (default: one per tenant)")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.tenant, args.apply, args.expect_plan, args.request_id))


if __name__ == "__main__":
    sys.exit(main())
