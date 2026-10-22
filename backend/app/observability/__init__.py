"""Logs, request ids and error tracking (AA-871): JSON logs (``log_config``), the request-id middleware
(``request_id``), PII / secret redaction (``scrub``) and Sentry (``sentry``)."""

from __future__ import annotations

from app.observability.log_config import configure_logging
from app.observability.sentry import init_sentry
from app.settings import Settings


def setup(settings: Settings) -> None:
    """Process-wide set-up, once per process before the app serves: JSON logging first, then Sentry."""
    configure_logging(settings)
    init_sentry(settings)
