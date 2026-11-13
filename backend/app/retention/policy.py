"""AA-825 — Booking's retention policies (data standards §7 retention table, windows approved in E3; DM-6.6, DM-7).

Two owner-run TTL job kinds on ``booking.job`` (ADR 0003a decision 5, DM-7.2):

``bk_retention_job``
    Finished jobs, each batch archived to S3 before it is deleted (AA-825 "archive each batch to S3 before delete";
    review S251): ``succeeded`` 30 days after ``finished_at``, ``failed`` / ``cancelled`` 90 days after
    ``finished_at``. A failed archive put deletes nothing (same transaction). ``queued`` / ``running`` jobs are never
    touched (the status filter).

``bk_retention_inbox_redact``
    ``booking.inbox_events`` rows received more than 90 days ago get ``payload`` rebuilt from an allowlist of the
    contract's non-personal keys (customer name, email, phone and custom day notes nulled; every unknown key dropped,
    see ``redact.py``) and lose a failed row's ``error`` text (DM-6.6). The row, ``idempotency_key`` and
    ``contract_version`` stay, so the pull's dedupe keeps working; ``redacted_at`` (bk-012) marks the row. Rows still
    ``received`` (not processed yet) are never redacted: they are counted as ``skipped_unprocessed`` instead. Nothing
    is deleted, so nothing is archived (DM-7.3 "archive after redaction" applies when a delete window is set).

Changing a window is a DM-7.5 decision and makes the next scheduled run a DRY RUN again (the job compares the windows
with the last live run). S279 L3 (bk-056): the windows are pricing policy parameters (Pricing > Policy, approver group
``admin``, maker-checker, versioned): ``retention_job_succeeded_days`` (30), ``retention_job_failed_days`` (90) and
``retention_inbox_redact_days`` (90). A :class:`Part` names its key; :func:`resolve` reads the values in force on the
run's UTC day (no fallback in code: a run without them fails, the status page answers 503).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import asyncpg

from app.pilot import uuid_array_literal
from app.pricing import policy_values

JOB_KIND = "bk_retention_job"
INBOX_KIND = "bk_retention_inbox_redact"

# Archive location: the CIS archive bucket (AA-825, CIS ``services/retention/policy.py``), under a Booking-only
# prefix so the task role can be granted ``s3:PutObject`` on ``archive/booking/*`` alone. Objects are
# ``archive/booking/<table>/<yyyy>/<mm>/<job_id>-<run>-<part>-b<NNNNN>.jsonl.gz``.
# The bucket name ends with the acc2 account ID (``aa-cis-bronze-<account>``). This repo is public, so the account
# is not written here (AA-902): ``archive_bucket`` builds the name from the account the task runs in (STS, in
# ``app.retention.jobs.default_bucket``). A live run that archives records that real bucket in its result; the summary
# and dry runs show ``ARCHIVE_BUCKET_DISPLAY``. AA-998 rule R1: admin pages (the audit log renders run results) never
# print an account label such as "acc2", so the placeholder names the account ID generically.
ARCHIVE_BUCKET_STEM = "aa-cis-bronze"
ARCHIVE_BUCKET_DISPLAY = f"{ARCHIVE_BUCKET_STEM}-<account-id>"
ARCHIVE_REGION = "us-west-1"
ARCHIVE_PREFIX = "archive/booking"


def archive_bucket(account_id: str) -> str:
    """The archive bucket of an AWS account: ``aa-cis-bronze-<12-digit account ID>``."""
    if len(account_id) != 12 or not account_id.isdigit():
        raise ValueError("an AWS account ID has 12 digits")
    return f"{ARCHIVE_BUCKET_STEM}-{account_id}"


@dataclass(frozen=True)
class Part:
    """Rows of the spec's table whose retention column is older than ``window_days`` and that match ``where``.
    ``window_days`` is the pricing policy parameter ``window_key`` in force (0 until :func:`resolve` sets it)."""

    name: str
    window_key: str  # pricing policy parameter (app.pricing.policy_params, section "retention")
    where: str  # predicate on alias ``t``, no bind parameters
    archive: bool = False
    window_days: int = 0


@dataclass(frozen=True)
class Spec:
    kind: str
    table: str  # schema-qualified
    archive_name: str  # S3 prefix segment under ARCHIVE_PREFIX
    ts_col: str  # retention column; (ts_col, id) is indexed (bk-012)
    action: str  # 'delete' | 'redact'
    parts: tuple[Part, ...]
    # AA-969 (root ADR 0019 d16): a predicate on alias ``t`` that leaves the pilot tenant's rows out; ``{pilots}`` is
    # replaced at run time by a validated uuid[] literal (app.pilot.uuid_array_literal). Empty = the table holds no
    # tenant data.
    pilot_where: str = ""

    def windows(self) -> dict[str, int]:
        return {p.name: p.window_days for p in self.parts}

    def window_keys(self) -> tuple[str, ...]:
        return tuple(p.window_key for p in self.parts)


JOB_SPEC = Spec(
    kind=JOB_KIND,
    table="booking.job",
    archive_name="job",
    ts_col="finished_at",
    action="delete",
    parts=(
        Part("succeeded", "retention_job_succeeded_days", "t.status = 'succeeded'", archive=True),
        Part("failed", "retention_job_failed_days", "t.status IN ('failed', 'cancelled')", archive=True),
    ),
)

INBOX_SPEC = Spec(
    kind=INBOX_KIND,
    table="booking.inbox_events",
    archive_name="inbox_events",
    ts_col="received_at",
    action="redact",
    parts=(Part("payload_pii", "retention_inbox_redact_days", "t.redacted_at IS NULL AND t.status <> 'received'"),),
    pilot_where=(
        "NOT EXISTS (SELECT 1 FROM booking.trip_cases pc"
        " WHERE pc.inbox_event_id = t.id AND pc.party_tenant_id = ANY({pilots}))"
    ),
)

SPECS: dict[str, Spec] = {s.kind: s for s in (JOB_SPEC, INBOX_SPEC)}
KINDS: tuple[str, ...] = tuple(SPECS)


def with_windows(spec: Spec, values: Mapping[str, Any]) -> Spec:
    """``spec`` with each part's ``window_days`` from ``values`` (policy key -> whole days, at least 1)."""
    parts = []
    for part in spec.parts:
        days = values.get(part.window_key)
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            raise policy_values.PolicyUnavailable(f"{part.window_key}: no valid retention window ({days!r})")
        parts.append(replace(part, window_days=days))
    return replace(spec, parts=tuple(parts))


async def resolve(conn: asyncpg.Connection, spec: Spec, on: date) -> Spec:
    """``spec`` with the retention windows in force on ``on`` (pricing policy; PolicyUnavailable without one)."""
    policy = await policy_values.require(conn, on, *spec.window_keys())
    return with_windows(spec, {key: policy.integer(key) for key in spec.window_keys()})


def without_pilots(spec: Spec, pilots: list[Any]) -> Spec:
    """``spec`` with every part's predicate also leaving the pilot tenants' rows out (unchanged when there is no pilot
    or the table holds no tenant data)."""
    if not pilots or not spec.pilot_where:
        return spec
    guard = spec.pilot_where.format(pilots=uuid_array_literal(pilots))
    return replace(spec, parts=tuple(replace(p, where=f"({p.where}) AND {guard}") for p in spec.parts))


def spec_for(kind: str) -> Spec:
    try:
        return SPECS[kind]
    except KeyError:
        raise ValueError(f"unknown retention kind {kind!r}; one of {sorted(SPECS)}") from None
