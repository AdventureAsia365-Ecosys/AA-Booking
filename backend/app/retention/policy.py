"""AA-825 — Booking's retention policies (data standards §7 retention table, windows approved in E3; DM-6.6, DM-7).

Two owner-run TTL job kinds on ``booking.job`` (ADR 0003a decision 5, DM-7.2):

``bk_retention_job``
    Finished jobs: ``succeeded`` deleted 30 days after ``finished_at``; ``failed`` / ``cancelled`` archived to S3 and
    then deleted 90 days after ``finished_at``. ``queued`` / ``running`` jobs are never touched (the status filter).

``bk_retention_inbox_redact``
    ``booking.inbox_events`` rows received more than 90 days ago lose the personal data in ``payload`` (customer name,
    email, phone, custom day notes) and a failed row's ``error`` text (DM-6.6). The row, ``idempotency_key`` and
    ``contract_version`` stay, so the pull's dedupe keeps working; ``redacted_at`` (bk-012) marks the row. Rows still
    ``received`` (not processed yet) are never redacted: they are counted as ``skipped_unprocessed`` instead. Nothing
    is deleted, so nothing is archived (DM-7.3 "archive after redaction" applies when a delete window is set).

Changing a window is a DM-7.5 decision (Nghiệp approves) and makes the next scheduled run a DRY RUN again (the job
compares the windows with the last live run).
"""

from __future__ import annotations

from dataclasses import dataclass

JOB_KIND = "bk_retention_job"
INBOX_KIND = "bk_retention_inbox_redact"

# Archive location: the CIS archive bucket (AA-825, CIS ``services/retention/policy.py``), under a Booking-only
# prefix so the task role can be granted ``s3:PutObject`` on ``archive/booking/*`` alone. Objects are
# ``archive/booking/<table>/<yyyy>/<mm>/<job_id>-<run>-<part>-b<NNNNN>.jsonl.gz``.
ARCHIVE_BUCKET = "aa-cis-bronze-005097885195"
ARCHIVE_REGION = "us-west-1"
ARCHIVE_PREFIX = "archive/booking"


@dataclass(frozen=True)
class Part:
    """Rows of the spec's table whose retention column is older than ``window_days`` and that match ``where``."""

    name: str
    window_days: int
    where: str  # predicate on alias ``t``, no bind parameters
    archive: bool = False


@dataclass(frozen=True)
class Spec:
    kind: str
    table: str  # schema-qualified
    archive_name: str  # S3 prefix segment under ARCHIVE_PREFIX
    ts_col: str  # retention column; (ts_col, id) is indexed (bk-012)
    action: str  # 'delete' | 'redact'
    parts: tuple[Part, ...]

    def windows(self) -> dict[str, int]:
        return {p.name: p.window_days for p in self.parts}


JOB_SPEC = Spec(
    kind=JOB_KIND,
    table="booking.job",
    archive_name="job",
    ts_col="finished_at",
    action="delete",
    parts=(
        Part("succeeded", 30, "t.status = 'succeeded'"),
        Part("failed", 90, "t.status IN ('failed', 'cancelled')", archive=True),
    ),
)

INBOX_SPEC = Spec(
    kind=INBOX_KIND,
    table="booking.inbox_events",
    archive_name="inbox_events",
    ts_col="received_at",
    action="redact",
    parts=(Part("payload_pii", 90, "t.redacted_at IS NULL AND t.status <> 'received'"),),
)

SPECS: dict[str, Spec] = {s.kind: s for s in (JOB_SPEC, INBOX_SPEC)}
KINDS: tuple[str, ...] = tuple(SPECS)


def spec_for(kind: str) -> Spec:
    try:
        return SPECS[kind]
    except KeyError:
        raise ValueError(f"unknown retention kind {kind!r}; one of {sorted(SPECS)}") from None
