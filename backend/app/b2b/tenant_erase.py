"""``bk_tenant_erase`` (AA-1093 lane L3; root plan docs/audits/2026-11-13-S279-session/aa1093-plan.md Part 2, tech lead
decisions D3 and D8): the Booking step of the tenant offboarding erasure, contract tenant.erasure.v1 (root
docs/contracts/tenant.erasure.v1.openapi.yaml).

**Booking anonymises, it does not delete (D3).** Booking holds no traveller identity (DM-6.8); what it holds of an
agency is the agency name on its partner row and staff free text on its B2B rows, notifications and audit rows. One
apply, in one transaction, for one tenant; ``booking.tenant_erase_scope(tenant)`` (bk-057) is the agency's footprint:

- ``trip_cases``: ``notes`` and ``inquiry_snapshot`` of the agency's B2B cases -> NULL;
- ``trip_case_status_history``: the status notes of those cases (a cancel reason lands here) -> NULL;
- ``supplier_bookings``: ``notes`` / ``voucher_note`` -> NULL, a ``cancel_reason`` -> ``[redacted]`` (bk-026 CHECK);
- ``trip_case_checks``: emergency contact and note -> NULL (``cleared_at`` stamped, as the lifecycle clearing does), a
  custom check's ``title`` -> ``[redacted]``;
- ``b2b_cancellations``: ``waive_reason`` -> ``[redacted]`` (the one move the bk-047 guard allows);
- ``agency_invoice``: ``staff_note`` -> NULL, ``void_reason`` -> ``[redacted]``; ``agency_payment``: ``note`` -> NULL,
  ``reject_reason`` -> ``[redacted]`` (bk-057 payment guard). Amounts, numbers, references, the bill-to snapshot and
  the decisions stay (accounting exception, DM-7.1);
- ``notifications`` about any row of the footprint: ``title`` -> ``[redacted]``, ``message`` -> NULL, ``data`` -> only
  its ``reminder_key`` (so a daily reminder is not sent again);
- ``b2b_partners``: ``agency_name`` -> ``Erased agency <first 8 characters of the tenant id>``, status ``suspended``;
- ``audit_logs`` of any row of the footprint: ``before`` / ``after`` -> ``{"redacted": "tenant_erased"}``,
  ``entity_label`` -> NULL, ``summary`` -> ``[redacted]`` (a staff summary can carry a cancel reason or a check title);
- **kept** (counted as action ``keep``, never written): quotes, holds, desk bookings (their ``override_reason`` is the
  manual bk_owner run-script of the DSAR runbook), cancellations, departure fees (``note``: the same run-script), agency
  invoices with their bill-to, invoice reminders, agency payments.

The append-only tables only take these writes inside an **opened erasure**: the transaction sets
``booking.tenant_erase`` to the report id of this round's ok DRY RUN, and the bk-057 guards check that it names an ok
DRY RUN of the log and that each row belongs to that tenant's footprint.

**Plan and hash (contract).** :func:`plan` is read-only: one table entry per (table, action) with the rows it would
change and their digest, md5 hex of the sorted ``'<id>|<updated_at in UTC ISO 8601, or empty>'`` lines joined by a
newline; kept tables as ``keep`` entries (no digest). ``plan_hash`` = sha256 hex of
``'tenant.erasure.v1|<tenant>|<round>'`` followed by the sorted ``'<table>|<action>|<rows>|<digest>'`` lines, all
joined by a newline. An apply recomputes the plan inside its transaction and writes nothing when the hash is not the
approved one (``plan_changed``); each statement must touch exactly its planned rows. A plan after an apply finds 0
rows in every redact entry.

**Verify.** The census reads every ``pii:`` column (DM-6.1 comment) of the tables the plan names and counts the
tenant's rows that still hold a value in one of the columns the erasure clears; the columns kept by rule are listed in
:data:`CENSUS_KEPT`. A ``pii:`` column that is in neither list fails the verification (``census_incomplete``), so a new
column is never silently left out.

**Reports.** Every DRY RUN, apply and verification appends one row to ``booking.tenant_erasure_log`` (bk-057), read by
CIS through ``booking.v_tenant_erasure_report_v1``: status ``ok`` | ``blocked`` | ``refused`` | ``failed`` with an
error code (``not_due``, ``pilot_refused``, ``not_synthetic``, ``apply_disabled``, ``approval_expired``,
``approval_missing``, ``dry_run_missing``, ``plan_changed``, ``census_not_zero``, ``census_incomplete``,
``internal_error``). An apply ``ok`` row makes ``bk_tenant_pull`` skip the agency, so the CIS name is never written
back while CIS still holds the tenant (CIS erases last).

**Guards (D8), fail closed.** From the request (``is_pilot`` / ``is_synthetic`` as CIS copied them at T0): the pilot is
refused in every mode; outside ``ENVIRONMENT=prod`` only a synthetic tenant is applied; the apply also needs the kill
switch ``TENANT_ERASE_APPLY_ENABLED`` (default off; root thresholds audit section 4 item 7: a destructive kill switch
stays in Terraform), a hash the tech lead approved for this round and an approval that has not expired. When
``catalog.v_tenant_account_v1`` still holds the tenant and says it is the pilot, the apply is refused too.

**Requests.** :class:`EraseSchedule` reads ``catalog.v_tenant_erasure_v1`` (CIS 410, feature-detected) ``WHERE app =
'booking'``, the newest row of each request: for a step ``active`` past its ``not_before``, it queues one job per
(request, round, phase) for phase ``dry_run``, ``apply`` or ``verify`` that has no report yet.

CLI, in the Booking API container (``DATABASE_URL`` = role ``booking``) through ECS Exec on ``aa-booking-dev-api``
(guards from ``catalog.v_tenant_account_v1``; the kill switch gates the job only, a CLI apply needs the DRY RUN hash)::

    python -m app.b2b.tenant_erase --tenant <uuid>                                  # DRY RUN: report + plan_hash
    python -m app.b2b.tenant_erase --tenant <uuid> --mode apply --expect-plan <plan_hash>
    python -m app.b2b.tenant_erase --tenant <uuid> --mode verify

Output: one JSON object (the report). No names, no row values.
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
CONTRACT = "tenant.erasure.v1"
APP = "booking"
MODES = ("dry_run", "apply", "verify")
MAX_ATTEMPTS = 3  # technical-constant: job runner mechanics (a transactional, idempotent run); follow-up: job settings
SLOT_SECONDS = 300  # technical-constant: schedule cadence of the request read, like bk_tenant_pull
REQUIRED_KEY = "bk-057"
ERASE_SETTING = "booking.tenant_erase"  # the bk-057 guards open only while it names an ok DRY RUN report
REDACTED_JSON = '\'{"redacted": "tenant_erased"}\'::jsonb'
REDACTED = "[redacted]"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Namespace of the request id of a CLI run (one per tenant), so a CLI run is reported like a saga request.
CLI_NAMESPACE = uuid.UUID("6f1c2b5e-2a49-5d0e-9c1a-0b9310930000")

ACCOUNT_VIEW = liveness.View("catalog", "v_tenant_account_v1", ("tenant_id", "is_pilot", "is_synthetic"))
ACCOUNT_SQL = f"SELECT is_pilot, is_synthetic FROM {ACCOUNT_VIEW.qualified} WHERE tenant_id = $1"  # noqa: S608
# The CIS request feed (contract tenant.erasure.v1 ErasureRequestRow, CIS 410). A view without one of these columns
# is treated as missing (the CLI stays the only way in).
REQUEST_VIEW = liveness.View(
    "catalog",
    "v_tenant_erasure_v1",
    ("request_id", "tenant_id", "phase", "round", "app", "step_status", "not_before", "approved_plan_hash",
     "approval_expires_at", "is_pilot", "is_synthetic", "updated_at"),
)  # fmt: skip
_REQUEST_COLUMNS = (
    "request_id, tenant_id, phase, round, step_status, not_before, approved_plan_hash, approval_expires_at, is_pilot,"
    " is_synthetic"
)
REQUESTS_SQL = f"""
SELECT * FROM (
    SELECT DISTINCT ON (request_id) {_REQUEST_COLUMNS}
      FROM {REQUEST_VIEW.qualified} WHERE app = 'booking' ORDER BY request_id, updated_at DESC
) r WHERE step_status = 'active' AND phase IN ('dry_run', 'apply', 'verify') AND not_before <= now()
"""  # noqa: S608 — fixed names
REQUEST_SQL = f"""
SELECT {_REQUEST_COLUMNS} FROM {REQUEST_VIEW.qualified}
 WHERE app = 'booking' AND request_id = $1 ORDER BY updated_at DESC LIMIT 1
"""  # noqa: S608 — fixed names
LOG_TABLE_SQL = "SELECT to_regclass('booking.tenant_erasure_log') IS NOT NULL AS ok"
KEY_SQL = "SELECT EXISTS (SELECT 1 FROM booking.v_schema_versions WHERE version = $1) AS ok"

SCOPE = "SELECT id FROM booking.tenant_erase_scope($1) AS s(id)"
CASES = "SELECT id FROM booking.trip_cases WHERE channel = 'b2b' AND party_tenant_id = $1"
PARTNER = "SELECT id FROM booking.b2b_partners WHERE tenant_id = $1"
INVOICES = "SELECT id FROM booking.agency_invoice WHERE party_tenant_id = $1"


@dataclass(frozen=True)
class Entry:
    """One table entry of the plan: ``where`` selects the rows it changes (``$1`` = tenant id), ``version`` is their
    row version column (``None`` when the table has no ``updated_at``), ``sets`` the redaction."""

    table: str
    action: str
    where: str
    version: str | None
    sets: str

    @property
    def update(self) -> str:
        return f"UPDATE {self.table} SET {self.sets} WHERE {self.where}"  # noqa: S608 — constant entry SQL


_AUDIT_LEFT = (
    f"(before IS NOT NULL AND before <> {REDACTED_JSON}) OR (after IS NOT NULL AND after <> {REDACTED_JSON})"
    f" OR entity_label IS NOT NULL OR (summary IS NOT NULL AND summary <> '{REDACTED}')"
)

ENTRIES: tuple[Entry, ...] = (
    Entry(
        "booking.trip_cases", "redact",
        "channel = 'b2b' AND party_tenant_id = $1 AND (notes IS NOT NULL OR inquiry_snapshot IS NOT NULL)",
        "updated_at", "notes = NULL, inquiry_snapshot = NULL",
    ),
    Entry(
        "booking.trip_case_status_history", "redact", f"trip_case_id IN ({CASES}) AND note IS NOT NULL", None,
        "note = NULL",
    ),
    Entry(
        "booking.supplier_bookings", "redact",
        f"trip_case_id IN ({CASES}) AND (notes IS NOT NULL OR voucher_note IS NOT NULL"
        f" OR cancel_reason <> '{REDACTED}')",
        "updated_at",
        f"notes = NULL, voucher_note = NULL, cancel_reason = CASE WHEN cancel_reason IS NULL THEN NULL"
        f" ELSE '{REDACTED}' END",
    ),
    Entry(
        "booking.trip_case_checks", "redact",
        f"trip_case_id IN ({CASES}) AND (note IS NOT NULL OR emergency_contact_name IS NOT NULL"
        f" OR emergency_contact_phone IS NOT NULL OR (code = 'custom' AND title <> '{REDACTED}'))",
        "updated_at",
        "emergency_contact_name = NULL, emergency_contact_phone = NULL, note = NULL,"
        " cleared_at = coalesce(cleared_at, now()),"
        f" title = CASE WHEN code = 'custom' THEN '{REDACTED}' ELSE title END",
    ),
    Entry(
        "booking.b2b_cancellations", "redact",
        f"partner_id IN ({PARTNER}) AND waive_reason IS NOT NULL AND waive_reason <> '{REDACTED}'", None,
        f"waive_reason = '{REDACTED}'",
    ),
    Entry(
        "booking.agency_invoice", "redact",
        f"party_tenant_id = $1 AND (staff_note IS NOT NULL OR void_reason <> '{REDACTED}')", "updated_at",
        f"staff_note = NULL, void_reason = CASE WHEN void_reason IS NULL THEN NULL ELSE '{REDACTED}' END",
    ),
    Entry(
        "booking.agency_payment", "redact",
        f"invoice_id IN ({INVOICES}) AND (note IS NOT NULL OR reject_reason <> '{REDACTED}')", "updated_at",
        f"note = NULL, reject_reason = CASE WHEN reject_reason IS NULL THEN NULL ELSE '{REDACTED}' END",
    ),
    Entry(
        "booking.notifications", "redact",
        f"entity_id IN ({SCOPE}) AND (title <> '{REDACTED}' OR message IS NOT NULL"
        " OR data - 'reminder_key' <> '{}'::jsonb)",
        None,
        f"title = '{REDACTED}', message = NULL, data = CASE WHEN data ? 'reminder_key'"
        " THEN jsonb_build_object('reminder_key', data->'reminder_key') ELSE '{}'::jsonb END",
    ),
    Entry(
        "booking.b2b_partners", "anonymise",
        "tenant_id = $1 AND (agency_name <> 'Erased agency ' || left($1::text, 8) OR status <> 'suspended')",
        "updated_at", "agency_name = 'Erased agency ' || left($1::text, 8), status = 'suspended'",
    ),
    # Last: the redaction of the audit rows of the footprint (the entries above write no audit row of their own).
    Entry(
        "booking.audit_logs", "redact", f"entity_id IN ({SCOPE}) AND ({_AUDIT_LEFT})", None,
        f"before = CASE WHEN before IS NULL THEN NULL ELSE {REDACTED_JSON} END,"
        f" after = CASE WHEN after IS NULL THEN NULL ELSE {REDACTED_JSON} END,"
        f" entity_label = NULL, summary = CASE WHEN summary IS NULL THEN NULL ELSE '{REDACTED}' END",
    ),
)  # fmt: skip

# Kept by rule (D3, DM-7.1): counted as action keep (no digest), never written.
KEPT: tuple[tuple[str, str], ...] = (
    ("booking.b2b_quotes", f"partner_id IN ({PARTNER})"),
    ("booking.b2b_holds", f"partner_id IN ({PARTNER})"),
    ("booking.b2b_desk_bookings", f"partner_id IN ({PARTNER})"),
    ("booking.b2b_cancellations", f"partner_id IN ({PARTNER})"),
    ("booking.departure_fees", f"partner_id IN ({PARTNER})"),
    ("booking.agency_invoice", "party_tenant_id = $1"),
    ("booking.agency_invoice_reminder", f"invoice_id IN ({INVOICES})"),
    ("booking.agency_payment", f"invoice_id IN ({INVOICES})"),
)

# ── The census (verify) ──────────────────────────────────────────────────────────────────────────────────────────
# Per table: the tenant's rows, and per pii: column the erasure clears, the condition "still holds a value".
CENSUS: dict[str, tuple[str, dict[str, str]]] = {
    "booking.trip_cases": (
        "channel = 'b2b' AND party_tenant_id = $1",
        {"notes": "notes IS NOT NULL", "inquiry_snapshot": "inquiry_snapshot IS NOT NULL"},
    ),
    "booking.trip_case_status_history": (f"trip_case_id IN ({CASES})", {"note": "note IS NOT NULL"}),
    "booking.supplier_bookings": (
        f"trip_case_id IN ({CASES})",
        {"notes": "notes IS NOT NULL", "voucher_note": "voucher_note IS NOT NULL",
         "cancel_reason": f"cancel_reason <> '{REDACTED}'"},
    ),
    "booking.trip_case_checks": (
        f"trip_case_id IN ({CASES})",
        {"note": "note IS NOT NULL", "emergency_contact_name": "emergency_contact_name IS NOT NULL",
         "emergency_contact_phone": "emergency_contact_phone IS NOT NULL",
         "title": f"code = 'custom' AND title <> '{REDACTED}'"},
    ),
    "booking.b2b_cancellations": (f"partner_id IN ({PARTNER})", {"waive_reason": f"waive_reason <> '{REDACTED}'"}),
    "booking.agency_invoice": (
        "party_tenant_id = $1",
        {"staff_note": "staff_note IS NOT NULL", "void_reason": f"void_reason <> '{REDACTED}'"},
    ),
    "booking.agency_payment": (
        f"invoice_id IN ({INVOICES})",
        {"note": "note IS NOT NULL", "reject_reason": f"reject_reason <> '{REDACTED}'"},
    ),
    "booking.notifications": (
        f"entity_id IN ({SCOPE})",
        {"title": f"title <> '{REDACTED}'", "message": "message IS NOT NULL",
         "data": "data - 'reminder_key' <> '{}'::jsonb"},
    ),
    "booking.audit_logs": (
        f"entity_id IN ({SCOPE})",
        {"before": f"before IS NOT NULL AND before <> {REDACTED_JSON}",
         "after": f"after IS NOT NULL AND after <> {REDACTED_JSON}",
         "entity_label": "entity_label IS NOT NULL",
         "summary": f"summary IS NOT NULL AND summary <> '{REDACTED}'"},
    ),
    "booking.b2b_partners": ("tenant_id = $1", {}),
}  # fmt: skip
# pii: columns of those tables that stay by rule, with the rule. Never checked by the census.
CENSUS_KEPT: dict[str, str] = {
    "booking.trip_cases.trip_style": "trip facts of a kept money record (accounting exception)",
    "booking.trip_cases.start_date": "trip facts of a kept money record (accounting exception)",
    "booking.trip_cases.end_date": "trip facts of a kept money record (accounting exception)",
    "booking.trip_cases.pax": "trip facts of a kept money record (accounting exception)",
    "booking.trip_cases.pax_adults": "trip facts of a kept money record (accounting exception)",
    "booking.trip_cases.pax_children": "trip facts of a kept money record (accounting exception)",
    "booking.agency_invoice.bill_to": "bill-to snapshot of an issued invoice (accounting exception, DM-7.1)",
    "booking.agency_payment.reference": "payment evidence (accounting exception, DM-7.1)",
    "booking.agency_payment.document_ref": "payment evidence (accounting exception, DM-7.1)",
    "booking.agency_invoice_reminder.sent_to": "dunning evidence kept with its invoice (DM-7.1)",
    "booking.b2b_desk_bookings.override_reason": "manual bk_owner run-script (DM-7.1, DSAR runbook section 3)",
    "booking.departure_fees.note": "manual bk_owner run-script (DM-7.1, DSAR runbook section 3)",
}
PII_COLUMNS_SQL = """
SELECT n.nspname || '.' || c.relname || '.' || a.attname AS col
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = a.attnum AND d.classoid = 'pg_class'::regclass
 WHERE n.nspname || '.' || c.relname = ANY($1::text[]) AND a.attnum > 0 AND NOT a.attisdropped
   AND d.description LIKE 'pii:%'
 ORDER BY 1
"""


def census_tables() -> list[str]:
    return sorted({e.table for e in ENTRIES} | {t for t, _ in KEPT} | set(CENSUS))


def census_sql(table: str) -> str | None:
    where, columns = CENSUS[table]
    if not columns:
        return None
    left = " OR ".join(f"({p})" for p in columns.values())
    return f"SELECT count(*)::int AS pii_rows FROM {table} WHERE {where} AND ({left})"  # noqa: S608 — constant census SQL


def digest_sql(entry: Entry) -> str:
    """Rows and digest of an entry: md5 of the sorted '<id>|<updated_at UTC ISO 8601 or empty>' lines (contract)."""
    version = (
        f"""coalesce(to_char({entry.version} AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), '')"""
        if entry.version
        else "''"
    )
    head = "SELECT count(*)::int AS n, md5(coalesce(string_agg(line, E'\\n' ORDER BY line COLLATE \"C\"), ''))"
    return (
        f"{head} AS digest FROM (SELECT id::text || '|' || {version} AS line FROM {entry.table} WHERE {entry.where}) s"  # noqa: S608
    )


def kept_sql(table: str, where: str) -> str:
    return f"SELECT count(*)::int FROM {table} WHERE {where}"  # noqa: S608 — constant


# ── Requests, reports and refusals ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Request:
    """One erasure request as Booking acts on it (a ``catalog.v_tenant_erasure_v1`` row, or a CLI run)."""

    request_id: uuid.UUID
    tenant_id: uuid.UUID
    round: int
    phase: str
    step_status: str = "active"
    not_before: datetime | None = None
    approved_plan_hash: str | None = None
    approval_expires_at: datetime | None = None
    is_pilot: bool = False
    is_synthetic: bool = False
    source: str = "cis"

    @classmethod
    def from_row(cls, row: Any) -> Request:
        return cls(
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            round=int(row["round"]),
            phase=row["phase"],
            step_status=row["step_status"],
            not_before=row["not_before"],
            approved_plan_hash=row["approved_plan_hash"],
            approval_expires_at=row["approval_expires_at"],
            is_pilot=bool(row["is_pilot"]),
            is_synthetic=row["is_synthetic"] is True,
        )


class Refused(RuntimeError):
    """A guard stopped the run (nothing written but the report)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def erased_name(tenant_id: uuid.UUID | str) -> str:
    return f"Erased agency {str(tenant_id)[:8]}"


def cli_request_id(tenant_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(CLI_NAMESPACE, f"booking-tenant-erase:{tenant_id}")


def is_prod(environment: str | None) -> bool:
    """Only an explicit ``prod`` lifts the synthetic-only rule (an unset or other value counts as Dev: fail closed)."""
    return (environment or "").strip().lower() == "prod"


def plan_hash(tenant_id: uuid.UUID, round_: int, tables: list[dict[str, Any]]) -> str:
    """Contract: sha256 hex of 'tenant.erasure.v1|<tenant>|<round>' and the sorted table lines, joined by newlines."""
    lines = sorted(f"{t['table']}|{t['action']}|{t['rows']}|{t['digest'] or ''}" for t in tables)
    return hashlib.sha256("\n".join([f"{CONTRACT}|{tenant_id}|{round_}", *lines]).encode()).hexdigest()


async def plan(conn: asyncpg.Connection, tenant_id: uuid.UUID, round_: int) -> dict[str, Any]:
    """DRY RUN: one table entry per (table, action) and the plan hash. Reads only."""
    tables: list[dict[str, Any]] = []
    for entry in ENTRIES:
        row = await conn.fetchrow(digest_sql(entry), tenant_id)
        tables.append({"table": entry.table, "action": entry.action, "rows": int(row["n"]), "digest": row["digest"]})
    for table, where in KEPT:
        rows = await conn.fetchval(kept_sql(table, where), tenant_id)
        tables.append({"table": table, "action": "keep", "rows": int(rows or 0), "digest": None})
    return {
        "tables": tables,
        "rows": sum(t["rows"] for t in tables if t["action"] != "keep"),
        "plan_hash": plan_hash(tenant_id, round_, tables),
    }


async def census(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> tuple[dict[str, int], list[str]]:
    """Verify: (Census, the pii: columns neither cleared nor kept by rule). A clean erasure is ({..., 0}, [])."""
    tagged = [r["col"] for r in await conn.fetch(PII_COLUMNS_SQL, census_tables())]
    cleared = {f"{t}.{c}" for t, (_, cols) in CENSUS.items() for c in cols}
    unlisted = [c for c in tagged if c not in cleared and c not in CENSUS_KEPT]
    found = 0
    for table in CENSUS:
        sql = census_sql(table)
        if sql is not None:
            found += int(await conn.fetchval(sql, tenant_id) or 0)
    return {"pii_columns_checked": len([c for c in tagged if c in cleared]), "pii_rows_found": found}, unlisted


INSERT_REPORT_SQL = """
INSERT INTO booking.tenant_erasure_log
    (request_id, tenant_id, round, mode, status, error_code, plan_hash, tables, blockers, census, source, job_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11, $12::uuid)
RETURNING report_id::text, reported_at
"""
REPORTED_SQL = """
SELECT DISTINCT request_id, round, mode FROM booking.tenant_erasure_log WHERE request_id = ANY($1::uuid[])
"""
DRY_RUN_REPORT_SQL = """
SELECT report_id::text FROM booking.tenant_erasure_log
 WHERE request_id = $1 AND round = $2 AND mode = 'dry_run' AND status = 'ok' AND plan_hash = $3
 ORDER BY reported_at DESC LIMIT 1
"""
APPLIED_SQL = """
SELECT report_id::text FROM booking.tenant_erasure_log
 WHERE request_id = $1 AND mode = 'apply' AND status = 'ok' LIMIT 1
"""
ERASED_TENANTS_SQL = "SELECT DISTINCT tenant_id FROM booking.tenant_erasure_log WHERE mode = 'apply' AND status = 'ok'"


async def log_ready(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval(LOG_TABLE_SQL))


async def erased_tenants(conn: asyncpg.Connection) -> set[uuid.UUID]:
    """The tenants Booking erased (an apply report of status ok); empty before bk-057."""
    if not await log_ready(conn):
        return set()
    return {r["tenant_id"] for r in await conn.fetch(ERASED_TENANTS_SQL)}


async def write_report(
    conn: asyncpg.Connection,
    req: Request,
    mode: str,
    status: str,
    *,
    error_code: str | None = None,
    plan_hash_: str | None = None,
    tables: list[dict[str, Any]] | None = None,
    blockers: list[dict[str, Any]] | None = None,
    census_: dict[str, int] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        INSERT_REPORT_SQL,
        req.request_id,
        req.tenant_id,
        req.round,
        mode,
        status,
        error_code,
        plan_hash_,
        json.dumps(tables or []),
        json.dumps(blockers or []),
        json.dumps(census_) if census_ is not None else None,
        req.source,
        job_id,
    )
    return {
        "report_id": row["report_id"] if row else None,
        "request_id": str(req.request_id),
        "tenant_id": str(req.tenant_id),
        "app": APP,
        "round": req.round,
        "mode": mode,
        "status": status,
        "error_code": error_code,
        "plan_hash": plan_hash_,
        "tables": tables or [],
        "blockers": blockers or [],
        "census": census_,
        "job_id": job_id,
    }


async def _account_says_pilot(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> bool:
    """True when the CIS account view still holds the tenant and says it is the pilot (a second opinion; the view may
    no longer hold an erased tenant, which is not a refusal here)."""
    try:
        if not await liveness.view_ready(conn, ACCOUNT_VIEW):
            return False
        row = await conn.fetchrow(ACCOUNT_SQL, tenant_id)
    except (asyncpg.PostgresError, OSError):
        return False
    return bool(row and row["is_pilot"])


def _touched(status: str) -> int | None:
    match = re.fullmatch(r"UPDATE (\d+)", status or "")
    return int(match.group(1)) if match else None


async def _apply(conn: asyncpg.Connection, req: Request, *, job_id: str | None) -> dict[str, Any]:
    approved = req.approved_plan_hash or ""
    opening = await conn.fetchval(DRY_RUN_REPORT_SQL, req.request_id, req.round, approved)
    if opening is None:
        raise Refused("dry_run_missing", "no ok DRY RUN of this round has the approved hash")
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{KIND}:{req.tenant_id}")
        planned = await plan(conn, req.tenant_id, req.round)
        if planned["plan_hash"] != approved:
            changed = planned
        else:
            changed = None
            await conn.execute("SELECT set_config($1, $2, true)", ERASE_SETTING, opening)
            tables = []
            for entry, planned_entry in zip(ENTRIES, planned["tables"], strict=False):
                touched = _touched(await conn.execute(entry.update, req.tenant_id))
                if touched is not None and touched != planned_entry["rows"]:
                    raise RuntimeError(f"{entry.table}: touched {touched} rows, planned {planned_entry['rows']}")
                tables.append({**planned_entry, "rows": planned_entry["rows"] if touched is None else touched})
            tables += [t for t in planned["tables"] if t["action"] == "keep"]
            await audit.record(
                conn,
                actor_type="system",
                actor_ref=ACTOR,
                action="tenant.erased",
                entity_type="tenant",
                entity_id=str(req.tenant_id),
                after={
                    "request_id": str(req.request_id),
                    "round": req.round,
                    "plan_hash": approved,
                    "rows": planned["rows"],
                },  # fmt: skip
                entity_label=erased_name(req.tenant_id),
                summary="Tenant erased in Booking: free text redacted, agency name replaced; money rows and the "
                "invoice bill-to kept (accounting exception)",
            )
            report = await write_report(conn, req, "apply", "ok", plan_hash_=approved, tables=tables, job_id=job_id)
    if changed is not None:
        return await write_report(
            conn, req, "apply", "refused", error_code="plan_changed", plan_hash_=changed["plan_hash"],
            tables=changed["tables"], job_id=job_id,
        )  # fmt: skip
    log.info("bk_tenant_erase_applied tenant=%s request=%s rows=%d", req.tenant_id, req.request_id, planned["rows"])
    return report


def guard(req: Request, mode: str, *, prod: bool, apply_enabled: bool, now: datetime) -> None:
    """Raise :class:`Refused` unless ``mode`` may run for ``req`` here (D8; fail closed)."""
    if req.not_before is not None and req.not_before > now:
        raise Refused("not_due", "the step's not_before has not passed")
    if req.is_pilot:
        raise Refused("pilot_refused", "the pilot tenant is never erased (root ADR 0019 d16, AA-1093 D8)")
    if mode != "apply":
        return
    if not apply_enabled:
        raise Refused("apply_disabled", "TENANT_ERASE_APPLY_ENABLED is off")
    if not prod and not req.is_synthetic:
        raise Refused("not_synthetic", "outside prod only a synthetic tenant is erased (AA-1093 D8)")
    if not HASH_RE.match(req.approved_plan_hash or ""):
        raise Refused("approval_missing", "no approved Booking plan hash for this round")
    if req.source == "cis" and (req.approval_expires_at is None or req.approval_expires_at <= now):
        raise Refused("approval_expired", "the approval of this round has expired")


async def run_mode(
    conn: asyncpg.Connection,
    req: Request,
    mode: str,
    *,
    prod: bool,
    apply_enabled: bool,
    job_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one mode for one request and append its report (every outcome is a report, never a silent skip)."""
    now = now or datetime.now(UTC)
    try:
        guard(req, mode, prod=prod, apply_enabled=apply_enabled, now=now)
        if mode == "apply":
            if await conn.fetchval(APPLIED_SQL, req.request_id) is not None:
                return {"outcome": "already_applied", "request_id": str(req.request_id)}
            if await _account_says_pilot(conn, req.tenant_id):
                raise Refused("pilot_refused", "the CIS account view says the tenant is the pilot")
            return await _apply(conn, req, job_id=job_id)
        if mode == "dry_run":
            planned = await plan(conn, req.tenant_id, req.round)
            return await write_report(
                conn, req, "dry_run", "ok", plan_hash_=planned["plan_hash"], tables=planned["tables"], job_id=job_id
            )
        planned = await plan(conn, req.tenant_id, req.round)
        found, unlisted = await census(conn, req.tenant_id)
        left = planned["rows"]
        if unlisted:
            status, code = "failed", "census_incomplete"
        elif found["pii_rows_found"] or left:
            status, code = "failed", "census_not_zero"
        else:
            status, code = "ok", None
        blockers = [{"code": "pii_column_unlisted", "count": len(unlisted)}] if unlisted else []
        if unlisted:
            log.warning("bk_tenant_erase_census_unlisted columns=%s", ",".join(unlisted))
        return await write_report(
            conn, req, "verify", status, error_code=code, tables=planned["tables"], blockers=blockers,
            census_=found, job_id=job_id,
        )  # fmt: skip
    except Refused as exc:
        return await write_report(conn, req, mode, "refused", error_code=exc.code, job_id=job_id)
    except (asyncpg.PostgresError, RuntimeError) as exc:
        log.exception("bk_tenant_erase_failed request=%s mode=%s (%s)", req.request_id, mode, type(exc).__name__)
        return await write_report(conn, req, mode, "failed", error_code="internal_error", job_id=job_id)


# ── The job and its schedule ─────────────────────────────────────────────────────────────────────────────────────


async def read_request(conn: asyncpg.Connection, request_id: uuid.UUID) -> Request | None:
    if not await liveness.view_ready(conn, REQUEST_VIEW):
        return None
    row = await conn.fetchrow(REQUEST_SQL, request_id)
    return Request.from_row(row) if row else None


def build_handler(*, apply_enabled: bool, environment: str) -> Callable[[asyncpg.Pool, Job], Any]:
    async def handle(pool: asyncpg.Pool, job: Job) -> dict[str, Any]:
        return await run_job(pool, job, apply_enabled=apply_enabled, environment=environment)

    return handle


async def run_job(pool: asyncpg.Pool, job: Job, *, apply_enabled: bool, environment: str) -> dict[str, Any]:
    """Payload ``{request_id, tenant_id, round, mode}``. The request is read again (the newest view row); a job whose
    round or phase moved on writes no report. A refusal or a failure is a report, not a retry."""
    p = job.payload
    try:
        request_id, tenant_id = uuid.UUID(str(p["request_id"])), uuid.UUID(str(p["tenant_id"]))
        round_, mode = int(p["round"]), str(p["mode"])
    except (KeyError, TypeError, ValueError):
        return {"outcome": "invalid_payload"}
    if mode not in MODES:
        return {"outcome": "invalid_payload", "mode": mode}
    async with pool.acquire() as conn:
        if not await log_ready(conn) or not await conn.fetchval(KEY_SQL, REQUIRED_KEY):
            return {"outcome": "skipped", "reason": f"{REQUIRED_KEY} not applied"}
        req = await read_request(conn, request_id)
        if req is None:
            return {"outcome": "skipped", "reason": "request_not_found"}
        if req.tenant_id != tenant_id or req.round != round_ or req.phase != mode or req.step_status != "active":
            return {"outcome": "superseded", "round": req.round, "phase": req.phase, "step_status": req.step_status}
        report = await run_mode(conn, req, mode, prod=is_prod(environment), apply_enabled=apply_enabled, job_id=job.id)
    summary = {k: v for k, v in report.items() if k != "tables"}
    return {"outcome": report.get("status", report.get("outcome")), **summary}


def schedule_key(at: datetime) -> int:
    return int(at.timestamp()) // SLOT_SECONDS


class EraseSchedule:
    """Worker maintenance hook: once per :data:`SLOT_SECONDS` slot, while ``catalog.v_tenant_erasure_v1`` is readable
    and bk-057 is applied, queue one job per active Booking step past its not_before and its (round, phase) that has
    no report yet (phase ``apply`` only with an approved hash). The job applies the guards and reports."""

    def __init__(self, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
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
            requests = [Request.from_row(r) for r in await conn.fetch(REQUESTS_SQL)]
            reported = {
                (r["request_id"], r["round"], r["mode"])
                for r in await conn.fetch(REPORTED_SQL, [r.request_id for r in requests])
            }
        queued: list[str] = []
        for r in requests:
            if (r.request_id, r.round, r.phase) in reported:
                continue
            if r.phase == "apply" and not HASH_RE.match(r.approved_plan_hash or ""):
                continue
            payload = {"request_id": str(r.request_id), "tenant_id": str(r.tenant_id), "round": r.round,
                       "mode": r.phase}  # fmt: skip
            job_id = await queue.enqueue(
                pool, KIND, payload, max_attempts=MAX_ATTEMPTS, created_by=SCHEDULER,
                idempotency_key=f"{KIND}:{r.request_id}:{r.round}:{r.phase}",
            )  # fmt: skip
            if job_id is not None:
                queued.append(job_id)
        return queued


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────────────────────


async def cli_request(conn: asyncpg.Connection, tenant_id: uuid.UUID, round_: int, mode: str,
                      expected: str | None) -> Request:  # fmt: skip
    """The CLI's request: its own request id; pilot / synthetic from ``catalog.v_tenant_account_v1``. An unreadable
    view or a tenant it does not hold counts as the pilot for an apply or a verification (fail closed); a DRY RUN of
    such a tenant still shows its plan. The pilot itself is refused in every mode."""
    account = None
    try:
        if await liveness.view_ready(conn, ACCOUNT_VIEW):
            account = await conn.fetchrow(ACCOUNT_SQL, tenant_id)
    except (asyncpg.PostgresError, OSError):
        account = None
    return Request(
        request_id=cli_request_id(tenant_id),
        tenant_id=tenant_id,
        round=round_,
        phase=mode,
        approved_plan_hash=expected,
        is_pilot=bool(account and account["is_pilot"]) or (account is None and mode != "dry_run"),
        is_synthetic=bool(account and account["is_synthetic"] is True),
        source="cli",
    )


async def _main(tenant: str, mode: str, expected: str | None, round_: int) -> int:
    try:
        tenant_id = uuid.UUID(tenant)
    except ValueError:
        print(json.dumps({"error": "--tenant must be a UUID"}))
        return 2
    if mode == "apply" and not expected:
        print(json.dumps({"error": "--mode apply needs --expect-plan <plan_hash of the DRY RUN>"}))
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(json.dumps({"error": "DATABASE_URL not set"}))
        return 2
    conn = await asyncpg.connect(
        dsn, ssl=os.environ.get("DB_SSL", "require"), timeout=10,
        server_settings={"application_name": "booking-tenant-erase"},
    )  # fmt: skip
    try:
        if not await conn.fetchval(KEY_SQL, REQUIRED_KEY):
            print(json.dumps({"error": f"{REQUIRED_KEY} is not applied"}))
            return 3
        req = await cli_request(conn, tenant_id, round_, mode, expected)
        report = await run_mode(conn, req, mode, prod=is_prod(os.environ.get("ENVIRONMENT")), apply_enabled=True)
    finally:
        await conn.close()
    print(json.dumps(report, indent=1, default=str))
    return 0 if report.get("status") == "ok" or report.get("outcome") == "already_applied" else 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anonymise one agency tenant in Booking (DRY RUN by default).")
    parser.add_argument("--tenant", required=True, help="the CIS tenant id of the agency")
    parser.add_argument("--mode", choices=MODES, default="dry_run", help="dry_run (default), apply or verify")
    parser.add_argument("--expect-plan", default=None, help="with --mode apply: the DRY RUN's plan_hash (required)")
    parser.add_argument("--round", type=int, default=1, help="the round of the CLI request (default 1)")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.tenant, args.mode, args.expect_plan, args.round))


if __name__ == "__main__":
    sys.exit(main())
