"""In-app job checks (S263 alert diet, AA-1025): findings from a backlog sample, the once-a-day ledger dedupe and the
SNS publish. The ledger SQL runs against Postgres in tests/integration/test_job_metrics.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.jobs import ops_checks, queue
from app.jobs.metrics import Sample
from app.jobs.ops_checks import AlertPublisher, Finding, alert_key, findings, render

NOW = datetime(2026, 11, 1, 23, 59, tzinfo=UTC)


def test_thresholds_are_the_old_alarms() -> None:
    assert findings(Sample(900.0, 0, 0, 900.0)) == []
    found = findings(Sample(901.0, 2, 3, 901.0))
    assert [f.check for f in found] == ["jobs_stuck", "jobs_overdue"]
    assert found[0].details == {"oldest_queued_age_s": 901, "running": 3}
    assert found[1].details == {"overdue_running": 2, "running": 3}


def test_alert_key_is_per_check_and_utc_day() -> None:
    assert alert_key("jobs_stuck", NOW) == "bk_ops_alert:jobs_stuck:2026-11-01"
    assert ops_checks.OPS_ALERT_KIND.startswith("bk_")  # booking.job job_kind_chk


def test_render_subject_is_short_ascii() -> None:
    subject, text = render(findings(Sample(1200.0, 0, 0, 1200.0))[0], "dev")
    assert subject == "[AA-Booking dev] Ops check: Job queue stuck"
    assert len(subject) <= 100 and subject.isascii()
    assert "1200 s" in text and "oldest_queued_age_s: 1200" in text


def test_sns_client_short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    seen: dict[str, Any] = {}

    def fake_client(service: str, **kw: Any) -> str:
        seen.update(kw, service=service)
        return "client"

    monkeypatch.setattr(boto3, "client", fake_client)
    assert ops_checks.sns_client() == "client"
    cfg = seen["config"]
    assert (seen["service"], seen["region_name"]) == ("sns", "us-west-1")
    assert (cfg.connect_timeout, cfg.read_timeout, cfg.retries) == (3, 5, {"max_attempts": 1})


class FakeSns:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def publish(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if self.error:
            raise self.error
        return {"MessageId": "m-1"}


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"keys": {}, "forgotten": [], "error": None}

    async def record(pool: Any, kind: str, key: str, payload: dict[str, Any]) -> str | None:
        if state["error"]:
            raise state["error"]
        if key in state["keys"]:
            return None
        state["keys"][key] = (kind, payload)
        return f"id-{len(state['keys'])}"

    async def forget(pool: Any, job_id: str) -> None:
        state["forgotten"].append(job_id)

    monkeypatch.setattr(queue, "record_alert", record)
    monkeypatch.setattr(queue, "forget_alert", forget)
    return state


FOUND = [Finding("jobs_stuck", "Job queue stuck", "waited 1200 s", "check the worker", {"x": 1})]


@pytest.mark.asyncio
async def test_sent_once_per_day(ledger: dict[str, Any]) -> None:
    sns = FakeSns()
    pub = AlertPublisher("topic-placeholder", environment="dev", client_factory=lambda: sns)
    assert await pub.alert(object(), FOUND, NOW) == {"jobs_stuck": "sent"}  # type: ignore[arg-type]
    assert await pub.alert(object(), FOUND, NOW) == {"jobs_stuck": "already_today"}  # type: ignore[arg-type]
    assert len(sns.calls) == 1 and sns.calls[0]["TopicArn"] == "topic-placeholder"
    kind, payload = ledger["keys"]["bk_ops_alert:jobs_stuck:2026-11-01"]
    assert kind == "bk_ops_alert" and payload["summary"] == "waited 1200 s"


@pytest.mark.asyncio
async def test_failed_publish_forgets_its_ledger_row(ledger: dict[str, Any]) -> None:
    pub = AlertPublisher("topic-placeholder", client_factory=lambda: FakeSns(PermissionError("AccessDenied")))
    assert await pub.alert(object(), FOUND, NOW) == {"jobs_stuck": "failed"}  # type: ignore[arg-type]
    assert ledger["forgotten"] == ["id-1"]


@pytest.mark.asyncio
async def test_ledger_failure_is_failed_not_raised(ledger: dict[str, Any]) -> None:
    ledger["error"] = ConnectionError("db down")
    pub = AlertPublisher("topic-placeholder", client_factory=FakeSns)
    assert await pub.alert(object(), FOUND, NOW) == {"jobs_stuck": "failed"}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_no_topic_logs_only(ledger: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    pub = AlertPublisher("", client_factory=FakeSns)
    assert not pub.configured
    assert await pub.alert(object(), FOUND, NOW) == {"jobs_stuck": "not_configured"}  # type: ignore[arg-type]
    assert ledger["keys"] == {} and "ALERTS_TOPIC_ARN unset" in caplog.text


@pytest.mark.asyncio
async def test_forget_failure_is_logged(ledger: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken_forget(pool: Any, job_id: str) -> None:
        raise ConnectionError("db down")

    monkeypatch.setattr(queue, "forget_alert", broken_forget)
    pub = AlertPublisher("topic-placeholder", client_factory=lambda: FakeSns(RuntimeError("throttled")))
    assert await pub.alert(object(), FOUND, NOW) == {"jobs_stuck": "failed"}  # type: ignore[arg-type]
