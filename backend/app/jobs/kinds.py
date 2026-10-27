"""Per-kind facts for ``booking.job`` (AA-751): expected run time (plan §5 "Booking"), the deploy-gate exemptions
(plan §3, B3) and the deploy-blocking kinds (AA-993 G5).

A running job counts as **overdue** in the backlog metrics (:mod:`app.jobs.metrics`) once its current attempt has run
longer than ``OVERDUE_FACTOR`` x its expected seconds. Every kind the worker handles must be listed here (a unit test
compares this map with the handlers the lifespan registers); an unknown kind falls back to ``DEFAULT_EXPECTED_SECONDS``.

Values are generous upper bounds for a healthy run on Dev, not averages: a pull or a single inquiry is a few queries,
one e-mail is one SES call, a retention run is at most 50 batches of 1,000 rows under a 30 s statement timeout.

``DEPLOY_BLOCKING_KINDS`` (AA-993 G5, design D1.4): the deploy gate is unsafe only while a row of one of these
kinds is RUNNING, or QUEUED and due within ``DEPLOY_BLOCKING_HORIZON_SECONDS``. Queued and running rows of every
other kind are reported but no longer block: the worker releases a running job on shutdown and the new task re-claims
it (G4).

``DEPLOY_GATE_EXEMPT``: kinds the deploy gate leaves out of its ``active`` / ``by_status`` / ``by_kind`` counts
(information only since G5). Before G5 these counts decided the deploy; the reasons below date from then.
``bk_inquiry_pull`` is the recurring singleton that is always queued (re-enqueued at +60 s): counting it would block
every deploy, and interrupting it is harmless (a release puts it back, the next pull re-reads the outbox).
``bk_b2b_hold_expire`` (AA-973) is not exempt: it is queued only while a B2B hold is due and finishes in seconds, so it
holds a deploy back for one short run at most (DM-11.4 stays the default). ``bk_tour_lifecycle_pull`` (AA-986 L5) is
not exempt either, for the same reason: queued only while the CIS lifecycle outbox has new rows, done in seconds. Same
exemption idea as CIS ``deploy_gate_exempt``; the two lists stay separate until one registry exists (X1).
``bk_noop_sleep`` (AA-993 G4 proof, test-only) is exempt like the CIS ``noop_sleep``.
"""

from __future__ import annotations

DEFAULT_EXPECTED_SECONDS = 300
OVERDUE_FACTOR = 2

DEPLOY_GATE_EXEMPT: frozenset[str] = frozenset({"bk_inquiry_pull", "bk_noop_sleep"})

# AA-993 G5 (design D1.4): the only kinds whose RUNNING row makes the deploy gate unsafe. The retention runs keep
# no progress checkpoint (they are failed, not released, on shutdown: app.retention.jobs NOT_RELEASED), so a deploy mid
# run would end a run an approval capped; checkpointing them is a follow-up (design Q3). Every other kind is released on
# shutdown and re-claimed by the new task (G4), so its queued / running rows no longer block.
DEPLOY_BLOCKING_KINDS: frozenset[str] = frozenset({"bk_retention_job", "bk_retention_inbox_redact"})

# AA-993 G5 review: a QUEUED row of a deploy-blocking kind due within this window blocks too. The preflight runs before
# the image build + push and the old task keeps claiming until its SIGTERM minutes later, so it would claim the row and
# be stopped mid-run (a retention run is then failed, the approved run lost). 15 min covers build + push + roll; the
# retention kinds are enqueued due at once (daily schedule, manual run), MAX_ATTEMPTS 1, so a row due later is rare.
DEPLOY_BLOCKING_HORIZON_SECONDS = 900

# Test-only kinds (AA-993 G4 proof), registered only when JOB_TEST_KINDS is on (app.jobs.noop_sleep).
TEST_KINDS: frozenset[str] = frozenset({"bk_noop_sleep"})

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
    # AA-993 G4 proof (test-only, JOB_TEST_KINDS): sleeps up to 3600 s.
    "bk_noop_sleep": 3660,
}


def expected_seconds(kind: str) -> int:
    return EXPECTED_SECONDS.get(kind, DEFAULT_EXPECTED_SECONDS)


def overdue_after_seconds(kind: str) -> int:
    """Seconds after which the current attempt of a running job of ``kind`` counts as overdue."""
    return OVERDUE_FACTOR * expected_seconds(kind)
