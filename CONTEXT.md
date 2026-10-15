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
- **Trusts** the CIS staff login (ADR 0004 O1): CIS issues an RS256 JWT with `aud=booking`; Booking
  verifies it against the CIS JWKS and keeps only a thin staff profile (`booking.staff_users`).
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
| Frontend | Staff admin in `frontend/` (AA-683): Next.js 16 App Router, React 19, TypeScript, Tailwind v4 on the copied CIS kit tokens, react-query, TanStack Table. Vercel project not created yet. Customer surface (O8) not started |

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
  app/auth/                staff JWT + JWKS cache (tokens.py), profile/provisioning/permissions (staff.py), admin ops
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
  app/jobs/                booking.job queue (claim/lease/reap/backoff) + Worker loop (O3)
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
  tests/integration/       real Postgres: applies migrations/, checks role privileges, /ready
  Dockerfile               python:3.12-slim, port 8000, non-root, stdlib healthcheck
migrations/NNN_*.sql       schema booking, key bk-NNN (byte-copied to AA-CIS-Infra)
docs/adr/                  repo ADRs (0001 implements root ADR 0004)
docs/ui-spec.md            staff admin UI spec (AA-683; routes, waves, API per route)
docs/implementation-notes/ per-issue decisions (AA-678.md, AA-679.md, AA-681-slice2.md, AA-681-slice3.md, AA-681-w1.md,
                           AA-683.md, AA-810.md, AA-811.md, AA-822.md, AA-823.md)
.github/workflows/         ci.yml (5 required checks + non-required Frontend Build), deploy-dev.yml
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
`STAFF_JWT_ISSUER` (optional; default to the Dev CIS, `https://api-cis.lumiguides.it.com[/auth/.well-known/jwks.json]`).
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

## Staff authentication and permissions (AA-680, ADR 0004 O1)

- Every `/booking/*` route except `/booking/health*` needs `Authorization: Bearer <CIS staff JWT>`: RS256,
  `kid` header, `iss` = `STAFF_JWT_ISSUER`, `aud` = `booking`, `exp`, `sub` = CIS `admin_users.id`, claims
  `email`, `name`, `cis_role` (`admin|reviewer|content`). The JWKS is cached 10 min; an unknown `kid` refetches
  (at most every 30 s); with no key set at all the answer is 503, a bad token is 401.
- First valid token → `booking.staff_users` row: CIS `admin` → active Booking `admin`; `reviewer` → `sales`,
  `content` → `content_creator`, **inactive** (403 `staff_inactive`) until a Booking admin activates them
  (`PATCH /booking/staff/{cis_user_id}`). Role and `active` are owned by Booking admins; later tokens only refresh
  email/name/cis_role/last_seen_at.
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
  `version.write` (admin, sales); block / archive customers, staff admin, audit log read = admin. Notifications,
  settings, countries, the catalog place picker (`/booking/catalog/destinations`), own preferences and own staff
  detail = any active staff.
- Every staff write adds one `booking.audit_logs` row (`actor_ref` = CIS user id).

## Deferred work (`booking.job`, O3/O9)

Booking owns its job table `booking.job` (same shape and semantics as CIS `shared.job`, ADR 0003), so
Booking jobs never block the CIS deploy guard. Kinds are `bk_*`. On Dev the worker loop runs inside the
API process as one lifespan background task (never in a request path), one job at a time; overlapping
tasks during a rolling deploy are safe (`SKIP LOCKED`, idempotent handlers).

| Kind | What |
|---|---|
| `bk_inquiry_pull` | Recurring singleton (every ~60 s): reads `tripplanner.v_inquiry_outbox`, anti-joins `booking.inbox_events`, inserts new rows `ON CONFLICT DO NOTHING`, queues one process job each. `booking.inbox_cursor.safe_id` is only a scan bound, advanced behind a 600 s window (contract rule 4). |
| `bk_inquiry_process` | Validates v1/v1.1/v1.2; each leg gets the catalog tour key `(source_system, source_id)` from the v1.2 payload or by resolving `source_tour_id` against `catalog.v_tour` (null when unresolved; AA-830); creates a **lead** customer (`verified = false`, never merged into an existing customer; `needs_merge_review` + `merge_candidate_id` on an email/phone match), the trip case + legs, a staff `notifications` row (+ one `bk_notify_email` job for it, same transaction) and an audit row; marks the inbox row `processed`. `customer: null` or no email/phone → `parked`; invalid → `failed`. |
| `bk_notify_email` | One per new inquiry notification (idempotency key `bk_notify_email:<notification_id>`): reads the notification, trip case, lead customer, legs and (live, soft) the CIS `catalog.v_tour` titles/countries, sends one e-mail to `STAFF_NOTIFY_EMAIL` through SES; result `{outcome: sent, message_id, ...}`. Not configured → `skipped` with the reason; an SES refusal that cannot change (rejected, sender not allowed, paused) fails at once, throttling / network errors retry with backoff. At least once: a worker that dies after SES accepted the message re-sends it. |

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

## Staff admin frontend (AA-683)

- `frontend/` is a Next.js app for Booking staff (UI spec S236). The browser only talks to its own BFF:
  `POST /api/auth/login` (CIS `/auth/admin-login` → `/auth/booking-token`), `POST /api/auth/logout`, and
  `/api/booking/<path>` → `<BOOKING_API_URL>/booking/<path>` with the Booking JWT as Bearer. Tokens live only in
  httpOnly cookies (`bk_cis_session`, `bk_access`); `proxy.ts` and the BFF re-mint the 15-minute Booking token
  from the CIS session.
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
  API). Public: `NEXT_PUBLIC_CIS_ADMIN_URL`, `NEXT_PUBLIC_GOOGLE_SIGNIN_URL` (unset = no Google button, AA-703).

## Working rules

- Trunk-based: branch → PR → 5 required checks (Lint, Security Audit, Unit Tests, Integration
  Tests, Docker Build Check) → human merge to `main`. Agents never merge.
- Commits: `feat|fix|chore|refactor|test|docs: <english>` + body `Refs AA-xxx` (never `Fixes` /
  `Closes`, never `[AA-xx]` in the subject).
- Everything in Git is English. Done = merged + CI green + Dev rollout COMPLETED + live
  `/booking/health/ready` verified (`aa-ship`).
- A new contract version: copy the root file byte-for-byte into `backend/app/contracts/`, pin its
  SHA-256 in `tests/unit/test_inquiry_contract.py`, accept every minor of the major (contract rule 2).
