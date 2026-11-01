"""In-app job checks that replaced the ``booking.job`` CloudWatch alarms (S263 alert diet, AA-1025).

Until S263 the job sampler (:mod:`app.jobs.metrics`) published two custom metrics to CloudWatch and AA-CIS-Infra
alarmed on them (``<booking prefix>-jobs-queue-age``, ``-jobs-overdue-running``, ``-jobs-sampling-stopped``). The
values are now checked here, from the same database read, at $0:

- ``jobs_stuck``: a due queued row has waited more than :data:`STUCK_AFTER_S` (900 s) while no healthy job runs (the
  sampler's capacity rule for a serial worker);
- ``jobs_overdue``: a running row is past 2 x its kind's expected seconds.

A finding is published to the platform alerts SNS topic (env ``ALERTS_TOPIC_ARN``; the topic the removed alarms used,
so the recipients are the same) at most **once per check per UTC day**. The dedupe is a ledger row in ``booking.job``:
kind :data:`OPS_ALERT_KIND`, status ``succeeded``, idempotency key ``bk_ops_alert:<check>:<day>``
(:func:`app.jobs.queue.record_alert`). Only the task whose insert wins publishes, so two API tasks or a restart never
send twice; a failed publish deletes the row (:func:`app.jobs.queue.forget_alert`) so the next sample retries. The
rows are the history of sent alerts; no worker ever claims them (they are never queued).

The e-mail goes from the sampler, not through a job, on purpose: these checks are about the worker itself. The API
being down is the Infra alarm ``aa-booking-<env>-no-healthy-task``; the old "sampling stopped" alarm is gone.
Without ``ALERTS_TOPIC_ARN`` (local, tests) a finding is logged only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg

from app.jobs import queue

log = logging.getLogger(__name__)

OPS_ALERT_KIND = "bk_ops_alert"
STUCK_AFTER_S = 900
SNS_REGION = "us-west-1"  # acc2, where the alerts topic lives


@dataclass(frozen=True)
class Finding:
    check: str
    title: str
    summary: str
    action: str
    details: dict[str, Any] = field(default_factory=dict)


def findings(sample: Any) -> list[Finding]:
    """The findings of one backlog sample (:class:`app.jobs.metrics.Sample`). Pure."""
    out: list[Finding] = []
    if sample.oldest_queued_age_s > STUCK_AFTER_S:
        out.append(
            Finding(
                "jobs_stuck",
                "Job queue stuck",
                f"the oldest due booking.job row has waited {int(sample.oldest_queued_age_s)} s and no healthy job is"
                " running",
                "Check that the worker runs (WORKER_ENABLED, the bk_inquiry_pull row) and read the queued rows on the"
                " staff admin Jobs page and the ECS log.",
                {"oldest_queued_age_s": int(sample.oldest_queued_age_s), "running": sample.running},
            )
        )
    if sample.overdue_running:
        out.append(
            Finding(
                "jobs_overdue",
                "Job running too long",
                f"{sample.overdue_running} running booking.job row(s) past twice their expected time",
                "The serial worker is blocked behind it. Read the running row and the ECS log; cancel it if it hangs.",
                {"overdue_running": sample.overdue_running, "running": sample.running},
            )
        )
    return out


class SnsClient(Protocol):
    def publish(self, **kwargs: Any) -> dict[str, Any]: ...


def sns_client() -> SnsClient:
    import boto3  # lazy: only an alert needs it
    from botocore.config import Config

    return boto3.client(
        "sns", region_name=SNS_REGION, config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    )


def alert_key(check: str, now: datetime | None = None) -> str:
    day = (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()
    return f"{OPS_ALERT_KIND}:{check}:{day}"


def render(finding: Finding, environment: str) -> tuple[str, str]:
    """(subject, text). The subject stays under the SNS limit of 100 ASCII characters."""
    subject = f"[AA-Booking {environment}] Ops check: {finding.title}"[:100]
    lines = [f"{finding.title}: {finding.summary}.", "", finding.action, ""]
    lines += [f"{k}: {v}" for k, v in finding.details.items()]
    lines += [
        "",
        f"At most one e-mail per check and UTC day (check {finding.check}, an in-app check that replaced a CloudWatch"
        " alarm in S263).",
    ]
    return subject.encode("ascii", "replace").decode("ascii"), "\n".join(lines)


class AlertPublisher:
    """Publishes findings to the alerts topic once per check per UTC day (ledger row in booking.job)."""

    def __init__(
        self,
        topic_arn: str | None = None,
        *,
        environment: str | None = None,
        client_factory: Callable[[], SnsClient] = sns_client,
    ) -> None:
        self._topic_arn = (os.environ.get("ALERTS_TOPIC_ARN", "") if topic_arn is None else topic_arn).strip()
        self._environment = environment or os.environ.get("ENVIRONMENT", "dev")
        self._client_factory = client_factory
        self._client: SnsClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self._topic_arn)

    def _publish(self, subject: str, text: str) -> None:
        # Runs in a thread: creating the boto3 client reads credentials, so it is created here, once.
        if self._client is None:
            self._client = self._client_factory()
        self._client.publish(TopicArn=self._topic_arn, Subject=subject, Message=text)

    async def alert(self, pool: asyncpg.Pool, found: list[Finding], now: datetime | None = None) -> dict[str, str]:
        """{check: sent | already_today | not_configured | failed}. Never raises."""
        out: dict[str, str] = {}
        for f in found:
            if not self.configured:
                log.warning("ops_check_finding check=%s summary=%s (ALERTS_TOPIC_ARN unset)", f.check, f.summary)
                out[f.check] = "not_configured"
                continue
            try:
                payload = {"check": f.check, "summary": f.summary, "details": f.details}
                ledger_id = await queue.record_alert(pool, OPS_ALERT_KIND, alert_key(f.check, now), payload)
            except Exception as exc:  # noqa: BLE001 — the next sample tries again
                log.warning("ops_check_alert_failed check=%s stage=ledger error=%s", f.check, type(exc).__name__)
                out[f.check] = "failed"
                continue
            if ledger_id is None:
                out[f.check] = "already_today"
                continue
            subject, text = render(f, self._environment)
            try:
                await asyncio.to_thread(self._publish, subject, text)
            except Exception as exc:  # noqa: BLE001 — AccessDenied before the Infra apply, throttling, network
                log.warning("ops_check_alert_failed check=%s stage=publish error=%s", f.check, type(exc).__name__)
                try:
                    await queue.forget_alert(pool, ledger_id)
                except Exception as exc2:  # noqa: BLE001
                    log.warning("ops_check_alert_failed check=%s stage=forget error=%s", f.check, type(exc2).__name__)
                out[f.check] = "failed"
                continue
            log.warning("ops_check_alert check=%s summary=%s", f.check, f.summary)
            out[f.check] = "sent"
        return out
