"""AA-871: JSON logs, request ids, PII scrubbing and Sentry init."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from unittest.mock import patch

import pytest
import sentry_sdk
import sentry_sdk.transport
import structlog
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.jobs import worker as worker_mod
from app.jobs.queue import Job
from app.main import create_app
from app.observability import log_config, request_id, sentry
from app.observability.scrub import FILTERED, scrub, scrub_text
from app.settings import Settings, SettingsError, load_settings

SETTINGS = Settings(database_url=None, environment="test", git_sha="abc1234")


@pytest.fixture
def json_lines():
    """Route the root logger through the JSON handler into a buffer; yields a reader of the parsed lines."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    buf = io.StringIO()
    log_config.configure_logging(SETTINGS, stream=buf)

    def read() -> list[dict]:
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

    yield read
    for h in list(root.handlers):
        if h.get_name() == log_config.HANDLER_NAME:
            root.removeHandler(h)
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)
    structlog.reset_defaults()


def _app_with_probe():
    app = create_app(SETTINGS)
    probe = APIRouter()

    @probe.get("/booking/_probe")
    async def _probe() -> dict:
        logging.getLogger("app.probe").info("probe %s", "hit")
        return {"ok": True}

    @probe.get("/booking/_boom")
    async def _boom() -> dict:
        raise RuntimeError("boom for a@b.example")

    app.include_router(probe)
    return app


# --- request id ---------------------------------------------------------------------------------------------------


def test_request_id_generated_and_echoed() -> None:
    with TestClient(create_app(SETTINGS)) as client:
        r = client.get("/booking/health")
    rid = r.headers["x-request-id"]
    assert len(rid) == 32 and int(rid, 16) >= 0


def test_incoming_request_id_is_kept() -> None:
    rid = "3f1c2a9e-8f0b-4c1d-9e2a-0b1c2d3e4f50"  # API Gateway context.requestId shape
    with TestClient(create_app(SETTINGS)) as client:
        r = client.get("/booking/health", headers={"X-Request-ID": rid})
    assert r.headers["x-request-id"] == rid


@pytest.mark.parametrize("bad", ["", "x" * 129, "a b", "id\r\nSet-Cookie: x", "<script>", "é"])
def test_malformed_request_id_is_replaced(bad: str) -> None:
    assert request_id.accept_request_id(bad) is None


def test_unhandled_500_keeps_the_request_id() -> None:
    with TestClient(_app_with_probe(), raise_server_exceptions=False) as client:
        r = client.get("/booking/_boom", headers={"X-Request-ID": "req-500"})
    assert r.status_code == 500
    assert r.text == "Internal Server Error"
    assert r.headers["x-request-id"] == "req-500"


# --- JSON logs ----------------------------------------------------------------------------------------------------


def test_every_line_of_a_request_is_json_with_its_request_id(json_lines) -> None:
    with TestClient(_app_with_probe()) as client:
        client.get("/booking/_probe", headers={"X-Request-ID": "req-1"})
    lines = [x for x in json_lines() if x.get("request_id") == "req-1"]
    events = [x["event"] for x in lines]
    assert "probe hit" in events  # an existing-style stdlib %-call
    done = next(x for x in lines if x["event"] == "request_completed")
    assert done["status"] == 200 and done["method"] == "GET" and done["path"] == "/booking/_probe"
    assert done["route"] == "/booking/_probe"
    assert isinstance(done["duration_ms"], float)
    for x in lines:
        assert x["service"] == "aa-booking-api" and x["git_sha"] == "abc1234" and x["environment"] == "test"
        assert {"timestamp", "level", "logger"} <= set(x)


def test_request_id_not_leaked_to_lines_outside_the_request(json_lines) -> None:
    with TestClient(_app_with_probe()) as client:
        client.get("/booking/_probe", headers={"X-Request-ID": "req-2"})
    logging.getLogger("app.after").info("after the request")
    after = next(x for x in json_lines() if x["event"] == "after the request")
    assert "request_id" not in after


def test_structlog_calls_render_the_same_way(json_lines) -> None:
    with structlog.contextvars.bound_contextvars(request_id="req-3"):
        structlog.get_logger("app.new").info("thing_done", count=2, email="x@y.example")
    line = next(x for x in json_lines() if x["event"] == "thing_done")
    assert line["request_id"] == "req-3" and line["count"] == 2 and line["email"] == FILTERED
    assert line["level"] == "info"


def test_log_lines_are_scrubbed(json_lines) -> None:
    log = logging.getLogger("app.pii")
    log.warning("lead %s phone %s auth %s", "jane@example.com", "+84 90 123 4567", "Bearer abc.def.ghijklmnop")
    try:
        raise ValueError("Key (email)=(jane@example.com) already exists")
    except ValueError:
        log.exception("insert failed")
    text = json.dumps(json_lines())
    assert "jane@example.com" not in text and "123 4567" not in text and "ghijklmnop" not in text
    assert "[email]" in text and "[phone]" in text


def test_uvicorn_access_log_is_replaced(json_lines) -> None:
    assert logging.getLogger("uvicorn.access").disabled is True
    assert logging.getLogger("uvicorn.error").propagate is True


# --- worker binds job ids -----------------------------------------------------------------------------------------


def test_worker_binds_job_id_and_kind(json_lines, monkeypatch) -> None:
    seen: dict = {}

    async def handler(pool, job):
        seen.update(structlog.contextvars.get_contextvars())
        logging.getLogger("app.handler").info("handler ran")
        return {"ok": True}

    async def complete(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(worker_mod.queue, "complete", complete)
    w = worker_mod.Worker(pool=None, handlers={"bk_test": handler})  # type: ignore[arg-type]
    job = Job("11111111-1111-1111-1111-111111111111", "bk_test", {}, 1, 3)
    asyncio.run(w._run(None, job))  # type: ignore[arg-type]
    assert seen == {"job_id": job.id, "job_kind": "bk_test"}
    line = next(x for x in json_lines() if x["event"] == "handler ran")
    assert line["job_id"] == job.id and line["job_kind"] == "bk_test"
    assert structlog.contextvars.get_contextvars() == {}


def test_failed_job_goes_to_sentry_retried_attempt_does_not() -> None:
    job = Job("j1", "bk_test", {}, 1, 3)
    with patch("sentry_sdk.capture_exception") as capture:
        worker_mod._report_failed(job, RuntimeError("x"), "queued")
        assert capture.call_count == 0
        worker_mod._report_failed(job, RuntimeError("x"), "failed")
        assert capture.call_count == 1


# --- scrubbing ----------------------------------------------------------------------------------------------------


def test_scrub_by_key_and_pattern() -> None:
    out = scrub(
        {
            "email": "a@b.example",
            "Phone": "0901234567",
            "name": "Jane Doe",
            "first_name": "Jane",
            "Authorization": "Bearer x",
            "x-api-key": "k",
            "access_token": "t",
            "password": "p",
            "request_id": "r1",
            "job_id": "j1",
            "count": 3,
            "note": "call +1 (415) 555-0100 or mail jane.doe+t@mail.example.co",
            "nested": [{"customer_name": "Jane"}, "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig"],
        }
    )
    for k in ("email", "Phone", "name", "first_name", "Authorization", "x-api-key", "access_token", "password"):
        assert out[k] == FILTERED, k
    assert out["request_id"] == "r1" and out["job_id"] == "j1" and out["count"] == 3
    assert out["note"] == "call [phone] or mail [email]"
    assert out["nested"] == [{"customer_name": FILTERED}, "[jwt]"]


def test_scrub_text_keeps_ids_and_counts() -> None:
    text = "inquiry pull: 12 new of 1200 scanned (safe_id=123456789) job 3f1c2a9e-8f0b-4c1d-9e2a-0b1c2d3e4f50"
    assert scrub_text(text) == text


def test_scrub_query_params_and_pg_detail() -> None:
    assert scrub_text("GET /x?api_key=SECRET&q=1") == "GET /x?api_key=***&q=1"
    assert scrub_text("Key (phone)=(+84901234567) already exists") == "Key (phone)=([Filtered]) already exists"


# --- Sentry -------------------------------------------------------------------------------------------------------


def test_sentry_is_a_no_op_without_dsn() -> None:
    with patch("sentry_sdk.init") as init:
        assert sentry.init_sentry(SETTINGS) is False
    init.assert_not_called()


def test_sentry_init_options_with_dsn() -> None:
    s = Settings(database_url=None, environment="dev", git_sha="deadbeef", sentry_dsn="https://k@o1.ingest.example/1")
    with patch("sentry_sdk.init") as init, patch("sentry_sdk.set_tag") as tag:
        assert sentry.init_sentry(s) is True
    kw = init.call_args.kwargs
    assert kw["dsn"] == "https://k@o1.ingest.example/1"
    assert kw["release"] == "deadbeef" and kw["environment"] == "dev"
    assert kw["send_default_pii"] is False and kw["include_local_variables"] is False
    assert kw["max_request_body_size"] == "never"
    assert kw["traces_sample_rate"] == 0.1 and kw["profiles_sample_rate"] == 0.1
    assert kw["before_send"] is sentry.before_send and kw["before_breadcrumb"] is sentry.before_breadcrumb
    assert kw["before_send_transaction"] is sentry.before_send_transaction
    tag.assert_called_with("service", "api")


def test_before_send_strips_pii() -> None:
    event = {
        "request": {
            "url": "https://api-booking.example/booking/customers?token=abc",
            "query_string": "email=a@b.example",
            "data": {"customer": {"email": "a@b.example"}},
            "cookies": {"s": "1"},
            "headers": {"Authorization": "Bearer abc", "X-Request-ID": "r1", "User-Agent": "ua", "X-Api-Key": "k"},
        },
        "user": {"email": "a@b.example", "ip_address": "1.2.3.4"},
        "exception": {
            "values": [
                {
                    "type": "UniqueViolationError",
                    "value": "Key (email)=(a@b.example) already exists",
                    "stacktrace": {"frames": [{"function": "f", "vars": {"row": {"name": "Jane"}}}]},
                }
            ]
        },
        "logentry": {"message": "lead %s", "params": ["a@b.example"]},
        "extra": {"phone": "+84901234567"},
        "tags": {"request_id": "r1", "note": "x@y.example"},
        "breadcrumbs": {"values": [{"message": "mail a@b.example", "data": {"url": "https://x/?api_key=K"}}]},
    }
    out = sentry.before_send(event, {})
    text = json.dumps(out)
    assert "a@b.example" not in text and "Jane" not in text and "+84901234567" not in text
    assert "Bearer abc" not in text and "api_key=K" not in text and "token=abc" not in text
    assert "user" not in out and "data" not in out["request"] and "cookies" not in out["request"]
    assert set(out["request"]["headers"]) == {"X-Request-ID", "User-Agent"}
    assert out["tags"]["request_id"] == "r1"
    assert "vars" not in out["exception"]["values"][0]["stacktrace"]["frames"][0]


def test_value_echoing_exception_messages_are_dropped() -> None:
    event = {
        "exception": {
            "values": [
                {
                    "type": "DataError",
                    "module": "asyncpg.exceptions",
                    "value": "invalid input for query argument $1: 'Jane'",
                },
                {
                    "type": "ValidationError",
                    "module": "pydantic_core._pydantic_core",
                    "value": "input_value='Jane Doe'",
                },
                {"type": "RuntimeError", "module": "builtins", "value": "lease lost"},
            ]
        }
    }
    values = [v["value"] for v in sentry.before_send(event, {})["exception"]["values"]]
    assert values == [FILTERED, FILTERED, "lease lost"]


class _Capture(sentry_sdk.transport.Transport):
    def __init__(self, options=None) -> None:
        super().__init__(options)
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)


def test_real_sdk_event_from_a_request_carries_request_id_and_no_pii() -> None:
    """The SDK itself (not a mock) with our options: an unhandled error in a request with an Authorization header."""
    transport = _Capture()
    s = Settings(database_url=None, environment="test", git_sha="cafe123", sentry_dsn="https://k@o1.ingest.example/1")
    real_init = sentry_sdk.init
    with patch("sentry_sdk.init", lambda **kw: real_init(**kw, transport=transport)):
        assert sentry.init_sentry(s) is True
    try:
        with TestClient(_app_with_probe(), raise_server_exceptions=False) as client:
            r = client.get(
                "/booking/_boom?email=a@b.example",
                headers={"X-Request-ID": "req-sentry", "Authorization": "Bearer abcdefghijkl", "Cookie": "s=1"},
            )
        sentry_sdk.flush(2)
    finally:
        sentry_sdk.get_client().close()
        real_init()  # back to a disabled client for the rest of the run
    assert r.status_code == 500 and r.headers["x-request-id"] == "req-sentry"
    assert transport.events, "no event captured"
    ev = transport.events[0]
    assert ev["tags"]["request_id"] == "req-sentry" and ev["release"] == "cafe123"
    text = json.dumps(ev)
    assert "a@b.example" not in text and "abcdefghijkl" not in text and "s=1" not in text


# --- settings: missing-DSN guard ----------------------------------------------------------------------------------


def test_missing_dsn_guard() -> None:
    with pytest.raises(SettingsError, match="SENTRY_DSN is empty"):
        load_settings({"ENVIRONMENT": "dev", "SENTRY_REQUIRED": "true"})
    assert load_settings({"ENVIRONMENT": "dev"}).sentry_dsn == ""  # not required: Sentry simply off
    ok = load_settings({"ENVIRONMENT": "dev", "SENTRY_REQUIRED": "true", "SENTRY_DSN": "https://k@o.example/1"})
    assert ok.sentry_required is True


def test_dsn_must_be_https_and_is_not_in_repr() -> None:
    with pytest.raises(SettingsError, match="https"):
        load_settings({"SENTRY_DSN": "http://k@o.example/1"})
    s = load_settings({"SENTRY_DSN": "https://k@o.example/1"})
    assert "k@o.example" not in repr(s)


def test_transactions_get_the_same_request_scrub_and_no_query_string() -> None:
    # AA-871 review: traced transactions carry the request block too; plain customer values in the query string
    # (a name or a search term that no PII pattern catches) must never reach Sentry.
    event = {
        "type": "transaction",
        "transaction": "/booking/customers",
        "request": {
            "url": "https://api-booking.example/booking/customers?q=Nguyen+Van+An#top",
            "query_string": "q=Nguyen+Van+An&page=2",
            "headers": {"Authorization": "Bearer abc", "X-Request-ID": "r1", "X-Forwarded-For": "1.2.3.4"},
            "cookies": {"s": "1"},
        },
        "user": {"ip_address": "1.2.3.4"},
    }
    out = sentry.before_send_transaction(event, {})
    text = json.dumps(out)
    assert "Nguyen" not in text and "page=2" not in text and "Bearer abc" not in text and "1.2.3.4" not in text
    assert (
        "query_string" not in out["request"]
        and out["request"]["url"] == "https://api-booking.example/booking/customers"
    )
    assert set(out["request"]["headers"]) == {"X-Request-ID"}


def test_sdk_contexts_are_sent_as_they_are_other_contexts_are_scrubbed() -> None:
    # AA-BOOKING-API-1 showed `runtime = [Filtered] 3.12.15`: the key scrub hit runtime.name ("CPython").
    event = {
        "contexts": {
            "runtime": {"name": "CPython", "version": "3.12.15"},
            "os": {"name": "Linux"},
            "trace": {"trace_id": "a" * 32, "span_id": "b" * 16},
            "customer": {"name": "Jane Roe", "email": "jane@example.com"},
        }
    }
    out = sentry.before_send(event, None)["contexts"]
    assert out["runtime"] == {"name": "CPython", "version": "3.12.15"}
    assert out["os"] == {"name": "Linux"}
    assert out["trace"]["trace_id"] == "a" * 32
    assert out["customer"] == {"name": FILTERED, "email": FILTERED}
