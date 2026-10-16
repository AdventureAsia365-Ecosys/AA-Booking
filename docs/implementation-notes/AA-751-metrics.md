# AA-751 (X6) B5 — booking.job backlog metrics (AA/Jobs/Booking)

Plan: `AA-Ecosys/docs/audits/2026-10-16-S240-aa751/aa751_plan.md` §5 "Booking" column, F13 (capacity-aware metrics),
F18 (per-attempt timestamp, already written by the B2 claim). S240 contract (Nghiệp approved, ~US$1.75/month for the
whole AA-832 + AA-751 CloudWatch set): in-process samplers instead of the Lambda -> API -> job chain, emails on ALARM
only. Infra (IAM + alarms) is a separate AA-CIS-Infra PR; CIS has its own sampler for `AA/Jobs/CIS`.

## Decisions

- **`backend/app/jobs/metrics.py` `JobMetricsSampler(pool, every_s=300)`**, one asyncio task started by the lifespan
  next to the worker (same condition: `worker_enabled and db.configured`), a separate task from the worker so a stuck
  worker loop still produces samples. It samples at once on start, then every 300 s until `stop`.
- **Shutdown:** the lifespan sets `stop`, then **cancels** the sampler task and awaits it, before waiting for the worker
  and before `db.close()`. A sample in flight is lost; a `PutMetricData` already in its thread finishes in the
  background (bounded by the 3 s / 5 s timeouts).
- **Reads:** two SELECTs on one pooled connection (bounded by the pool's `command_timeout` 5 s from `app.db`, no extra
  per-query timeout):
  - `min(run_after)` over `status = 'queued' AND run_after <= now() AND NOT cancel_requested`;
  - `(kind, now() - coalesce(progress->>'attempt_started_at', started_at))` per running row.
- **Pure rule** `backlog_sample(oldest_due_age, running)`:
  - `OverdueRunningJobs` = running rows with attempt age **>** `2 x EXPECTED_SECONDS[kind]` (strict), unknown kind ->
    default 300 s (600 s threshold);
  - `OldestQueuedAgeSeconds` = the oldest due age, but **0 when at least one running row is not overdue**. With two
    workers in a rolling deploy, one healthy job is enough to zero it; the stuck one shows as overdue.
  - A running row with no start time at all (cannot happen with the current claim) counts as healthy, never overdue.
- **Cancel-requested queued rows are excluded** from the queue age (decision not in the plan): the claim never takes
  them and nothing in Booking ends them yet, so counting them would page forever on a row that is not backlog. Nothing
  sets `cancel_requested` in Booking today.
- **Unhandled kinds count** (plan): a due queued `bk_x6_probe` row ages the metric, which is how window B fires it.
- **Publish:** one `put_metric_data(Namespace="AA/Jobs/Booking", MetricData=[OldestQueuedAgeSeconds (Seconds),
  OverdueRunningJobs (Count)])` per tick, no dimensions, server timestamp. boto3 client created lazily inside the
  thread on first use and reused; botocore `Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})`.
  botocore's `max_attempts` counts **retries after the first request**, so this is at most one retry (two sends), not a
  single attempt: worst case about (3 s + 5 s) x 2 plus backoff, ~16-17 s per tick, inside the `to_thread` worker, with
  no DB connection held, well under the 300 s period. Kept as the contract value (same as the CIS sampler);
  `total_max_attempts=1` would be the one-send setting if ever wanted.
  Region is the constant `CLOUDWATCH_REGION = "us-west-1"` (same convention as `app.retention.policy.ARCHIVE_REGION`),
  so no new environment variable.
- **Never raises:** a read failure logs `ops_metrics_sample_failed ... stage=read` and publishes **nothing** (missing
  data is what `-jobs-sampling-stopped` detects); a put failure (AccessDenied before the Infra apply, throttling,
  network, no credentials) logs `stage=put` with the error. Each successful tick logs one line
  `ops_metrics_sampled namespace=AA/Jobs/Booking oldest_queued_age_s=… overdue_running_jobs=… running=…
  due_queued_age_s=…` (`due_queued_age_s` is the raw age before the "healthy job running" rule, for debugging).
- **`backend/app/jobs/kinds.py` `EXPECTED_SECONDS`:** `bk_inquiry_pull` 60, `bk_inquiry_process` 60, `bk_notify_email`
  60, `bk_retention_job` 900, `bk_retention_inbox_redact` 900 (plan values; these are all five registered kinds).
  Kind names are string literals (importing the handler modules from `app.jobs` would create an import cycle); a unit
  test (`test_lifespan_worker.py::test_every_handled_kind_has_an_expected_run_time`) asserts the map equals the
  handler set the lifespan registers, so a new kind cannot be forgotten.

## Changed

- `backend/app/main.py`: starts / cancels the sampler; docstring.
- `CONTEXT.md`: layout line for `app/jobs/`, a "Backlog metrics" bullet in "Deferred work", notes list.
- Tests: `tests/unit/test_job_metrics.py` (rule, payload, boto3 config, never-raise, loop), `tests/unit/
  test_lifespan_worker.py` (sampler started and cancelled; not started when the worker is off; kinds map = handlers),
  `tests/integration/test_job_metrics.py` (the SQL as role `booking` on Postgres: due / not due / cancel-requested
  rows, attempt stamp vs `started_at`, claim feeds the sample, sampler publishes what it read).
- No migration, no grant change, no new setting or environment variable.

## Tradeoffs

- **Sample in the API process, not a separate service:** free and simple; the cost is that a dead or scaled-to-0
  service gives no samples, which the `-jobs-sampling-stopped` alarm (breaching on missing data) turns into a page.
  On Dev that alarm fires whenever Booking is scaled to 0 or `WORKER_ENABLED` is off; Infra documents it there.
- **"0 while a healthy job runs" instead of per-kind caps** (Booking has one serial worker): a long healthy retention
  run (up to 1,800 s before it counts as overdue) holds the queue age at 0, so a backlog behind it only shows once the
  run is overdue. Accepted in the plan (F13).
- **Thresholds are not yet checked against live p95** of `finished_at - started_at` per kind (plan §5 asks for a
  read-only ECS exec before merge). Not done from this worktree (no AWS access by rule). Recommended before merge.

## Should know

- Until the Infra PR grants `cloudwatch:PutMetricData` with condition `cloudwatch:namespace = AA/Jobs/Booking` on the
  Booking task role, every tick logs `ops_metrics_sample_failed ... stage=put error=ClientError: ... AccessDenied`
  (every 5 min). Harmless.
- Merge while `booking.job` is idle (Deploy Dev runs on `backend/**`; plan F22 / B4).
- Cost (CloudWatch, us-west-1): 2 custom metrics in the account-wide total, 288 `PutMetricData` calls/day; the total
  for the whole AA-832 + AA-751 set is in the Infra cost comment (~US$1.75/month).
