"""Expected run time per ``booking.job`` kind (AA-751, plan §5 "Booking").

A running job counts as **overdue** in the backlog metrics (:mod:`app.jobs.metrics`) once its current attempt has run
longer than ``OVERDUE_FACTOR`` x its expected seconds. Every kind the worker handles must be listed here (a unit test
compares this map with the handlers the lifespan registers); an unknown kind falls back to ``DEFAULT_EXPECTED_SECONDS``.

Values are generous upper bounds for a healthy run on Dev, not averages: a pull or a single inquiry is a few queries,
one e-mail is one SES call, a retention run is at most 50 batches of 1,000 rows under a 30 s statement timeout.
"""

from __future__ import annotations

DEFAULT_EXPECTED_SECONDS = 300
OVERDUE_FACTOR = 2

EXPECTED_SECONDS: dict[str, int] = {
    "bk_inquiry_pull": 60,
    "bk_inquiry_process": 60,
    "bk_notify_email": 60,
    "bk_retention_job": 900,
    "bk_retention_inbox_redact": 900,
}


def expected_seconds(kind: str) -> int:
    return EXPECTED_SECONDS.get(kind, DEFAULT_EXPECTED_SECONDS)


def overdue_after_seconds(kind: str) -> int:
    """Seconds after which the current attempt of a running job of ``kind`` counts as overdue."""
    return OVERDUE_FACTOR * expected_seconds(kind)
