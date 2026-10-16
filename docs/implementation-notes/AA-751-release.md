# AA-751 (X6) B2 — release on shutdown, cancel_requested, attempt timestamp, job table template test

Plan: `AA-Ecosys/docs/audits/2026-10-16-S240-aa751/aa751_plan.md` §B2, §2.1 (comparator), F5 / F14 / F15 / F18 / F22.
ADR: root `docs/adr/0003a-per-owner-job-tables.md` decisions 2 and 4 (Booking's release-on-shutdown is part of X6,
data standards C6.2c). The bk-013 migration itself is PR B1 (its notes are `AA-751.md`); this file is the app PR.
It is a separate file so B1 and B2 do not both create `AA-751.md`.

## Decisions

- **Port of CIS `release`** (`AA-CIS-App/shared/jobs/queue.py`): same SQL shape (`WITH cur AS (...) UPDATE ... FROM
  cur`), same `MAX_RELEASES = 3`, `attempt = greatest(attempt - 1, 0)`, `run_after = now()`, lock cleared,
  `progress || {released: true, releases: n}`, guarded by `status = 'running' AND locked_by = $2`. Booking has no
  `cost_usd` reporting in handlers, so `release(pool, job_id, worker_id)` takes no cost argument (the column keeps its
  default 0).
- **Error texts** for cancel paths say "cancel requested" instead of CIS's "cancelled by admin": nothing in Booking
  sets `cancel_requested` yet, so naming an admin would be a guess.
- **Shutdown path in `Worker._run`:** `except asyncio.CancelledError` awaits `asyncio.shield(release(...))`, logs and
  re-raises. `wait_for` in the lifespan awaits the cancelled task, so the UPDATE runs before `db.close()`. A failing
  release is logged and the cancel still goes through (the reaper is the backstop).
- **`no_release` kinds (deviation, see Changed):** the two retention kinds are failed at once on shutdown instead of
  being released (`app.retention.jobs.NOT_RELEASED`, passed by `main.py`).
- **Reaper result** gains a `cancelled` count (`{"requeued", "failed", "cancelled"}`); the worker logs when any is
  non-zero.
- **Comparator and template are vendored byte copies** in `backend/tests/job_template/` (plan §2.1). The `.py` copy is
  excluded from ruff (`extend-exclude`) because it follows the CIS style; `__init__.py` there is Booking's own file.
- **Allowed extra index:** only the bk-002 singleton `job_active_pull_uq`, by its normalised definition
  `UNIQUE USING btree (kind) WHERE ((kind = 'bk_inquiry_pull'::text) AND (status = ANY (ARRAY['queued'::text,
  'running'::text])))` (taken from the first run against PG 16; PG 15 deparses it the same).
- **Drift check** `backend/scripts/check_job_template_copy.py`: stdlib, GitHub contents API, token then anonymous,
  retries, then FAIL, like Infra `db/check_migration_copies.py`. New CI job "Job template copy", **not a required
  check** (orchestrator decision S240).

## Changed (vs. the plan's B2 text)

- **Retention kinds are not released.** The plan said the CONTEXT retention note becomes "released on shutdown". A
  release would put a live manual run (`dry_run: false`, `max_rows` = the approved count) back in the queue with its
  attempt uncounted; the re-run counts afresh and could delete up to `max_rows` **again**, i.e. more than Nghiệp
  approved, because Booking retention keeps no progress checkpoint (CIS `retention_job.py` does: `deleted_total`,
  `cursors`). So the worker fails these kinds at once on shutdown (`NOT_RELEASED_ERROR`) — the same end state the
  reaper gave them before (attempt 1 of `max_attempts = 1`), without the 300 s wait. Making them resumable (a progress
  checkpoint like CIS) is a follow-up.
- **`fail()` honours `cancel_requested` too.** The plan listed claim, reaper and release. A retryable failure of a job
  with a cancel request would otherwise go back to `queued` and, because the claim now skips such rows, stay queued
  forever (and block the future B4 deploy gate). It now ends as `cancelled` (`RETURNING status`).
- Extra integration tests beyond the plan list: the no-release kind on shutdown, release of a row that is not ours,
  a retryable failure with a cancel request, read_shape as role `booking` (F15), and a drift test proving the
  comparison is not vacuous (dropped index, reset reloption, unlisted index all reported).

## Tradeoffs

- `asyncio.shield` protects the UPDATE from a second cancel; the cost is that, if the event loop is torn down right
  after, the shielded task may be abandoned. ECS gives 30 s after SIGTERM and the lifespan uses 20 s, so there is room.
- Cancellation that lands while `claim` itself is awaiting (UPDATE committed server-side, reply not yet read) leaves
  that row `running` until the reaper — same as before; not worth a protocol change.
- The vendored comparator is a test-only dependency (not in the image: the Dockerfile copies `app/` only).

## Should know

- **B2 must be rebased on B1 (bk-013) before CI.** This branch is cut from main without bk-013; the code and tests
  assume the bk-013 columns. Without them Integration Tests fail (`/ready` misses bk-013; `cancel_requested` /
  `progress` columns missing). Locally the B1 file was copied in, uncommitted, to run the suite.
- **The vendored files must equal AA-CIS-App main** (C1). They were copied from the C1 worktree while C1 was still in
  progress; after C1 merges, re-copy with `cp` from CIS main before the B2 PR (the "Job template copy" job fails
  until then, by design).
- **Deploy order:** bk-013 applied by DB Migrate first (else `/booking/health/ready` returns 503, since
  `REQUIRED_SCHEMA_VERSIONS` now lists bk-013), and merge only while `booking.job` has no non-exempt queued/running
  rows (F22: `backend/**` includes tests and triggers Deploy Dev; no gate until B4).
- Nothing in Booking sets `cancel_requested` yet (no staff cancel endpoint); a running job is not interrupted by it,
  it only changes how claim / reap / release / retry end the row.
- `progress.attempt_started_at` is a JSON timestamp string in the session time zone (`(progress->>'attempt_started_at')
  ::timestamptz` parses it); B5's overdue-running metric reads it.
- The interrupted part of a retention run that is failed on shutdown writes no audit row (pre-existing: the handler's
  `except Exception` does not see `CancelledError`). The S3 archive objects are the trace.
