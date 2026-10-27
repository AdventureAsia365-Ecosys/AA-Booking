"""Per-kind facts for ``booking.job`` (AA-751): expected run time (plan §5 "Booking") and the deploy-gate exemptions
(plan §3, B3).

A running job counts as **overdue** in the backlog metrics (:mod:`app.jobs.metrics`) once its current attempt has run
longer than ``OVERDUE_FACTOR`` x its expected seconds. Every kind the worker handles must be listed here (a unit test
compares this map with the handlers the lifespan registers); an unknown kind falls back to ``DEFAULT_EXPECTED_SECONDS``.

Values are generous upper bounds for a healthy run on Dev, not averages: a pull or a single inquiry is a few queries,
one e-mail is one SES call, a retention run is at most 50 batches of 1,000 rows under a 30 s statement timeout.

``DEPLOY_GATE_EXEMPT``: kinds the deploy gate (``GET /booking/ops/deploy-gate``, ``app.routers.ops``) does not count.
``bk_inquiry_pull`` is the recurring singleton that is always queued (re-enqueued at +60 s): counting it would block
every deploy, and interrupting it is harmless (a release puts it back, the next pull re-reads the outbox).
``bk_b2b_hold_expire`` (AA-973) is not exempt: it is queued only while a B2B hold is due and finishes in seconds, so it
holds a deploy back for one short run at most (DM-11.4 stays the default). ``bk_tour_lifecycle_pull`` (AA-986 L5) is
not exempt either, for the same reason: queued only while the CIS lifecycle outbox has new rows, done in seconds. Same
exemption idea as CIS ``deploy_gate_exempt``; the two lists stay separate until one registry exists (X1).
"""

from __future__ import annotations

DEFAULT_EXPECTED_SECONDS = 300
OVERDUE_FACTOR = 2

DEPLOY_GATE_EXEMPT: frozenset[str] = frozenset({"bk_inquiry_pull"})

EXPECTED_SECONDS: dict[str, int] = {
    "bk_inquiry_pull": 60,
    "bk_inquiry_process": 60,
    "bk_notify_email": 60,
    "bk_retention_job": 900,
    "bk_retention_inbox_redact": 900,
    "bk_b2b_hold_expire": 120,
    "bk_tour_lifecycle_pull": 120,
    # AA-682 S2 (bk-027): daily, batches of 200 cases / reminders; not exempt (a run takes seconds).
    "bk_case_lifecycle": 300,
    "bk_ops_reminders": 120,
}


def expected_seconds(kind: str) -> int:
    return EXPECTED_SECONDS.get(kind, DEFAULT_EXPECTED_SECONDS)


def overdue_after_seconds(kind: str) -> int:
    """Seconds after which the current attempt of a running job of ``kind`` counts as overdue."""
    return OVERDUE_FACTOR * expected_seconds(kind)
