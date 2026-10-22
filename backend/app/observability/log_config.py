"""structlog JSON logging for the API process and its job worker (AA-871).

Every line on stdout (CloudWatch log group ``/ecs/aa-booking-dev``) is one JSON object. Both kinds of call render the
same way:

- the existing stdlib calls (``logging.getLogger(__name__).info("job %s %s succeeded", ...)``) go through the root
  handler's :class:`structlog.stdlib.ProcessorFormatter`; ``event`` is the formatted message;
- ``structlog.get_logger().info("evt", key=value)`` for new code.

Fields on every line: ``timestamp`` (UTC ISO 8601), ``level``, ``logger``, ``event``, ``service``, ``environment``,
``git_sha``, plus what is bound in :mod:`structlog.contextvars` (``request_id`` by the request-id middleware,
``job_id`` / ``job_kind`` by the worker). Values pass through :func:`app.observability.scrub.scrub` before rendering.
"""

from __future__ import annotations

import logging
import sys
from typing import IO, Any

import structlog

from app.observability.scrub import scrub
from app.settings import SERVICE_NAME, Settings

HANDLER_NAME = "aa-json"
# Fields this code writes itself; never redacted (an id is not personal data, DM-6.4).
_KEEP = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "service",
        "environment",
        "git_sha",
        "request_id",
        "job_id",
        "job_kind",
        "method",
        "path",
        "route",
        "status",
        "duration_ms",
    }
)


def _static_fields(settings: Settings):
    fields = {"service": SERVICE_NAME, "environment": settings.environment, "git_sha": settings.git_sha}

    def add(_logger: Any, _method: str, event_dict: dict) -> dict:
        for k, v in fields.items():
            event_dict.setdefault(k, v)
        return event_dict

    return add


def scrub_event(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog processor: redact PII / secrets in every value except the fields in ``_KEEP``."""
    event_dict.pop("color_message", None)  # uvicorn's ANSI-coloured copy of `event`
    if event_dict.get("message") == event_dict.get("event"):
        event_dict.pop("message")  # LogRecord.message, set when another handler formatted the record first
    return scrub(event_dict, keep=_KEEP)


def _shared_processors(settings: Settings) -> list:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _static_fields(settings),
    ]


def configure_logging(settings: Settings, stream: IO[str] | None = None, level: int = logging.INFO) -> None:
    """Install the JSON handler on the root logger. Idempotent; safe to call again (tests pass a ``stream``)."""
    shared = _shared_processors(settings)
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            scrub_event,
            structlog.processors.JSONRenderer(default=str),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for h in list(root.handlers):
        # Ours from an earlier call, or a plain basicConfig StreamHandler. pytest's capture handlers are subclasses
        # of StreamHandler and are left alone, so caplog keeps working.
        if h.get_name() == HANDLER_NAME or type(h) is logging.StreamHandler:
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own text handlers before it imports the app; route them through the JSON handler.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # The request-id middleware writes one `request_completed` line per request instead of uvicorn's access log.
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
