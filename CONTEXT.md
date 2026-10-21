---
id: ARCH-06-booking
title: "AA-Booking — Repo Context"
description: Stack, boundaries, ownership, domain terms and working rules of AA-Booking (booking API, booking job worker and staff admin); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-booking-api, aa-booking-admin]
last_reviewed: 2026-10-19
review_every_days: 90
related: [ARCH-01, ARCH-05, ARCH-08, docs/adr/README.md]
---

# AA-Booking — Repo Context

AA-Booking (AAA) is the booking back office of the Adventure Asia ecosystem: customers (OTP
identity), trip cases opened from TripPlanner inquiries, proposals, suppliers, invoices and
payments. It is built in-house from scratch (no vendor code was delivered). Today the repo holds
the **bootstrap health slice** (FastAPI service with liveness/readiness, the 5-check CI, the Dev deploy
workflow, migration `bk-001`) and **slice 2 "inquiry intake"** (AA-681: migration `bk-002`, the
`booking.job` worker, TripPlanner `inquiry.submitted` → lead customer + trip case + staff notification) and
**slice 3 "staff workflow"** (AA-680 staff half + AA-681: migration `bk-003`, staff authentication with the CIS
staff JWT, staff profiles, trip case list/detail/edit/status/assignment endpoints, proposal versions) and the
**Wave 1 staff admin backend** (migration `bk-005`: case create, dashboard summary, activity, version send /
approve / delete, cross-case version list, customers, notifications, staff directory, settings, audit log read).

> Read this file before designing or changing anything here (AA-Ecosys steering, "CONTEXT.md
> first"). Change it in the same PR when a boundary, owner or the stack changes.

Design sources, in order:

1. Root ADR 0004 — AA-Booking stack, repo layout, runtime and bootstrap
   (`AA-Ecosys/docs/adr/0004-aa-booking-stack-and-bootstrap.md`). This repo's
   [`docs/adr/0001-implements-root-0004.md`](docs/adr/0001-implements-root-0004.md) records how it
   is implemented here.
2. Root ADR 0001 — ecosystem target architecture (placement on acc2, contracts, one IaC repo).
3. Root ADR 0002 — only CIS writes content; Booking reads published views only.
4. `AA-Ecosys/docs/database/schema-ownership.md` — who owns which schema and version key.
5. ERD v2 — `AA-Ecosys/docs/AAA/erd-v2/` (target data model; built in slices, see below).

## Role in the ecosystem

- **Owns** schema `booking.*` on the shared acc2 RDS (`aa-cis-dev-db`, PostgreSQL 15), migration key
  `bk-NNN`, and the **customer identity** (OTP) of the ecosystem (ADR 0001 decision 6).
- **Consumes** through owner grants on views: TripPlanner `tripplanner.v_inquiry_outbox`
  (`inquiry.submitted` v1 / v1.1 / v1.2, contract in `AA-Ecosys/docs/contracts/`, grant = TripPlanner `tp-009`) —
  pattern outbox → pull job → `booking.inbox_events`. CIS catalog read model (contract `catalog.tour.v1`, CIS
  migration 225 grant to role `booking`), **read live** at request time (no copy, no outbox consumer):
  `catalog.v_destination` (supplier place picker and check, AA-822) and `catalog.v_tour` / `v_tour_day` /
  `v_tour_photo` (trip plan endpoints, AA-679, S237 decision D4). Later: other catalog views (`v_tour_place`), CIS
  golden records.
- **Trusts** the Cognito WORKFORCE staff pool (root ADR 0006 / 0006a, AA-680 Phase 2): Booking verifies WORKFORCE
  access tokens of the `booking-staff` and `breakglass` clients and takes the Booking role from the `booking-*` groups
  (granted in CIS → Platform → Staff). During the transition it still accepts the CIS bridge token (ADR 0004 O1: CIS
  issues an RS256 JWT with `aud=booking`, verified against the CIS JWKS). Booking keeps only a thin staff profile
  (`booking.staff_users`) and never reads the CIS staff directory.
- **Publishes** to CIS (AA-858, contract `ops.overview.v1`, root ADR 0008 §5): two aggregate views in its own schema,
  `booking.v_ops_funnel_daily` (per UTC day, metric, source system) and `booking.v_ops_case_status` (cases per current
  status), `bk-017`, SELECT to the CIS runtime role `cis_app` (plus USAGE on schema `booking`); the CIS admin overview
  reads them. No other `booking.*` object is granted to CIS (isolation probe: `cis_app` → `booking.customers` 42501).
- **Never** writes another app's schema, never reads CIS pipeline tables (`acp_*`,
  `*_aa_internal`), never writes content (ADR 0002).

## Stack (same as AA-CIS-App)

| Part | Choice |
|---|---|
| Language | Python 3.12 |
| API | FastAPI, Pydantic v2, uvicorn (keep-alive 105 s > ALB idle 100 s) |
| DB access | asyncpg with plain SQL, no ORM in request paths; `ssl="require"` |
| Lint / tests | ruff, pytest + pytest-asyncio, migration guard, integration tests on Postgres 15 |
| Runtime | ECS Fargate service `aa-booking-dev-api` on `aa-cis-dev-cluster` (acc2, us-west-1) |
| Frontend | Staff admin in `frontend/` (AA-683): Next.js 16 App Router, React 19, TypeScript, Tailwind v4 on the copied CIS kit tokens, react-query, TanStack Table. Vercel project `aa-booking-admin`, deployed by CI (`.github/workflows/vercel-deploy.yml`, see Staff admin frontend). Customer surface (O8) not started |

## Layout

```text
backend/
  app/main.py              FastAPI app factory; every path is under /booking
  app/routers/health.py    GET /booking/health, GET /booking/health/ready
  app/routers/staff.py     GET /booking/staff/me, admin GET/PATCH /booking/staff
  app/routers/trip_cases.py  staff trip case + version endpoints (/booking/trip-cases*, /booking/versions)
  app/routers/customers.py  /booking/customers* (directory, detail, manual lead, edit/block, archive)
  app/routers/notifications.py  /booking/notifications* (staff feed, per-staff read state)
  app/routers/reference.py  /booking/settings, /booking/countries, /booking/audit-logs (admin)
  app/auth/                identity_verifier.py (byte copy of AA-CIS-App api/core/identity_verifier.py, the multi-issuer
                           verifier; never edited here), identity_config.py (accepted issuers + clients, JWKS prefetch,
                           AA-680), profile / Cognito identity link / permissions (staff.py), admin ops (admin.py),
                           deploy workflow GitHub OIDC verifier (ci.py, AA-751) over the single-issuer cache (tokens.py)
  app/routers/ops.py       GET /booking/ops/deploy-gate (machine route: Deploy Dev workflow OIDC token only, AA-751)
  app/trip_cases/          trip case service (list, summary, detail, activity, create, edit, status, assign),
                           versions (items upsert by id, send, approve, soft delete, cross-case list), sql.py fragments
  app/customers/           customers over booking.v_customer_summary (status lead/active/past), leads, archive
  app/notifications/       staff feed + notify helpers (service.py, rows); staff_email.py = bk_notify_email, the
                           "new inquiry" e-mail to the staff inbox (AA-811)
  app/mailer.py            SES v2 send helper (boto3 sesv2 send_email, From + configuration set from settings, AA-811)
  app/paging.py            keyset cursors (next_before) + ILIKE escaping; app/reference.py settings/countries
  app/audit.py, errors.py  audit_logs writes; service errors → HTTP 404/409/422
  app/db.py                lazy asyncpg pool + readiness probe (every REQUIRED_SCHEMA_VERSIONS row)
  app/settings.py          environment → Settings
  app/jobs/                booking.job queue (claim/lease/reap/backoff/release) + Worker loop (O3, maintenance hooks,
                           release on shutdown, AA-751); kinds.py (EXPECTED_SECONDS per kind, DEPLOY_GATE_EXEMPT) +
                           metrics.py (backlog metrics sampler, AA/Jobs/Booking to CloudWatch, AA-751)
  app/retention/           AA-825 retention kinds bk_retention_job / bk_retention_inbox_redact: policy (windows),
                           redact (inbox payload PII), archive (S3 JSONL.gz), runner (batched count/delete/redact),
                           jobs (modes, handlers, daily schedule hook, admin status)
  app/routers/retention.py /booking/retention (status), POST /booking/retention/runs (DRY RUN default),
                           GET /booking/retention/runs/{job_id} — retention.admin (admin only)
  app/inquiry/             inquiry.submitted validation (contract.py) + bk_inquiry_pull/process (intake.py)
                           + leg catalog tour key resolve / back-fill (leg_keys.py, `python -m app.inquiry.leg_keys`)
  app/pricing/             ADR 0005 pricing: pure Decimal engine (engine.py) + rule/FX reads (repository.py, approved
                           rows only) + rules/FX admin with maker-checker (policy.py, bk-007)
  app/routers/catalog.py   GET /booking/catalog/destinations (place picker, any active staff, AA-822);
                           GET /booking/catalog/trip-plans (+ /facets, /{source_id}), PUT …/{source_id}/commercial,
                           POST …/{source_id}/trip-cases (AA-679, bk-009)
  app/catalog/destinations.py  reads of catalog.v_destination (fail closed on writes, soft on reads; re-exported by
                           app/catalog/__init__.py)
  app/catalog/trip_plans.py  catalog.v_tour LEFT JOIN booking.trip_plans (live), commercial upsert, items from v_tour_day
  app/routers/pricing.py   POST /booking/pricing/quote-preview (pricing.read); GET/POST /booking/pricing/rules and
                           /booking/pricing/fx-rates, POST …/{id}/approve (reads pricing.read, writes pricing.write)
  app/pricing/service.py   rule + FX in force + engine, shared by quote-preview and POST …/versions/{n}/price
  app/routers/suppliers.py /booking/suppliers* (list, summary, detail, create, edit, deactivate/activate, contracts)
                           and /booking/contracts/{id} (edit a draft, publish, expire) — supply.read / supply.write
  app/suppliers/           supplier + contract service (bk-008): place check, maker-checker publish, audit rows
  app/routers/supply.py    /booking/hotels*, /booking/room-types/{id}, /booking/services*, /booking/contracts/{id}/rates,
                           /booking/rates/{id}, /booking/supply/options, /booking/supply/rate-in-force (bk-010, AA-823)
  app/supply/              hotels + room types + services (records.py, one implementation for both kinds) and supplier
                           net rates + the default net of a version item (rates.py)
  app/contracts/           byte copies of AA-Ecosys docs/contracts/*.json (SHA-256 pinned by a test)
  tests/unit/              unit tests + migration guard (tests/migration_guard.py)
  tests/integration/       real Postgres: applies migrations/, checks role privileges, /ready, booking.job vs template
  tests/job_template/      byte copies of AA-CIS-App shared/jobs/job_table_template.sql + job_table_shape.py (AA-751;
                           never edited here, drift-checked by CI "Vendored CIS copies")
  scripts/                 check_vendored_copies.py (that drift check, also for app/auth/identity_verifier.py; stdlib,
                           GitHub contents API)
  Dockerfile               python:3.12-slim, port 8000, non-root, stdlib healthcheck
migrations/NNN_*.sql       schema booking, key bk-NNN (byte-copied to AA-CIS-Infra)
docs/adr/                  repo ADRs (0001 implements root ADR 0004)
docs/ui-spec.md            staff admin UI spec (AA-683; routes, waves, API per route)
docs/implementation-notes/ per-issue decisions (AA-678.md, AA-679.md, AA-680.md, AA-681-slice2.md, AA-681-slice3.md, AA-681-w1.md,
                           AA-683.md, AA-751.md, AA-751-release.md, AA-751-metrics.md, AA-751-B3-B4.md, AA-810.md,
                           AA-811.md, AA-822.md, AA-823.md, AA-825.md)
.github/workflows/         ci.yml (5 required checks + non-required Frontend Build and Vendored CIS copies), deploy-dev.yml,
                           docs-lint.yml (AA-888: front-matter, ADR index, single-line AWS CLI, offline links; weekly
                           freshness report)
.github/scripts/           booking-deploy-preflight.sh (the Deploy Dev job guard, AA-751 B4)
tools/docs/                docs lint tools vendored from AA-Ecosys tools/docs (AA-888; README says how to keep them identical)
docs/templates/            frontmatter.schema.json, copy of the AA-Ecosys schema the front-matter check uses
frontend/                  staff admin (AA-683), see "Staff admin frontend" below
```

## Runtime and request path

```text
client → API Gateway REST "aa-booking-dev" (ANY /booking/{proxy+})
       → existing VPC link → NLB → ALB HTTP listener, rule path /booking/*
       → target group :8000 (health check /booking/health) → this container
```

All resources (ECR `aa-booking-dev-api`, log group `/ecs/aa-booking-dev`, task/exec roles, secret
`booking/dev/database-url`, service, target group, listener rule, REST API, OIDC deploy role
`aa-booking-dev-app-deploy`) are defined in **AA-CIS-Infra** `accounts/aa365/booking.tf`. This repo
owns code only ("App owns code, Infra owns resources"). The service starts with `desired_count = 0`;
scaling to 1 is an approved, billable step.

Process environment (set by the task definition): `DATABASE_URL` (from the secret), `ENVIRONMENT`,
`GIT_SHA` (baked into the image), `DB_SSL` (only `test`/`local` may set `disable`), `WORKER_ENABLED`
(optional; default on when `ENVIRONMENT=dev` and `DATABASE_URL` is set), `CIS_JWKS_URL` and
`STAFF_JWT_ISSUER` (optional; the CIS bridge, default the Dev CIS, `https://api-cis.lumiguides.it.com[/auth/.well-known/jwks.json]`).
Cognito (AA-680, all optional and **not set on Dev**, defaults are code constants in `app/auth/identity_config.py`):
`COGNITO_WORKFORCE_ISSUER`, `COGNITO_BOOKING_STAFF_CLIENT_ID`, `COGNITO_BREAKGLASS_CLIENT_ID` (same names as CIS).
E-mail (AA-811, all optional): `STAFF_NOTIFY_EMAIL` (staff inbox for the new inquiry e-mail; empty = no e-mail),
`SES_FROM_ADDRESS` (must equal the Infra `ses_from_address`, the only From the task role may use; empty = no e-mail),
`SES_CONFIGURATION_SET` (Infra `aa-email-<env>-transactional`), `SES_REGION` (default `us-west-1`), `STAFF_ADMIN_URL`
(links in staff e-mail; default the Dev Vercel alias `https://aa-booking-admin.vercel.app`).

## E-mail (AA-811)

Booking sends transactional e-mail through **Amazon SES** on acc2 (identity `lumiguides.it.com`, Infra
`accounts/aa365/ses.tf`; the task role may send only as `ses_from_address`, IAM `ses:FromAddress`). Today there is one
message: the staff **"new inquiry"** e-mail. Booking sends it, not TripPlanner (S238 decision, ADR 0004 boundary:
TripPlanner is the front door, Booking owns customers, trip cases and notifications); TripPlanner's `notify.py` only
logs. Sending happens in a `booking.job` (`bk_notify_email`), never in a request path or inside the intake
transaction. Templates are in code (`app/notifications/staff_email.py`). Bounce/complaint suppression in Booking
tables is not built yet (the SES configuration set's account suppression list applies). Customer e-mail (proposal
send, OTP) is not built: those still write rows only.

## Staff authentication and permissions (AA-680; root ADR 0006 / 0006a, ADR 0004 O1)

- Every `/booking/*` route except `/booking/health*` and `/booking/ops/*` needs `Authorization: Bearer <token>`,
  checked by the multi-issuer verifier (`app/auth/identity_verifier.py`, a byte copy of the CIS module: RS256 only, the
  issuer picked by the unverified `iss`, one JWKS cache per issuer, unknown `kid` refetch at most every 60 s, last good
  keys kept on a failed fetch; no key set ever loaded = 503 `auth_unavailable`, any bad token = 401 `invalid_token`).
  The lifespan prefetches both JWKS in the background (`identity_jwks_prefetched` per issuer; off when
  `ENVIRONMENT=test`). Accepted (`app/auth/identity_config.py`):
  - **WORKFORCE** (issuer `DEV_WORKFORCE_ISSUER` in `backend/app/auth/identity_config.py` on Dev) x clients
    **booking-staff** and **breakglass**: access tokens only (`token_use = access`), the client in `client_id` (no
    `aud` check), roles from `cognito:groups`. `cis-admin` is not accepted (no CIS screen calls Booking yet).
  - **BRIDGE** (transition only, ADR 0006a A6): `iss` = `STAFF_JWT_ISSUER`, `aud` = `booking`, `sub` = CIS
    `admin_users.id`, the role in `cis_role` (`admin|reviewer|content`). Removed by AA-680 PR-4 (`BRIDGE_ENABLED`).
- **WORKFORCE identity (bk-016).** The Booking role is the first `booking-*` group in the order `booking-admin` →
  `admin`, `booking-sales` → `sales`, `booking-contracting` → `contracting`, `booking-content_creator` →
  `content_creator`, re-read on **every** request (a change is written and audited `staff.role_synced`); no
  `booking-*` group = 403 `no_booking_role`, nothing written. `cis_role` = the highest `cis-*` group (informational).
  The row is found by `(cognito_iss, cognito_sub)` only and never rebound. No row → one is created, active, keyed
  `'cog:' || sub` (`email` from the token, `name` = the e-mail local part; audited `staff.provisioned`). Bridge-era rows
  keep their CIS key: AA-CIS-Infra `db/scripts/aa680_link_staff_cognito.py` links them once from `shared.admin_users`.
  A `cog:` key already bound to another issuer = 403 `identity_conflict`. `active = false` = 403 `staff_inactive`:
  `active` stays a Booking admin switch (suspend); the **role** of a linked row is refused by `PATCH
  /booking/staff/{id}` with 409 `managed_in_directory` (it is managed in CIS → Platform → Staff). `GET /booking/staff*`
  shows `managed_in_directory`.
- **BRIDGE provisioning** (unchanged): first valid token → `booking.staff_users` row keyed by `sub`: CIS `admin` →
  active Booking `admin`; `reviewer` → `sales`, `content` → `content_creator`, **inactive** (403 `staff_inactive`)
  until a Booking admin activates them. Later bridge tokens only refresh email/name/cis_role/last_seen_at, also on a
  linked row. A bridge `sub` starting with `cog:` is 401.
- Permissions are a matrix in code (`app/auth/staff.py`, provisional until the PRD matrix is confirmed):
  read trip cases / customers = admin, sales, contracting; write / assign / versions / customers write = admin,
  sales; staff directory (pickers) = admin, sales, contracting; pricing (margins, net costs, FX: `pricing.read`) =
  admin, sales, contracting; new pricing rule / FX rows and their approval (`pricing.write`) = admin, contracting
  (maker-checker: the creator of a row cannot approve it, 403 `own_row`); suppliers and supplier contracts read
  (`supply.read`) = admin, sales, contracting, write (`supply.write`) = admin, contracting (maker-checker on contract
  publish: not the draft's last writer, 403 `maker_checker`; hotels, room types, services and supplier rates use the
  same pair, and writing a rate makes you the contract's maker); catalog reads (`catalog.read`) = every role,
  content_creator included (without the price hint, channel and commercial notes, which need `pricing.read`); the
  trip plan commercial row (`trip_plan.write`) = admin, contracting; "Use for trip case" = `trip_case.write` +
  `version.write` (admin, sales); block / archive customers, staff admin, audit log read, retention runs and status
  (`retention.admin`, AA-825) = admin. Notifications,
  settings, countries, the catalog place picker (`/booking/catalog/destinations`), own preferences and own staff
  detail = any active staff.
- Every staff write adds one `booking.audit_logs` row (`actor_ref` = the staff key: CIS user id or `cog:<sub>`).
- **One machine caller: the Deploy Dev workflow, by GitHub OIDC** (AA-751 B3, `app/auth/ci.py`). `GET
  /booking/ops/deploy-gate` takes no staff JWT and no `staff_users` row. It accepts only a GitHub Actions OIDC token:
  RS256 against GitHub's JWKS (`https://token.actions.githubusercontent.com/.well-known/jwks`, same cache code as the
  staff JWKS), `iss` = `https://token.actions.githubusercontent.com`, `aud` = `aa-booking-deploy-gate`, `exp`/`iat`
  required, `repository` = `AdventureAsia365-Ecosys/AA-Booking` and its immutable `repository_id` = `1418188499` /
  `repository_owner_id` = `276539096` (names are reusable after a rename or transfer; a repo transfer or recreate
  means changing these constants), `ref` = `refs/heads/main`, `environment` = `dev`,
  `event_name` in `push`/`workflow_dispatch`, `job_workflow_ref` =
  `AdventureAsia365-Ecosys/AA-Booking/.github/workflows/deploy-dev.yml@refs/heads/main`. Any other token is 401;
  GitHub's JWKS unreachable (no cached keys) is 503. All values are code constants: no secret, no variable.

## Deferred work (`booking.job`, O3/O9)

Booking owns its job table `booking.job` (same shape and semantics as CIS `shared.job`, ADR 0003 / 0003a), so
Booking jobs never block the CIS deploy guard. Kinds are `bk_*`. On Dev the worker loop runs inside the
API process as one lifespan background task (never in a request path), one job at a time; overlapping
tasks during a rolling deploy are safe (`SKIP LOCKED`, idempotent handlers).

The table follows the job table DDL template owned by CIS (`AA-CIS-App/shared/jobs/job_table_template.sql`, ADR 0003a
decision 2; caught up by bk-013): Integration Tests render it into a scratch schema and compare `booking.job` with it
through pg_catalog (`tests/integration/test_job_table_template.py`; the only allowed extra is the `bk_inquiry_pull`
singleton index). Runner rules since AA-751 (port of CIS `shared/jobs/queue.py`):

- **Shutdown releases.** The lifespan gives a running job 20 s (`WORKER_STOP_TIMEOUT_S`), then cancels the worker task,
  which puts the job back to `queued` at once (due now, the interrupted attempt not counted, `progress.releases` + 1)
  before the pool closes; the third release fails it. A kind in the worker's `no_release` set is failed at once
  instead (today the two retention kinds, see below). A job is left to the lease reaper (300 s) only when the process
  dies without a graceful stop.
- **`cancel_requested`** is never claimed; the reaper, a release and a retryable failure end such a job as
  `cancelled` (never back to `queued`). Nothing in Booking sets it yet (no staff cancel endpoint); a running job is not
  interrupted by it.
- **Per-attempt start:** each claim writes `progress.attempt_started_at`; `started_at` keeps the first attempt's time.
- **Backlog metrics (AA-751, `app/jobs/metrics.py`).** A second lifespan task, next to the worker and only when it runs,
  sends one `PutMetricData` to CloudWatch namespace `AA/Jobs/Booking` (no dimensions) every 300 s:
  `OldestQueuedAgeSeconds` = age of the oldest due queued row without a cancel request (any kind), but **0 while a
  non-overdue job is running** (the worker is serial); `OverdueRunningJobs` = running rows whose current attempt is older
  than 2 x `EXPECTED_SECONDS[kind]` (`app/jobs/kinds.py`, default 300 s; every handled kind must be listed, a unit test
  checks it). It reads `booking.job` directly (never through the queue), never raises, and is cancelled on shutdown
  before the pool closes. AA-CIS-Infra owns the IAM grant (task role, namespace `AA/Jobs/Booking` only) and the three
  alarms `<booking prefix>-jobs-queue-age` (> 900 s), `-jobs-overdue-running` (> 0) and `-jobs-sampling-stopped` (no
  sample for 45 min, which also fires while the service is scaled to 0 or `WORKER_ENABLED` is off).
- **Deploy gate (AA-751 B3/B4).** `GET /booking/ops/deploy-gate` returns `{active, by_status, by_kind, safe_to_deploy,
  exempt_kinds}`: `queued` + `running` rows with no time filter, kinds in `DEPLOY_GATE_EXEMPT` (`app/jobs/kinds.py`,
  today `bk_inquiry_pull`, the always-queued pull) not counted. The `preflight` job of `deploy-dev.yml` (logic in
  `.github/scripts/booking-deploy-preflight.sh`, tested with stub `aws`/`curl` by
  `backend/tests/unit/test_deploy_preflight_script.py`) skips the gate only when the service has no task running,
  pending or desired; a task that is starting (none running, one pending or desired) blocks; otherwise it blocks the
  deploy unless `safe_to_deploy`. Every block can be lifted by a `workflow_dispatch` `override_running_jobs` reason;
  an unreachable gate blocks too. Pushes that only touch `backend/tests/**`
  do not deploy.

| Kind | What |
|---|---|
| `bk_inquiry_pull` | Recurring singleton (every ~60 s): reads `tripplanner.v_inquiry_outbox`, anti-joins `booking.inbox_events`, inserts new rows `ON CONFLICT DO NOTHING`, queues one process job each. `booking.inbox_cursor.safe_id` is only a scan bound, advanced behind a 600 s window (contract rule 4). |
| `bk_inquiry_process` | Validates v1/v1.1/v1.2; each leg gets the catalog tour key `(source_system, source_id)` from the v1.2 payload or by resolving `source_tour_id` against `catalog.v_tour` (null when unresolved; AA-830); creates a **lead** customer (`verified = false`, never merged into an existing customer; `needs_merge_review` + `merge_candidate_id` on an email/phone match), the trip case + legs, a staff `notifications` row (+ one `bk_notify_email` job for it, same transaction) and an audit row; marks the inbox row `processed`. `customer: null` or no email/phone → `parked`; invalid → `failed`. |
| `bk_retention_job` | AA-825 TTL (data standards §7, E3): deletes finished jobs past their window — `succeeded` 30 d; `failed` / `cancelled` 90 d, archived to S3 first (`s3://aa-cis-bronze-<acc2>/archive/booking/job/yyyy/mm/`, gzipped JSON Lines, one object per batch and UTC month); `queued` / `running` never. Rows referenced by a FK are never deleted. |
| `bk_retention_inbox_redact` | AA-825 / DM-6.6: 90 d after `received_at`, rebuilds `inbox_events.payload` from an allowlist of the contract's non-PII keys (`customer.name` / `email` / `phone` and `itinerary.custom_days[].note` nulled, any other key dropped, non-object parts nulled, so a failed non-conforming payload keeps no PII either) and replaces a failed row's `error`; sets `redacted_at` (bk-012). Row, `idempotency_key`, `contract_version`, status kept (dedupe unchanged); rows still `received` are skipped and counted. Nothing deleted, nothing archived. |
| `bk_notify_email` | One per new inquiry notification (idempotency key `bk_notify_email:<notification_id>`): reads the notification, trip case, lead customer, legs and (live, soft) the CIS `catalog.v_tour` titles/countries, sends one e-mail to `STAFF_NOTIFY_EMAIL` through SES; result `{outcome: sent, message_id, ...}`. Not configured → `skipped` with the reason; an SES refusal that cannot change (rejected, sender not allowed, paused) fails at once, throttling / network errors retry with backoff. At least once: a worker that dies after SES accepted the message re-sends it. |

Retention runs (both kinds, `app/retention/jobs.py`, DM-7.2): a manual run is a **DRY RUN** unless it says
`dry_run: false` with `max_rows` (the approved count); the worker queues one `trigger: schedule` run per kind per UTC day
(maintenance hook, first pass at or after 19:00 UTC), which stays a DRY RUN until a live run of the same windows
succeeded, then runs live unless the candidates exceed 2x the last live run (floor 1,000): then **held** (nothing
written, a notification to each active admin). Batches of at most 1,000 rows (keyset on `(ts, id)`, `FOR UPDATE SKIP
LOCKED`, 30 s statement timeout), at most 50,000 rows per run, `max_attempts = 1` and **not released on shutdown**
(the run keeps no progress checkpoint, so a second run could exceed the approved `max_rows`; a shutdown fails it at
once, the next run counts afresh), one advisory lock per kind. Every run writes its counts to the job result **and** one `booking.audit_logs` row
(`entity_type = 'retention'`, action `retention.dry_run|live|held|failed`); the last live run is read from the audit log.
The archive needs the task role to hold `s3:PutObject` on `archive/booking/*` (AA-CIS-Infra); without it a live run of
`bk_retention_job` with failed candidates fails before deleting them.

## Health contract (ADR 0004 decision 9)

- `GET /booking/health` → 200 `{"status":"ok","service":"aa-booking-api","git_sha":"<sha>"}`.
  No DB call. Used by the ALB target group, so a DB outage never trips the ECS circuit breaker.
- `GET /booking/health/ready` → 200 `{"status":"ok","db":"ok","schema_version":"bk-005"}` when the
  DB answers and **every** `bk-NNN` in `REQUIRED_SCHEMA_VERSIONS` is registered (a gap, e.g. bk-005 in but
  bk-004 missing, is not ready); otherwise 503 with `db` = `ok` | `error` | `not_configured` (plus
  `missing_schema_versions` when the DB answers). Bodies never contain the DSN or error messages.

## Database rules

- Migrations live in `migrations/`, are reviewed here, then **byte-copied** to
  `AA-CIS-Infra/db/migrations/booking/` and applied by the **DB Migrate** workflow (check → apply).
  Never applied from a laptop. Apply a migration before merging code that needs it, then add it to
  `REQUIRED_SCHEMA_VERSIONS`.
- **Owner of `booking.*` = NOLOGIN role `bk_owner` from bk-014** (X1, ADR 0008 decision 6; created by
  AA-CIS-Infra `db/migrations/roles/001`, granted to the RDS master). Until bk-014 DB Migrate ran every
  file as the RDS master `aa_cis_admin`, which owned everything (default privileges `FOR ROLE
  aa_cis_admin`, ADR 0004 decision 6). bk-014 moves the schema and all 34 relations, 5 standalone
  sequences and 12 functions to `bk_owner`, restates the default privileges `FOR ROLE bk_owner` and
  revokes the old rows; role `booking`'s privileges are unchanged. From bk-015 on DB Migrate runs
  each file as `bk_owner` (`SET ROLE`, it follows the live owner of schema `booking`; since X1 S22 its login is
  `db_migrator`, a member of `bk_owner`, no longer the RDS master), so new objects
  are owned by `bk_owner` and new default privileges say `FOR ROLE bk_owner`. `bk_owner` needs USAGE on
  `shared` + SELECT, INSERT on `shared.schema_versions` (CIS 243): registration, and
  `booking.v_schema_versions` (a view reads with its owner's rights) for readiness. Rollback of bk-014
  is an Infra `roles/` undo file (the master), never a `booking/` file.
- **Isolation from CIS (X1 done, 19/10/2026).** The CIS runtime logs in as `cis_app` since X1 S18 and its
  task role is denied the master and `booking/*` secrets since S19, so it cannot read `booking.*` (42501;
  isolation probe cell "CIS runtime → `booking.customers` 42501" passes, 58/58 after S22). `cis_app` reads only the
  contract views granted in bk-017. The RDS master `aa_cis_admin` still inherits `bk_owner` (rl-001; on
  PostgreSQL 15 the grant follows the member's INHERIT, per-grant `INHERIT FALSE` needs 16), so it can read and
  alter `booking.*`, but it is break-glass only (data standards DM-8.9): no app, workflow or ECS-exec script logs
  in as the master, and the nightly probe cell `sessions.master_break_glass_only` flags any session of it.
- The app connects as role `booking`: `USAGE` on schema `booking` only (no `CREATE`), DML on its
  tables, `audit_logs` append-only, views read-only. Its LOGIN password is set out-of-band by
  Nghiep and stored only in Secrets Manager `booking/dev/database-url`. **No password in Git.**
- The migration guard (unit test, required check) rejects any file that is not wrapped in
  `BEGIN/COMMIT`, does not register its own `bk-NNN`, touches anything outside schema `booking`
  (the `shared.schema_versions` row is the one exception), creates logins/passwords/extensions/
  `SECURITY DEFINER` functions, grants to anyone but `booking`, or leaves a view writable. One exception (bk-017,
  AA-858): a contract read of another app's runtime role (`CONSUMER_ROLES` = `cis_app`) may get `USAGE ON SCHEMA
  booking` and `SELECT` on `booking.v_*` views only (rule `consumer-grant`); every such grant is listed in AA-CIS-Infra
  `db/owners.yaml` `booking.consumer_grants`, which the isolation probe checks.
- X1 (per-app owner roles, AA-Ecosys `docs/audits/2026-10-17-S241-x1/x1-plan.md`): DB Migrate runs a
  file as the **live owner of schema `booking`** (`SET ROLE` when that is not its login). The guard's
  migration role is therefore per file (`tests/migration_guard.py` `migration_role`): `aa_cis_admin`
  for bk-001 … bk-014, `bk_owner` from bk-015 on (default privileges `FOR ROLE bk_owner`; `FOR ROLE
  aa_cis_admin` only as bk-014's REVOKE). Only bk-014 may `ALTER … OWNER TO bk_owner`; no file may
  switch roles (`SET`/`RESET ROLE`, `SESSION AUTHORIZATION`, `set_config('role', …)`).
- Integration Tests replay the migrations as a **non-superuser** `aa_cis_admin` (CREATEROLE, member
  of `bk_owner`, and of `cis_app` as on Dev, the bk-017 grantee), the RDS shape:
  `tests/integration/bootstrap_roles.py` sets the roles up with the container superuser, which the tests never use.
- Grants on other apps' objects are written by **their** owners, in their migrations, unguarded,
  after role `booking` exists (ADR 0004 decision 6).

### `bk-001` (first slice, ADR 0004 decision 7)

Schema + role + grants + default privileges; `customers`, `customer_otps`, `customer_sessions`,
`media_files`, `audit_logs`, `inbox_events` (table only); `v_schema_versions` (Booking's rows of
`shared.schema_versions`, for readiness). No enum types yet.

### `bk-002` (AA-681 slice 2, inquiry intake)

`booking.job` (+ singleton index for the recurring pull), `inbox_cursor`, `inbox_events.status`
(`received|processed|parked|failed`), `contract_version`, `source_position`; customer lead columns
(`verified`, `source`, `needs_merge_review`, `merge_candidate_id`, `external_ref`, consent) with
email/phone unique among **verified** live customers only; `trip_cases` (ERD v2 first slice, no
`trip_plan_id`, status `TEXT + CHECK` with ERD's 13 values), `trip_case_legs` (`source_tour_id TEXT`),
`notifications` (staff queue, `recipient_id NULL` until O1).

### `bk-003` (AA-680 staff half + AA-681 slice 3)

`staff_users` (thin profile keyed by `cis_user_id` = JWT `sub`; role `admin|sales|contracting|content_creator`,
`active`), `trip_case_status_transitions` (read-only for the app; pre-sale edges only: `new_inquiry → discovery →
building → proposal_sent`, back-steps `building → discovery`, `proposal_sent → building`, `→ cancelled` from those
four) + a trigger rejecting other edges + `trip_case_status_history` written by trigger (actor/reason from
transaction-local `booking.actor` / `booking.status_note`; append-only for the app), `staff_assignments` (one open
assignment per case and role), `trip_case_versions` (one approved per case; approved = locked by trigger) +
`trip_case_version_itinerary` (free-text items until AA-679), `trip_cases.notes`, `audit_logs.actor_ref`.

### `bk-004` (AA-810, ADR 0005 first cut — needs `bk-003`)

`pricing_rules` (channel `b2c_tailor_made|fixed_departure|b2b`, target/floor margin, peak uplift points,
card fee, `valid_from`; seeded with the ADR 0005 table, to confirm with Ms. Thư), `fx_rates` (units per USD,
selling buffer, `as_of`, `source`; no seed). Both are **append-only** for role `booking` (UPDATE/DELETE
revoked): a change is a new row, the row in force is the newest one dated on or before the pricing day.
`version_item_costs` (net cost lines of a bk-003 version item, supplier currency), locked with the version
once it is approved (bk-006: or sent); they survive a version item save (D2 keeps item ids) and go with an item that is removed
(`ON DELETE CASCADE`). Formula and rounding: `backend/app/pricing/engine.py` docstring and
`docs/implementation-notes/AA-810.md`.

### `bk-005` (Wave 1 staff admin, AA-681/AA-680/AA-810 W1)

Independent of `bk-004` (applies before or after it; per-table grants only, so the append-only pricing tables stay
append-only). Status edges `proposal_sent → confirmed` (only via version approval), `confirmed → cancelled` and
`cancelled → new_inquiry` (cancel and reopen need a reason); `trip_case_status_history.seq`; `trip_cases.pax_adults/pax_children` (+ split check);
`countries` (ISO-2 reference, not content) + `trip_case_countries`; `customers.code` (`CUS-000123`) and the read-only
view `v_customer_summary` (status lead / active / past); version `title`, `valid_until`, `channel`, FX lock columns,
`sent_by` / `approved_by` / `approval_note`, `updated_by`, `deleted_at` (soft-deleted drafts) and a **sent = locked**
trigger on versions and items (a sent version cannot be hard-deleted; role `booking` has no DELETE on versions); `notifications.recipient_ref` + `notification_reads` (per-staff read state);
`staff_users.preferences`; `settings` rows (`deposit_pct` 30, `quote_validity_days` 14; read-only for the app);
`audit_logs.entity_label` / `summary` + a time index.

### `bk-006` (AA-810, needs `bk-004`)

`version_item_cost_lock()` also refuses cost writes on a **sent** version (bk-004 locked approved only), so the
costs behind a sent price cannot change (S236 review finding 7).

### `bk-007` (AA-810 maker-checker, needs `bk-004`)

`policy_approvals`: the second-person approval of a `pricing_rules` or `fx_rates` row (ADR 0005 decision 8, UI spec
Q19), **append-only** for role `booking` (SELECT, INSERT), one approval per row (typed FKs `pricing_rule_id` /
`fx_rate_id`, `ON DELETE CASCADE` for the owner only). A trigger refuses an approval whose `approved_by` is the row's
`created_by`. bk-004's policy tables keep SELECT, INSERT only (no approval column, no UPDATE grant). The three bk-004
seed rules are approved as `seed:bk-004`; every other row that existed at the first apply as `backfill:bk-007`, so
quotes do not change. The code reads only **approved** rows as in force (`app/pricing/repository.py`); staff enter and
approve rows on `/pricing/rules` (`POST /booking/pricing/rules|fx-rates`, `POST …/{id}/approve`).

### `bk-008` (AA-822 wave 2a, needs `bk-001`, `bk-003`, `bk-005` only)

`suppliers` (code `SUP-000123`, `company_name`, `categories[]` from hotel / transport / guide / entrance / activity /
meal (Q20), website, `internal_notes` (staff only), contact name / title / e-mail / phone, address, region, postal
code, `destination_id` = a CIS `catalog.v_destination` id checked by the API (no FK across schemas), `country_code` →
`countries`, status `active|inactive`; unique on lower(name) + country). `supplier_contracts` (title, supplier
reference, `valid_from` / `valid_to` (NULL = open-ended), supplier `currency`, payment terms, cancellation policy,
notes, status `draft → published → expired`; CHECK: the publisher is not the draft's last writer
`coalesce(updated_by, created_by)`; trigger: a published contract only moves to expired, an expired one never
changes). Role `booking` has no DELETE on either table: suppliers deactivate (refused with 409 while a published
contract is in force or upcoming), contracts expire. Adds country `MN`. Grants per table (never `ON ALL TABLES`).

### `bk-009` (AA-679 Booking half, S237 decision D4 — needs `bk-003` only)

`trip_plans`: the **commercial-only** row of one CIS catalog tour, unique on the catalog key
`(source_system, source_id)` (`source_id` = contract catalog.tour.v1, stable across re-uploads; never `tour_id`).
Columns: `sellable`, `trip_style` (free text, Q16), `default_channel`, a price hint (amount + currency + basis, all or
nothing; never a quote), `commercial_notes` (`conf:commercial`), who / when. No catalog content is copied (O7): the
API joins `catalog.v_tour` live, so a tour that leaves the catalog drops out of every list. Never deleted (role
`booking` has no DELETE / TRUNCATE). No supplier link yet (later key).

### `bk-010` (AA-823 wave 2b, needs `bk-001`, `bk-003`, `bk-004`, `bk-005`, `bk-008`)

Supply **facts** Booking owns (root ADR 0004a decision 1); no customer-facing description, highlights or photo column
anywhere (O7, Q17; a schema test asserts it). `hotels` (code `HTL-000123`, supplier, name, style, stars 1–5,
address, `destination_id` = CIS place soft key checked by the API, country, geo point, check-in/out, amenities, staff
links, internal notes, status `active|inactive`), `hotel_room_types` (name, max occupancy, bed types, notes, status),
`services` (code `SVC-000123`, supplier, `service_type` activity / transfer / guide / entrance / meal, internal title,
pricing `unit` person / group / vehicle / day, CIS place, optional CIS `source_system`/`source_id` reference, max
group size, duration, inclusions, notes, status). Never deleted (role `booking` has no DELETE). `supplier_rates`: net
rate rows of one room type (`room_night`, meal plan) or one service (its unit) under a supplier contract (season,
validity inside the contract's, pax band, net amount in the contract currency); trigger `supplier_rate_guard`: rates
are written under a **draft** contract only, of the same supplier, in its currency, inside its validity, and are fixed
once the contract is published (maker-checker via the contract). Version items gain `hotel_room_type_id` /
`service_id` (at most one); cost lines gain `supplier_rate_id` (provenance of a defaulted net). An item that books
supply and is saved without cost lines takes the rate in force on its first day (trip start + day_from - 1) for the
case pax. Known gap (ADR 0004a d4): CIS still connects as the owner role, so the DB does not hide these rates from CIS.

### `bk-011` (AA-830, data standards X3 — needs `bk-002` only)

`trip_case_legs.source_system` / `source_id`: the catalog tour key of contract catalog.tour.v1 (DM-2.2), nullable, both
or neither, `cis` only (CHECK), indexed, no FK (soft key; the nightly schema dump reports orphans against
`catalog.v_tour`, DM-4.2). `source_tour_id` stays as TripPlanner sent it (either key kind) for compatibility; never join
on it. Filled by intake; legs opened before bk-011 are back-filled by `python -m app.inquiry.leg_keys` (DRY RUN by
default, `--apply` writes only rows still null; a superseded tour_id is not in `catalog.v_tour` and stays unmatched,
listed in the report). `/booking/health/ready` requires `bk-011`.

### `bk-012` (AA-825 retention, needs `bk-002` only)

`inbox_events.redacted_at` (NULL = payload as received) + `COMMENT ON COLUMN … payload` `pii:contact` (DM-6.1) +
`inbox_events_redact_due_idx (received_at, id) WHERE redacted_at IS NULL`; `job_finished_idx (finished_at, id) WHERE
finished_at IS NOT NULL` on `booking.job` (same as CIS `shared.job`, the X6 finished index). No grant change.
`/booking/health/ready` requires `bk-012`.

### `bk-013` (AA-751 X6, job table template catch-up — needs `bk-002` + `bk-012`)

`booking.job` gains the template columns bk-002 left out: `cancel_requested` (default false), `progress` (jsonb,
default `{}`), `cost_usd` (numeric(12,6), default 0), `parent_job_id` (FK onto `booking.job.id`, ON DELETE NO ACTION,
`job_parent_idx`). Indexes converge on the template names: `job_queued_due_idx (run_after, created_at) WHERE queued`
and `job_running_idx (kind, locked_until) WHERE running` replace bk-002's `job_due_idx` / `job_lease_idx`
(CREATE IF NOT EXISTS + DROP IF EXISTS, never RENAME: re-running every migration must converge);
`job_active_pull_uq` stays (Booking-only). Queue autovacuum `scale_factor 0.02, threshold 50`. No grant change. A
future kind that sets `parent_job_id` must accept that `bk_retention_job` never deletes a job while a child row still
points at it (its FK guard picks the self-FK up). The worker's recurring-kind prune (`app/jobs/queue.py prune`, e.g.
`bk_inquiry_pull` history) has no such guard: before any kind sets `parent_job_id` to a recurring job, add
`NOT EXISTS (SELECT 1 FROM booking.job c WHERE c.parent_job_id = booking.job.id)` to prune (`job_parent_idx` serves it),
or one ForeignKeyViolation fails the whole DELETE, escapes `maintenance()` and stops the worker claiming.
`/booking/health/ready` requires `bk-013` (since the AA-751 release-on-shutdown change).

### `bk-014` (AA-852, X1 plan S6 — needs Infra `rl-001` + CIS `243`)

Ownership only, no shape change: schema `booking`, its 32 tables + 2 views (indexes, row types and the
`trip_case_status_history.seq` identity sequence follow), 5 standalone sequences and 12 trigger functions → `bk_owner`;
default privileges for role `booking` restated `FOR ROLE bk_owner` (bk-001's three rows), the `FOR ROLE aa_cis_admin`
rows revoked. Guards: refuses without `bk_owner`/membership/CIS 243 grants; raises if any object in `booking` is still
not owned by `bk_owner` (an object newer than its list), if a SECURITY DEFINER function exists, if the default
privileges are not exactly bk-001's rows, or if any non-owner privilege differs from a snapshot taken at the start.
Runs only as `aa_cis_admin` (first guard): idempotent as the master (the CI re-run), never re-run by DB Migrate after
the transfer (it would run as `bk_owner` and refuse; a forced redo is a `roles/` file). Locks: one up-front `LOCK TABLE`
of the 32 tables + `v_customer_summary` (hot tables first, app inbox order), held until COMMIT, all or none within
`lock_timeout` 5 s. Ownership only: the CIS runtime still reaches `booking.*` through `aa_cis_admin`'s membership in
`bk_owner` until X1 S18/S19 (see "Ownership only; not isolation yet" above).
Applied in the X1 S8 train (`rl-001 → 243 → bk-014 → tp-013`) with `booking.job` idle. Not added to
`REQUIRED_SCHEMA_VERSIONS` here (migration-only PR, MIG-4); readiness keeps working because the view's new owner has
the 243 SELECT grant.

### `bk-016` (AA-680 Phase 2, root ADR 0006 / 0006a — needs `bk-003` only)

`staff_users.cognito_iss` / `cognito_sub`: the linked Cognito WORKFORCE identity, a pair (both NULL or both set, issuer
`https://…`, sub 1–128 chars; same check as CIS `shared.admin_users`), UNIQUE `(cognito_iss, cognito_sub)` (NULL pairs
are distinct, so unlinked rows coexist). `cis_user_id` stays the primary key and every staff FK target; it is now
**the Booking staff key** under its legacy name: CIS `admin_users.id` for bridge-era rows, `'cog:' || sub` for rows
created on a WORKFORCE sign-in, and `staff_users_cognito_key_chk` makes a `cog:` key always carry its own sub.
Bridge-era rows are linked once by AA-CIS-Infra `db/scripts/aa680_link_staff_cognito.py` (DB Migrate run-script, DRY
RUN first) from `shared.admin_users.cognito_iss / cognito_sub`. No grant change (bk-003 grants are table-level). Not
added to `REQUIRED_SCHEMA_VERSIONS` in its own PR (migration-only); the AA-680 code PR adds it after the apply, so
`/booking/health/ready` requires `bk-016` from then on.

### `bk-017` (AA-858 ops overview — needs `bk-003`, `bk-005` and Infra `rl-001` (role `cis_app`))

Two read-only aggregate views for the CIS admin overview (contract `ops.overview.v1`, AA-Ecosys `docs/contracts/`):
`v_ops_funnel_daily (day, metric, source_system, n)`, UTC days, last 400 days, metrics `inquiry_received` /
`inquiry_parked` / `inquiry_failed` (inbox rows of contract `inquiry.submitted`, by `received_at`, parked/failed = current
status), `trip_case_created` (not soft-deleted cases, by `created_at`), `proposal_sent` / `confirmed` / `cancelled` (the
**first** time a not soft-deleted case reached that status in `trip_case_status_history`); `v_ops_case_status (status,
n)`: not soft-deleted cases by current status. Aggregates only (no id, name, e-mail, payload or free text). Owned by
`bk_owner`; role `booking` keeps SELECT only; `cis_app` gets USAGE on schema `booking` + SELECT on the two views. The
CIS runtime still logs in as `aa_cis_admin` until X1 S18 and reads them through its `bk_owner` membership. Metric names
are append-only. Applied after `bk-016` (AA-680). No readiness change (`REQUIRED_SCHEMA_VERSIONS` untouched: the Booking
app does not read the views).

Waiting for decisions: post-sale status edges (AA-682), `trip_case_countries` / `areas` / `country_profiles` (O7,
ISO mapping), the trip plan price matrix / `v_from_prices` and catalog-backed version item types (AA-679 follow-up),
operations / money (AA-682).

## Out of scope today

Post-sale status edges, **customer e-mail** (proposal send writes a customer notification row only and says so; the
only e-mail today is the staff new inquiry e-mail, AA-811), Booking-side SES bounce/complaint suppression, a catalog
sync / outbox consumer (D4: live reads instead), catalog titles on trip case legs (Q11), the trip plan price matrix
and `v_from_prices`, destinations / countries pages, rate-sheet upload (rates are entered one by one under a draft
contract), the supply outbox / `v_supply_*` views / `supply.hotel.v1`·`supply.service.v1` contracts (ADR 0004a, own
issue), supplier media (`entity_media`), contract document upload, supplier hard delete,
editing or withdrawing a pending pricing rule / FX row (append-only: enter a new row with another date), any use of
`shared.job`, the `trip_case.status_changed` back-channel, CIS grant migrations, the frontend's custom domain (Vercel
project `aa-booking-admin` exists since S236, root `frontend/`).

## AI boundary for high-risk advice (AA-820)

AA-Booking must not use AI to advise travellers on insurance (choice, underwriting or claims), financing or
instalments, refunds or claims decisions, or visas and immigration, unless a qualified staff member reviews each
recommendation, with authority to change it, before it is sent, **and** the traveller is clearly told that AI was
used. AI-drafted proposals and quotes stay behind the existing staff review before sending. Any traveller-facing chat
must disclose AI at the start of each session. Source: Anthropic Usage Policy high-risk use case requirements
(effective 12/11/2026); proposal wording in root `docs/legal/tenant-terms-ai-clause-draft.md` §5, may be tightened
after the business review. Today Booking makes no AI calls, so this is a rule for future features.

## Staff admin frontend (AA-683)

- `frontend/` is a Next.js app for Booking staff (UI spec S236). The browser only talks to its own BFF.
- **Sign-in (AA-680 Phase 2): "Sign in with company email"** = Cognito WORKFORCE managed login on
  `staff-auth.lumiguides.it.com` through the confidential **booking-staff** client (authorization code + PKCE S256;
  `lib/cognito.ts`, ported from CIS `frontend/lib/cognito.ts`): `GET /api/auth/signin/cognito?next=…` →
  `GET /api/auth/callback/cognito` (state / nonce / iss / client checks, then `GET /booking/staff/me` decides: 403
  `no_booking_role` → no session, refresh token revoked, `/login?reason=no_booking_role`) → cookies `bk_access` (the
  access token; the cookie lives for the 12 h session) + `bk_staff_rt` (refresh token, path `/api`). `proxy.ts` sends a
  page whose Cognito token is under 2 min from expiry to `GET /api/auth/refresh?next=…`; the BFF proxy refreshes
  itself (single-flight). `POST /api/auth/logout` revokes the refresh token and returns the Cognito `/logout` URL.
  Offered only on the registered hosts with `COGNITO_BOOKING_STAFF_CLIENT_SECRET` set (Production AND Preview
  targets): a Vercel production build accepts `booking.lumiguides.it.com` and `aa-booking-admin.vercel.app`, a
  preview build only the fixed Preview host `booking-preview.lumiguides.it.com`, `next dev` also `localhost:3001`
  (`VERCEL_ENV` picks the list). Per-deployment `*.vercel.app` preview URLs get only the legacy form; the Preview
  host never shows the legacy form and answers 404 on `POST /api/auth/login`.
- **Preview sign-in (AA-911, root ADR 0006a A11):** `booking-preview.lumiguides.it.com` is a Vercel domain of
  `aa-booking-admin` (bound to the never-pushed branch `preview-signin`, so production deploys never claim it) and a
  registered `booking-staff` callback / sign-out URL. The Vercel Deploy workflow points it at a preview only on
  opt-in: the PR carries the label `preview-signin` when its preview deploys, or a dispatch with
  `alias_only_url=<https://aa-booking-admin-….vercel.app>` (checked over the Vercel REST API: this project, not
  production, READY). Preview builds send `X-Robots-Tag: noindex, nofollow`. Under Standard Deployment Protection the
  host is most likely behind Vercel Authentication (Hobby: only the owner passes; automation needs the project's
  protection-bypass value). Unmerged same-repo PR code on that host sees the Booking tokens of whoever signs in
  (accepted for Dev; separate preview client before prod / first paying tenant). Runbook: root
  `docs/ops/runbooks/preview-staff-sign-in.md`.
- **Legacy CIS password bridge (until AA-680 PR-4)**: `POST /api/auth/login` (CIS `/auth/admin-login` →
  `/auth/booking-token`) → `bk_cis_session` + `bk_access`; `proxy.ts` and the BFF re-mint the 15-minute Booking token
  from the CIS session. On hosts with company sign-in the form sits collapsed under the company button (E2E account).
  One session kind at a time: each sign-in clears the other kind's cookies.
- `/api/booking/<path>` → `<BOOKING_API_URL>/booking/<path>` with the access token as Bearer. Tokens live only in
  httpOnly cookies. Booking roles of company sign-in profiles are read-only on `/staff` (set in CIS → Platform →
  Staff, API 409 `managed_in_directory`); Activate / Deactivate stays.
- `frontend/ui/` holds byte copies of the CIS kit (`app/_kit`, `_brand/tokens.ts`, the `--aa-*` blocks of
  `globals.css`) with the source commit in each header. Brand values change in CIS first, then are copied again.
  Booking-only components are in `frontend/ui/booking/` and use Tailwind utilities mapped to the same variables.
- Auth hardening: `lib/safeNext.ts` (post-login target stays same-origin), `lib/sameOrigin.ts` (BFF writes, login and
  logout refuse cross-origin requests), `ui/booking/QueryProvider.tsx` (a 401 anywhere goes to `/login?next=…`; 4xx is
  never retried). The CIS `ui/Providers.tsx` copy stays byte-comparable but is not mounted.
- Every route of UI spec §3 exists. Wave 1 pages, `/audit-log` (S237, over `GET /booking/audit-logs`) and
  `/pricing/rules` (S237, AA-810: rules + FX tabs, new-row drawers, Approve), `/suppliers` (S237, AA-822: list with
  KPIs, add / edit form with the CIS place typeahead, detail with Profile + Contracts + Rates tabs (AA-823: rates per
  contract, editable while the contract is a draft); Bookings / Bills tabs are frames), `/catalog/trip-plans` (S237,
  AA-679: list, detail with CIS days / photos tagged "From CIS", commercial drawer, "Use for trip case" modal; CIS
  photos load from the CIS public API, `lib/config.ts` `CIS_MEDIA_URL`), `/hotels` (S237, AA-823: grid + hotel drawer
  with facts, room types, rates, activity; `?hotel=HTL-…`) and `/services` + `/services/{SVC-…}` (S237, AA-823) read
  the real API; the proposal item drawer can book a room type or service (its net defaults from the rate); the other
  wave 2/3 pages render their layout frame with "Coming in wave N". Each nav entry in
  `frontend/ui/nav.tsx` carries its own `shipped` flag (set it when that page reads the API), so pages ship one at a
  time. The UI spec lives in `docs/ui-spec.md` (AA-683).
- Environment (server): `BOOKING_API_URL` (default: the Dev execute-api stage), `CIS_API_URL` (default: the live CIS
  API), `COGNITO_BOOKING_STAFF_CLIENT_SECRET` (secret; unset = no company sign-in), optional
  `COGNITO_BOOKING_STAFF_CLIENT_ID` / `COGNITO_WORKFORCE_DOMAIN` / `COGNITO_WORKFORCE_ISSUER` (Dev defaults in code).
  Public: `NEXT_PUBLIC_CIS_ADMIN_URL`. Staff never sign in with Google (ADR 0006a A9): the AA-703 Google placeholder is
  removed.
- Frontend deploys (S241): CI-created via `.github/workflows/vercel-deploy.yml` (Vercel project `aa-booking-admin`),
  only when a PR or a push to `main` touches `frontend/**` — PR → preview + one PR comment with the URL (and whether
  the Preview sign-in host now serves it), `main` → production. The Vercel Git integration is off via `frontend/vercel.json` (`git.deploymentEnabled: false`) because
  the Hobby team cap (~100 deployments / 24 h for all projects, canceled ones included) was being used up. Needs the
  repo secret `VERCEL_TOKEN`.

## Working rules

- Trunk-based: branch → PR → 5 required checks (Lint, Security Audit, Unit Tests, Integration
  Tests, Docker Build Check) → human merge to `main`. Agents never merge.
- Commits: `feat|fix|chore|refactor|test|docs: <english>` + body `Refs AA-xxx` (never `Fixes` /
  `Closes`, never `[AA-xx]` in the subject).
- Everything in Git is English. Done = merged + CI green + Dev rollout COMPLETED + live
  `/booking/health/ready` verified (`aa-ship`).
- A new contract version: copy the root file byte-for-byte into `backend/app/contracts/`, pin its
  SHA-256 in `tests/unit/test_inquiry_contract.py`, accept every minor of the major (contract rule 2).
