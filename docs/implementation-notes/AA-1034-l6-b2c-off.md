# AA-1034 R0 — the B2C intake off by flag (lane l6, Booking code PR)

Root ADR 0019 d8 (B2B-only ecosystem model, approved S265). Database half: bk-036 (`AA-1034-l6-bk036.md`, the
migration-only PR this branch is stacked on).

## Decisions

- **A code default, no Infra change.** `B2C_INTAKE_ENABLED` (`Settings.b2c_intake_enabled`) defaults to off in the
  dataclass and in `load_settings`; the ECS task definition does not set it. `true` turns the frozen intake back on
  (bk-036 must then be undone by a later file too).
- **Scheduler skip + claim refusal at the worker.** `Worker(disabled_kinds=...)` drops the kinds from its handlers and
  its recurring list, so `bk_inquiry_pull` is never enqueued and neither kind is claimed. Every maintenance pass
  cancels their queued rows (`queue.cancel_queued`, error text `DISABLED_ERROR`): without that, the singleton pull row
  left by the previous image would stay due and trip the `jobs_stuck` ops check after 900 s. A running row is left
  alone. The switched-off kinds are logged once at worker start; a cancel is logged only when it cancels something.
- **Both intake kinds.** `bk_inquiry_process` is switched off with the pull: a leftover process row would insert a
  customer (42501 after bk-036).
- **API: refuse only what would insert a traveller.** `POST /booking/customers`, `POST /booking/trip-cases` with
  `new_customer`, and `POST /booking/catalog/trip-plans/{id}/trip-cases` with `new_customer` answer 410
  `{"detail": "b2b_only", "message": ...}` (new `errors.Gone`) after auth, before any transaction. Reading, editing,
  archiving customers and a case for an existing customer still work (no INSERT on the B2C tables).
- **Staff admin: hidden behind `B2C_INTAKE_OPEN`** (`NEXT_PUBLIC_B2C_INTAKE === "on"`, build time; default off):
  the dashboard "New inquiries" tile, Customers "New lead" and row "New trip case", the customer page "New trip case",
  the Trip cases "New trip case" button, and the "New trip case" target of "Use for trip case" (the modal opens on
  "Add to an existing case"). `/cases/new` answers a kit `EmptyState` notice (old links). Copy is English, no issue ids.

## Changed

- Backend: `app/b2c.py` (new), `app/settings.py`, `app/errors.py`, `app/jobs/worker.py`, `app/jobs/queue.py`,
  `app/main.py`, `app/routers/{customers,trip_cases,catalog}.py`, `app/jobs/ops_checks.py` (hint text).
- Tests: unit `test_aa1034_b2c_off.py` (new), `test_job_worker.py`, `test_job_queue.py`, `test_lifespan_worker.py`,
  `test_w1_routes.py` (intake on for the frozen routes); integration `test_aa1034_intake_off.py` (new: queued intake
  rows cancelled, never claimed, no new pull; a lead answers 410 with no row written), `test_bk005_w1.py` and
  `test_aa679_trip_plans_api.py` (intake on for their frozen B2C paths).
- Frontend: `lib/config.ts`, the six pages above, `ui/booking/b2cOff.test.mjs` (source guards).
- `CONTEXT.md`: the env var.

## Tradeoffs

- 410 Gone, not 403 or 409: the feature is withdrawn by a business decision, not a permission or a state conflict.
- The cancel sweep runs every maintenance pass (every 15 ticks): one indexed UPDATE that finds nothing after the first
  pass. Cheaper than a one-shot flag and survives a row enqueued by an older image during a rolling deploy.
- Hiding by a build-time constant (not a runtime API read) keeps the change small; the API refuses anyway.

## Should know

- The branch is stacked on `chore/aa-1034-bk036-revoke-b2c-insert` (MIG-4): open this PR after the migration-only PR
  merges and rebase on `origin/main` (the duplicate commits drop out). The code does not need bk-036 applied.
- Live check after deploy: no new `bk_inquiry_pull` row in `booking.job` (the last one ends `cancelled` with
  "kind switched off ..."); `POST /booking/customers` as staff answers 410 `b2b_only`.
