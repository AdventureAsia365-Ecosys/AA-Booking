# AA-1025 (lane alert-diet, S263) — in-app job checks instead of CloudWatch alarms (Booking)

Decision table: root `AA-Ecosys/docs/audits/2026-11-01-S263-alerting-cost/diet.md`.

## Decisions

- The job sampler stops publishing `AA/Jobs/Booking` and runs two checks on the same read (`app/jobs/ops_checks.py`):
  `jobs_stuck` (oldest due queued row > 900 s while no healthy job runs) and `jobs_overdue` (> 0 running past 2 x
  expected). Thresholds are the removed alarms'.
- Alerts go to the platform alerts SNS topic (`ALERTS_TOPIC_ARN`), the topic the removed alarms used: same recipients,
  and the sampler (not a job) sends, because the checks are about the worker itself.
- Once per check per UTC day, deduped by a ledger row in `booking.job` (kind `bk_ops_alert`, status `succeeded`,
  idempotency key per check and day): race-free across two API tasks and restarts without a migration. A failed
  publish deletes the row so the next sample retries.

## Changed

- `app/jobs/metrics.py`: no boto3 CloudWatch client; `JobMetricsSampler(pool, every_s, publisher=...)`.
- `app/jobs/queue.py`: `record_alert`, `forget_alert`.

## Tradeoffs

- No "sampling stopped" alarm: the sampler lives in the API process, whose death is `aa-booking-<env>-no-healthy-task`.
  A sampler that stops while the API serves would go unnoticed (it never raises and loops forever; accepted).
- `bk_ops_alert` rows are not pruned by a retention kind (at most two a day); they show on the Jobs page as the history
  of sent alerts.

## Should know

- Needs the AA-CIS-Infra apply (task role `sns:Publish`, env `ALERTS_TOPIC_ARN` in the task definition) and then a
  deploy; without the env the sampler only logs `ops_check_finding`.
- Live proof: a `booking.job` row of an unhandled kind due 20 minutes ago makes the next sample mail once and write
  the ledger row; delete the probe row after.
