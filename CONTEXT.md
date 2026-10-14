# AA-Booking — Repo Context

AA-Booking (AAA) is the booking back office of the Adventure Asia ecosystem: customers (OTP
identity), trip cases opened from TripPlanner inquiries, proposals, suppliers, invoices and
payments. It is built in-house from scratch (no vendor code was delivered). Today the repo holds
only the **bootstrap health slice**: a FastAPI service with liveness/readiness endpoints, the 5-check
CI, the Dev deploy workflow and migration `bk-001`.

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
- **Consumes** (later, through owner grants on views): CIS catalog read model (`catalog.tour.*`,
  AA-679), CIS golden records (`shared.destinations`, photo views), TripPlanner
  `tripplanner.v_inquiry_outbox` (`inquiry.submitted` v1, contract in
  `AA-Ecosys/docs/contracts/`).
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
| Frontend | Next.js on Vercel — **not started** (waits for staff identity O1 and customer surface O8) |

## Layout

```text
backend/
  app/main.py              FastAPI app factory; every path is under /booking
  app/routers/health.py    GET /booking/health, GET /booking/health/ready
  app/db.py                lazy asyncpg pool + readiness probe (REQUIRED_SCHEMA_VERSION)
  app/settings.py          environment → Settings
  tests/unit/              unit tests + migration guard (tests/migration_guard.py)
  tests/integration/       real Postgres: applies migrations/, checks role privileges, /ready
  Dockerfile               python:3.12-slim, port 8000, non-root, stdlib healthcheck
migrations/NNN_*.sql       schema booking, key bk-NNN (byte-copied to AA-CIS-Infra)
docs/adr/                  repo ADRs (0001 implements root ADR 0004)
docs/implementation-notes/ per-issue decisions (AA-678.md)
.github/workflows/         ci.yml (5 required checks), deploy-dev.yml
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
`GIT_SHA` (baked into the image), `DB_SSL` (only `test`/`local` may set `disable`).

## Health contract (ADR 0004 decision 9)

- `GET /booking/health` → 200 `{"status":"ok","service":"aa-booking-api","git_sha":"<sha>"}`.
  No DB call. Used by the ALB target group, so a DB outage never trips the ECS circuit breaker.
- `GET /booking/health/ready` → 200 `{"status":"ok","db":"ok","schema_version":"bk-001"}` when the
  DB answers and the newest registered `bk-NNN` ≥ `REQUIRED_SCHEMA_VERSION`; otherwise 503 with
  `db` = `ok` | `error` | `not_configured`. Bodies never contain the DSN or error messages.

## Database rules

- Migrations live in `migrations/`, are reviewed here, then **byte-copied** to
  `AA-CIS-Infra/db/migrations/booking/` and applied by the **DB Migrate** workflow (check → apply).
  Never applied from a laptop. Apply a migration before merging code that needs it, then bump
  `REQUIRED_SCHEMA_VERSION`.
- DB Migrate runs as the RDS master user `aa_cis_admin`, so every `booking.*` object is owned by it.
  Default privileges therefore say `FOR ROLE aa_cis_admin` (ADR 0004 decision 6).
- The app connects as role `booking`: `USAGE` on schema `booking` only (no `CREATE`), DML on its
  tables, `audit_logs` append-only, views read-only. Its LOGIN password is set out-of-band by
  Nghiep and stored only in Secrets Manager `booking/dev/database-url`. **No password in Git.**
- The migration guard (unit test, required check) rejects any file that is not wrapped in
  `BEGIN/COMMIT`, does not register its own `bk-NNN`, touches anything outside schema `booking`
  (the `shared.schema_versions` row is the one exception), creates logins/passwords/extensions/
  `SECURITY DEFINER` functions, grants to anyone but `booking`, or leaves a view writable.
- Grants on other apps' objects are written by **their** owners, in their migrations, unguarded,
  after role `booking` exists (ADR 0004 decision 6).

### `bk-001` (first slice, ADR 0004 decision 7)

Schema + role + grants + default privileges; `customers`, `customer_otps`, `customer_sessions`,
`media_files`, `audit_logs`, `inbox_events` (table only); `v_schema_versions` (Booking's rows of
`shared.schema_versions`, for readiness). No enum types yet.

Waiting for decisions: `staff_users` (O1 staff identity), `trip_cases*` + status enums (O6 guest
inquiries, status vocabulary), `areas` / `country_profiles` (O7 content boundary, ISO mapping),
catalog / `trip_plans` (AA-679), versions / operations / money (AA-681/682).

## Out of scope today

Staff tables and staff endpoints, catalog sync, the inquiry intake consumer, any job (no
`shared.job` use until the CIS deploy guard filters by kind — ADR 0004 decision 8), CIS/TripPlanner
grant migrations, the frontend and its Vercel project, a custom domain.

## Working rules

- Trunk-based: branch → PR → 5 required checks (Lint, Security Audit, Unit Tests, Integration
  Tests, Docker Build Check) → human merge to `main`. Agents never merge.
- Commits: `feat|fix|chore|refactor|test|docs: <english>` + body `Refs AA-xxx` (never `Fixes` /
  `Closes`, never `[AA-xx]` in the subject).
- Everything in Git is English. Done = merged + CI green + Dev rollout COMPLETED + live
  `/booking/health/ready` verified (`aa-ship`).
