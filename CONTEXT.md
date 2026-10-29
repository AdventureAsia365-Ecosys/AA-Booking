---
id: ARCH-06-booking
title: "AA-Booking — Repo Context"
description: Stack, boundaries, ownership, domain terms and working rules of AA-Booking (booking API, booking job worker and staff admin); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-booking-api, aa-booking-admin]
last_reviewed: 2026-10-27
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

Architecture diagram (AWS icons): `AA-Ecosys/docs/architecture/diagrams/booking.svg`, drawn from `booking.py` in the
same folder; the catalog page `AA-Ecosys/docs/architecture/diagrams/README.md` holds every view and is shown in the CIS
admin at `/admin/docs?path=docs/architecture/diagrams/README.md`.

- **Owns** schema `booking.*` on the shared acc2 RDS (`aa-cis-dev-db`, PostgreSQL 15), migration key
  `bk-NNN`, and the **customer identity** (OTP) of the ecosystem (ADR 0001 decision 6).
- **Consumes** through owner grants on views: TripPlanner `tripplanner.v_inquiry_outbox`
  (`inquiry.submitted` v1 / v1.1 / v1.2 / v1.3 / v1.4, contract in `AA-Ecosys/docs/contracts/`, grant = TripPlanner `tp-009`) —
  pattern outbox → pull job → `booking.inbox_events`. CIS catalog read model (contract `catalog.tour.v1`, CIS
  migration 225 grant to role `booking`), **read live** at request time (no copy, no outbox consumer):
  `catalog.v_destination` (supplier place picker and check, AA-822; Catalog → Destinations / Countries, AA-996, with
  `v_destination_photo` feature-detected) and `catalog.v_tour` / `v_tour_day` /
  `v_tour_photo` (trip plan endpoints, AA-679, S237 decision D4). **Tour liveness** (AA-986 L5, root ADR 0015
  decision 5, contract `catalog.tour.v1.3`, CIS migration 274): `catalog.v_tour_retired` (tombstone: `retired` /
  `paused` + reason; a `restored` row of a key served again is no tombstone), read live next to `v_tour` (`app/catalog/liveness.py`), and `catalog.v_tour_lifecycle_outbox`,
  pulled by `bk_tour_lifecycle_pull` into `booking.inbox_events` (source `cis`, contract `catalog.tour`) for staff
  notifications. Both are feature-detected: without CIS 274 Booking degrades to `v_tour` alone. **Supply content
  read-back** (AA-835 S4, root ADR 0004a decision 4, CIS migration 278): `catalog.v_supply_content`, read live
  (`app/supply/content.py`): the hotel / service detail shows what CIS published (status, text, photo URLs, the CIS
  `edit_url` path for "Edit in CIS") and proposal items carry only **published** description and highlights. Also
  feature-detected: until CIS 278 is applied (or when the read fails with any database error, such as a column type
  CIS changed) `content` is null and the staff UI hides the panel; a CIS migration never fails a Booking request.
  `catalog.v_tour_place_name` (CIS 298, AA-996: list destinations; the names and positions of `v_tour_place` without
  its per-place card / destination / segment lookups) is read too; the list never reads `v_tour_place`.
  **Parse-backed views** (`v_tour_day`, `v_tour_place`, `v_tour_place_name`; their day text parses every live
  itinerary, `DISTINCT ON (tour_id, …)` behind a security barrier) are read **by `tour_id` only** (`= $1` or
  `= ANY($1::uuid[])`), once per page, never per row and never by `source_id` (#130: ~1.5 s a read, list 503).
  Enforced by the AA-996 perf guard (`tests/catalog_perf.py`: a static scan of `app/`, and the integration stub with
  the live plan shape, whose `EXPLAIN` is checked for every such statement any integration test runs). `v_tour_place`
  cost ~10 ms a tour on Dev even keyed by `tour_id` (380 ms for 25, 994 ms for 100, S255); the bounds kept as the
  safety net: destinations in chunks of 25 tours (each under the 1 s extra timeout), all chunks of a page inside a 2 s
  budget, with a 5-minute per-process cache by `tour_id`; a chunk that fails or does not fit leaves only its rows
  `null` (`degraded`). Later: CIS golden records.
- **Trusts** the Cognito WORKFORCE staff pool (root ADR 0006 / 0006a, AA-680 Phase 2): Booking verifies WORKFORCE
  access tokens of the `booking-staff` and `breakglass` clients and takes the Booking role from the `booking-*` groups
  (granted in CIS → Platform → Staff). During the transition it still accepts the CIS bridge token (ADR 0004 O1: CIS
  issues an RS256 JWT with `aud=booking`, verified against the CIS JWKS). Booking keeps only a thin staff profile
  (`booking.staff_users`) and never reads the CIS staff directory.
- **Publishes** to CIS (AA-858, contract `ops.overview.v1`, root ADR 0008 §5): two aggregate views in its own schema,
  `booking.v_ops_funnel_daily` (per UTC day, metric, source system) and `booking.v_ops_case_status` (cases per current
  status), `bk-017`, SELECT to the CIS runtime role `cis_app` (plus USAGE on schema `booking`); the CIS admin overview
  reads them. **Supply facts** (AA-835, root ADR 0004a, contracts `supply.hotel.v1` / `supply.service.v1`, `bk-023`):
  the `supply` stream of `booking.outbox` read through `booking.v_supply_outbox`, and the facts-only views
  `v_supply_hotels` / `v_supply_services` / `v_supply_media`, SELECT to `cis_app`; CIS drafts the customer content from
  them. **Data coverage** (AA-992, contract `ops.data_coverage.v1`, `bk-025`): `booking.v_ops_data_totals` (metric, n:
  trip plans / sellable, plan components, open departures, active suppliers, published contracts, current rates, media
  with rights, customers, B2B partners / holds; aggregates only) and `booking.v_ops_trip_plan_keys` (catalog key +
  sellable of each plan; no plan id, no `conf:commercial` column), SELECT to `cis_app`; the CIS admin page
  `/admin/data-coverage` reads them. No other `booking.*` object is granted to CIS (isolation probe: `cis_app` →
  `booking.customers` 42501).
- **Serves** partner agencies through AA-Partner (AA-972, root ADR 0005a / 0006b, contract `booking.b2b.v1`): the B2B
  API `/booking/b2b/v1` takes only client-credentials tokens of the Cognito M2M pool `aa-m2m-<env>` plus the agency's
  `X-AA-Tenant-Id`, checked against `booking.b2b_partners` (see "B2B API" below). The agency-facing price is
  `b2b_price`; the supplier net never leaves Booking. Fixed departures, holds and their confirmation into a **B2B
  trip case** (no customer, the agency as `party_tenant_id`) are AA-973 (bk-020); VND quotes, cancellation fees, the
  one-time hold extension and the re-hold limit are AA-974 (bk-021); the event feed `GET /booking/b2b/v1/events`
  over the shared outbox `booking.outbox`, the on-request desk and the departure incident flag (staff admin "B2B
  desk") are AA-975 (bk-022).
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
| Frontend | Staff admin in `frontend/` (AA-683): Next.js 16 App Router, React 19, TypeScript, Tailwind v4 on the UI kit core tokens (`frontend/ui/core`, root ADR 0016), react-query, TanStack Table. Vercel project `aa-booking-admin`, deployed by CI (`.github/workflows/vercel-deploy.yml`, see Staff admin frontend). Customer surface (O8) not started |

## Layout

```text
backend/
  app/main.py              FastAPI app factory; every path is under /booking
  app/routers/health.py    GET /booking/health, GET /booking/health/ready
  app/routers/docs.py      GET /booking/openapi.json, GET /booking/docs (staff.admin outside test/local, AA-876)
  app/routers/staff.py     GET /booking/staff/me, admin GET/PATCH /booking/staff (PATCH `reason`: audit row only, AA-996)
  app/routers/trip_cases.py  staff trip case + version endpoints (/booking/trip-cases*, /booking/versions)
  app/routers/customers.py  /booking/customers* (directory, detail, manual lead, edit/block, archive)
  app/routers/notifications.py  /booking/notifications* (staff feed, per-staff read state)
  app/routers/reference.py  /booking/settings, /booking/countries, /booking/audit-logs (admin; `q` text search, AA-996)
  app/auth/                identity_verifier.py (byte copy of AA-CIS-App api/core/identity_verifier.py, the multi-issuer
                           verifier; never edited here), identity_config.py (accepted issuers + clients, JWKS prefetch,
                           AA-680), profile / Cognito identity link / permissions (staff.py), admin ops (admin.py),
                           deploy workflow GitHub OIDC verifier (ci.py, AA-751) over the single-issuer cache (tokens.py)
  app/auth/m2m.py          aa-m2m client-credentials tokens for the B2B API (AA-972): IssuerConfig over the vendored
                           verifier (scope claim as groups), 401 / 403 split, scope constants
  app/auth/customer.py     signed-in travellers for the customer routes (AA-682 S5): EXTERNAL issuer x {tripplanner,
                           booking-customer} over the vendored verifier, 401 sign_in_required / 503 auth_unavailable
  app/b2b/                 booking.b2b.v1 (AA-972): access.py (scope + partner check), errors.py (contract Problem),
                           components.py (component ids, net cost of a line from rates in force), quotes.py (price,
                           price_hash, append-only store), reads.py (products, FX rates, price parity), partners.py and
                           plan_components.py (staff admin); AA-973: departures.py (capacity, held / sold counted under a
                           departure row lock, line availability, staff departure edits), holds.py (place / read /
                           release / confirm → B2B trip case + approved version), expiry.py (bk_b2b_hold_expire job + hook);
                           AA-974: holds.py also extend once + re-hold limit, cancellation.py (fee policies: tiers,
                           in force, staff admin; cancel / dry run → b2b_cancellations)
  app/routers/b2b.py       /booking/b2b/v1/products, /quotes, /fx-rates, /price-parity (M2M only, AA-972); /departures,
                           /holds, /holds/{id} (GET, DELETE), /holds/{id}/confirm (AA-973); /holds/{id}/extend,
                           /bookings/{id}/cancel (AA-974)
  app/routers/b2b_partners.py  /booking/b2b-partners (GET, POST, PATCH /{id}; b2b.admin = admin)
  app/routers/ops.py       GET /booking/ops/deploy-gate (machine route: Deploy Dev workflow OIDC token only, AA-751)
  app/ops/                 AA-682 operations after the sale (bk-026): supplier_bookings.py (generate / add / edit /
                           request / confirm / decline / cancel / re-request, supply state, case auto-advance, case-cancel
                           cascade, B2B line tie-in), files.py (ops-docs bucket: base64 upload, attachment download);
                           S2 (bk-027): trip_checks.py (list / case / custom / patch, derived checks, B2C-only emergency
                           contact), lifecycle.py (bk_case_lifecycle + bk_ops_reminders, OpsDailySchedule hook);
                           S3 (bk-028): invoices.py (create / edit draft / send / cancel / amend / remind, case Billing
                           tab, KPIs, case cancel cascade), payments.py (record / edit / verify / void, maker-checker),
                           money.py (Decimal amounts, default schedule, waterfall, request hash, card refusal),
                           track.py (case auto-advance over the supply and money tracks);
                           S4 (bk-030): supplier_bills.py (enter / edit / approve / cancel bills, payouts and their
                           void, billable bookings, KPIs, case cost vs billed)
  app/routers/billing.py   /booking/invoices (GET, GET/PATCH/DELETE /{number}, …/send|cancel|amend|remind|payments),
                           /booking/trip-cases/{ref}/billing (GET), /booking/trip-cases/{ref}/invoices (POST),
                           /booking/payments (GET, GET/PATCH /{ref}, …/verify|void), /booking/billing/summary —
                           billing.read / billing.write / payment.verify (AA-682 S3)
  app/routers/customer.py  /booking/customer/v1/payments, /booking/customer/v1/invoices/{number} — My Payments for a
                           signed-in traveller (AA-682 S5, contract booking.customer.v1; scope in app/ops/my_payments.py)
  app/routers/supplier_bookings.py  /booking/trip-cases/{ref}/supplier-bookings (GET, POST, …/generate),
                           /booking/supplier-bookings (list, summary, GET/PATCH /{id}, …/request|confirm|decline|cancel|
                           rerequest), /booking/ops-files (POST, /{id}/download, /{id}/remove) — ops.read / ops.write
  app/routers/trip_checks.py  /booking/trip-checks (GET, PATCH /{id}), /booking/trip-cases/{ref}/checks (GET, POST),
                           /booking/case-lifecycle/runs (GET) — ops.read / ops.write (AA-682 S2)
  app/trip_cases/          trip case service (list, summary, detail, activity, create, edit, status, assign),
                           versions (items upsert by id, send, approve, soft delete, cross-case list), sql.py fragments
  app/customers/           customers over booking.v_customer_summary (status lead/active/past), leads, archive
  app/notifications/       staff feed + notify helpers (service.py, rows); staff_email.py = bk_notify_email, the
                           "new inquiry" e-mail to the staff inbox (AA-811)
  app/mailer.py            SES v2 send helper (boto3 sesv2 send_email, From + configuration set from settings, AA-811)
  app/paging.py            keyset cursors (next_before) + ILIKE escaping; app/reference.py settings/countries
  app/audit.py, errors.py  audit_logs writes; service errors → HTTP 404/409/422
  app/db.py                lazy asyncpg pool + readiness probe (every REQUIRED_SCHEMA_VERSIONS row)
  app/observability/       AA-871: JSON logs (log_config.py), X-Request-ID middleware (request_id.py), PII / secret
                           redaction (scrub.py), Sentry init + before_send (sentry.py); set up once by main.build_app()
  app/settings.py          environment → Settings
  app/jobs/                booking.job queue (claim/lease/reap/backoff/release) + Worker loop (O3, maintenance hooks,
                           release on shutdown, AA-751); kinds.py (EXPECTED_SECONDS per kind, DEPLOY_GATE_EXEMPT,
                           DEPLOY_BLOCKING_KINDS, TEST_KINDS); noop_sleep.py (Dev-only test kind bk_noop_sleep, only
                           with JOB_TEST_KINDS, AA-993 G4 proof) +
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
                           POST …/{source_id}/trip-cases (AA-679, bk-009); GET|PUT …/{source_id}/components (AA-972);
                           GET|POST …/{source_id}/departures, PATCH /booking/catalog/departures/{id} (AA-973);
                           GET /booking/catalog/geo/destinations[/{id}], GET /booking/catalog/geo/countries[/{iso}]
                           (catalog.read), PATCH of both (catalog.admin) (AA-996, bk-029)
  app/catalog/geo.py       AA-996: Catalog → Countries / Destinations: CIS places (v_destination, v_destination_photo
                           when served) and countries from CIS ISO codes (catalog.v_country; places by
                           v_destination.country_iso2, tours by v_tour.country = v_country.name), joined to the Booking
                           state of bk-029 (country_profiles / destination_profiles: status active | inactive, reason,
                           area, internal notes; soft, audited, no row = active). A country outside booking.countries
                           is listed read-only (booking_reference false; PATCH 409; bk-031 added the AA tour countries
                           KG / PK / TW / UZ, so only a non-AA cross-border country is left there). Images = distinct CIS photo paths
                           of its places + its tour photos not tied to a place with a country (v_destination_photo,
                           v_tour_photo; fail soft); cover_photo_path = first photo of its best-covered place, else
                           tour; the detail adds photo_items (first 24, cover first). Paths only: the UI builds URLs
                           with cisPhotoUrl() (ui/booking/CisPhoto.tsx, neutral tile when missing or broken)
  app/catalog/destinations.py  reads of catalog.v_destination (fail closed on writes, soft on reads; re-exported by
                           app/catalog/__init__.py)
  app/catalog/trip_plans.py  catalog.v_tour LEFT JOIN booking.trip_plans (live), commercial upsert, items from v_tour_day;
                           list_orphaned (AA-986 L5: plans whose tour left v_tour, not paused; reason + attached);
                           AA-996: supplier on the commercial row (kept when left out), list sellable first, page rows
                           + destinations (v_tour_day places), first-photo cover fallback, from_price (LATERAL per row)
  app/catalog/trip_plan_prices.py  AA-996 (bk-032): the per-person price matrix of a plan; rows by hand or priced
                           from a net cost with app.pricing.service (quote-preview's rule / FX / rounding); PUT
                           replaces the set (audited), no overlaps per channel + currency; from price (B2C, 2 pax)
  app/catalog/liveness.py  AA-986 L5: served state of a catalog key (served | retired | paused | not_in_catalog) from
                           v_tour + v_tour_retired, feature detection of the CIS 274 views, strict / soft reads
  app/catalog/lifecycle.py AA-986 L5: bk_tour_lifecycle_pull (outbox → inbox → staff notifications) + PullSchedule
  app/b2b/served.py        AA-986 L5: 409 tour_not_served for a tp: line of a tour that is not served
  app/routers/pricing.py   POST /booking/pricing/quote-preview (pricing.read); GET/POST /booking/pricing/rules,
                           /booking/pricing/fx-rates and /booking/pricing/cancellation-policies (AA-974), POST
                           …/{id}/approve (reads pricing.read, writes pricing.write)
  app/pricing/service.py   rule + FX in force + engine, shared by quote-preview and POST …/versions/{n}/price
  app/routers/suppliers.py /booking/suppliers* (list, summary, detail, create, edit, deactivate/activate, contracts)
                           and /booking/contracts/{id} (edit a draft, publish, expire) — supply.read / supply.write
  app/suppliers/           supplier + contract service (bk-008): place check, maker-checker publish, audit rows
  app/routers/supply.py    /booking/hotels*, /booking/room-types/{id}, /booking/services*, /booking/contracts/{id}/rates,
                           /booking/rates/{id}, /booking/supply/options, /booking/supply/rate-in-force (bk-010, AA-823);
                           GET|POST /booking/{hotels|services}/{ref}/media, PATCH /booking/media/{id} (AA-835, bk-023)
  app/supply/              hotels + room types + services (records.py, one implementation for both kinds) and supplier
                           net rates + the default net of a version item (rates.py); AA-835 S4: supplier photos
                           (media.py: base64 upload, type and size read from the bytes, S3 put to SUPPLY_MEDIA_BUCKET,
                           presigned previews, usage_rights / removal) and the CIS content read-back (content.py)
  app/contracts/           byte copies of AA-Ecosys docs/contracts/*.json (SHA-256 pinned by a test)
  tests/unit/              unit tests + migration guard (tests/migration_guard.py)
  tests/integration/       real Postgres: applies migrations/, checks role privileges, /ready, booking.job vs template
  tests/job_template/      byte copies of AA-CIS-App shared/jobs/job_table_template.sql + job_table_shape.py (AA-751;
                           never edited here, drift-checked by CI "Vendored CIS copies")
  scripts/                 check_vendored_copies.py (that drift check, also for app/auth/identity_verifier.py; stdlib,
                           GitHub contents API); export_openapi.py (writes docs/api/openapi.json, AA-899)
  Dockerfile               python:3.12-slim, port 8000, non-root, stdlib healthcheck
migrations/NNN_*.sql       schema booking, key bk-NNN (byte-copied to AA-CIS-Infra)
docs/adr/                  repo ADRs (0001 implements root ADR 0004)
docs/ui-spec.md            staff admin UI spec (AA-683; routes, waves, API per route)
docs/api/                  openapi.json (generated API reference, AA-899; never edited by hand) + README (how the
                           breaking-change check works, the api-breaking escape hatch)
docs/implementation-notes/ per-issue decisions (AA-678.md, AA-679.md, AA-680.md, AA-681-slice2.md, AA-681-slice3.md, AA-681-w1.md,
                           AA-683.md, AA-751.md, AA-751-release.md, AA-751-metrics.md, AA-751-B3-B4.md, AA-810.md,
                           AA-811.md, AA-822.md, AA-823.md, AA-825.md, AA-871.md, AA-891.md, AA-972.md, AA-973.md,
                           AA-974.md, AA-835.md, AA-682.md, AA-993.md)
.github/workflows/         ci.yml (5 required checks + non-required Frontend Build and Vendored CIS copies), deploy-dev.yml,
                           post-deploy-verify.yml (AA-993: called by deploy-dev.yml, also dispatchable with expected_sha),
                           release-please.yml (AA-891: release PR + tag, App token of aa-ecosys-ci-bot),
                           docs-lint.yml (AA-888: front-matter, ADR index, single-line AWS CLI, offline links; weekly
                           freshness report), api-contract.yml (AA-899: oasdiff breaking-change check of
                           docs/api/openapi.json against the base branch)
.github/scripts/           booking-deploy-preflight.sh (the Deploy Dev job guard, AA-751 B4), openapi_breaking.sh (AA-899,
                           byte copy of the AA-CIS-App script), post_deploy_verify.sh (AA-993, same file as AA-Partner)
.github/verify/            booking.rows (AA-993: the HTTP rows of the post-deploy verify; a PR that adds an endpoint worth
                           guarding adds a row)
.github/oasdiff/           severity-levels.txt (AA-899: oasdiff checks raised to error)
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
`GIT_SHA` and `APP_VERSION` (both baked into the image; `APP_VERSION` = the release version from
`.release-please-manifest.json`, AA-891), `DB_SSL` (only `test`/`local` may set `disable`), `WORKER_ENABLED`
(optional; default on when `ENVIRONMENT=dev` and `DATABASE_URL` is set), `CIS_JWKS_URL` and
`STAFF_JWT_ISSUER` (optional; the CIS bridge, default the Dev CIS, `https://api-cis.lumiguides.it.com[/auth/.well-known/jwks.json]`).
Cognito (AA-680, all optional and **not set on Dev**, defaults are code constants in `app/auth/identity_config.py`):
`COGNITO_WORKFORCE_ISSUER`, `COGNITO_BOOKING_STAFF_CLIENT_ID`, `COGNITO_BREAKGLASS_CLIENT_ID` (same names as CIS).
E-mail (AA-811, all optional): `STAFF_NOTIFY_EMAIL` (staff inbox for the new inquiry e-mail; empty = no e-mail),
`SES_FROM_ADDRESS` (must equal the Infra `ses_from_address`, the only From the task role may use; empty = no e-mail),
`SES_CONFIGURATION_SET` (Infra `aa-email-<env>-transactional`), `SES_REGION` (default `us-west-1`), `STAFF_ADMIN_URL`
(links in staff e-mail; default the Dev Vercel alias `https://aa-booking-admin.vercel.app`).
B2B API (AA-972, both set by AA-CIS-Infra `booking.tf` from the M2M pool and the AA-Partner client; empty = every
`/booking/b2b/v1` route answers 503): `COGNITO_M2M_ISSUER`, `COGNITO_M2M_CRM_CLIENT_ID`.
Supplier photos (AA-835 S4, set by AA-CIS-Infra `booking_media.tf` / `booking.tf`): `SUPPLY_MEDIA_BUCKET` (the
`aa-booking-<env>-supply-media-<account>` bucket, checked against the bk-023 naming rule at start-up; empty = uploads
answer 503 `media_storage_unavailable`) and `SUPPLY_MEDIA_REGION` (default `us-west-1`).
Operations documents (AA-682 S1, set by AA-CIS-Infra `booking_ops_docs.tf` with the ops-docs bucket (SSE-S3; every
object verb denied to any principal but the Booking task role) and the task-role grant `s3:PutObject` /
`s3:GetObject` on `ops-docs/*`): `OPS_DOCS_BUCKET` (the
`aa-booking-<env>-ops-docs-<account>` bucket, checked against the bk-026 naming rule at start-up; empty = uploads and
downloads answer 503 `ops_storage_unavailable`, everything else works) and `OPS_DOCS_REGION` (default `us-west-1`).
Signed-in travellers (AA-908, optional, never set on Dev): `COGNITO_EXTERNAL_ISSUER`, the only issuer the inquiry intake
accepts for a v1.4 `customer.identity` (default the Dev EXTERNAL pool, `identity_config.DEV_EXTERNAL_ISSUER`; must
differ from the WORKFORCE and M2M issuers); since AA-682 S5 also the issuer of the customer routes, with the two
traveller clients `COGNITO_TRIPPLANNER_CLIENT_ID` / `COGNITO_BOOKING_CUSTOMER_CLIENT_ID` (defaults the Dev clients in
`identity_config`, never set on Dev; letters and digits, never a staff or M2M client).
Error tracking (AA-871, optional): `SENTRY_DSN` (Sentry off when empty; never logged) and `SENTRY_REQUIRED` (`true` =
start-up fails without a DSN, the missing-DSN guard; off until Infra wires the DSN secret).

## Releases (AA-891)

One version for the whole repo (API image and staff admin), kept by release-please in manifest mode
(`release-please-config.json`, `.release-please-manifest.json`; `CHANGELOG.md`; tags `vX.Y.Z`; pre-1.0 a `feat` or a
breaking change bumps the minor, a `fix` the patch). `.github/workflows/release-please.yml` keeps one release PR open on
`main`, opened with a token of the GitHub App `aa-ecosys-ci-bot` (environment `release`) so the required checks run on
it; merging it tags the release. The version also lives in `backend/pyproject.toml` (an `extra-files` entry, bumped by the
same PR). A release commit deploys nothing (Deploy Dev ignores those files, Vercel Deploy watches `frontend/**` only);
the next backend deploy, or a Deploy Dev dispatch on `main`, bakes the new version, which `GET /booking/health` returns
as `version` next to `git_sha`. Details: `docs/implementation-notes/AA-891.md`.

**API reference (AA-899).** `docs/api/openapi.json` is the OpenAPI document of the code, exported by
`backend/scripts/export_openapi.py` (no database, no secret; deterministic, no `servers`, `info.version` =
`unreleased`). The required Unit Tests check fails when it is stale; the "API contract" workflow diffs it against the
base branch with oasdiff and fails a PR that breaks a **public** path (`/booking/b2b/v1/*`, `/booking/health*`,
`/booking/ops/*`) unless the PR carries the label `api-breaking` or a `type!:` title. Staff routes (only this repo's
staff admin calls them) are reported as warnings. Each release gets the document as an asset
(`aa-booking-openapi-vX.Y.Z.json`, stamped with the version). How-to: [`docs/api/README.md`](docs/api/README.md).

## Logs, request ids and errors (AA-871)

- Every stdout line is **one JSON object** (structlog; the existing stdlib `logging` calls render the same way):
  `timestamp`, `level`, `logger`, `event`, `service`, `environment`, `git_sha`, and the bound context: `request_id` on
  every line written while serving a request, `job_id` + `job_kind` on every line written while a job runs. One
  `request_completed` line per request (method, path, route template, status, `duration_ms`; no query string) replaces
  uvicorn's access log. Values are redacted before rendering (`app/observability/scrub.py`: e-mails, `+` phone numbers,
  Bearer tokens, JWTs, key-like query values, Postgres `Key (email)=(…)` details, and whole values under keys such as
  `email`, `phone`, `name`, `token`, `authorization`). Log ids, never personal data (DM-6.4).
- **Slow statements** (AA-996): every pool connection carries a query logger; a statement at or over 1 s
  (`app.db.SLOW_QUERY_MS`, the pool `command_timeout` is 5 s) logs `slow_query duration_ms=… budget_ms=… catalog=…
  error=… sql=…` (compacted SQL text, never the arguments). A read cut by the timeout logs `error=TimeoutError`.
- **Request id:** a well-formed `X-Request-ID` (`[A-Za-z0-9._:-]{1,128}`) is kept, anything else is replaced by a new
  `uuid4().hex`; it is echoed as `X-Request-ID` on every response (the unhandled 500 too) and set as the Sentry tag
  `request_id`. API Gateway passes its `context.requestId` in that header once Infra maps it on the proxy integrations.
- **Sentry** (only with `SENTRY_DSN`): CIS AA-870 settings (`send_default_pii=False`, no bodies or cookies, header
  allowlist, 10 % traces / profiles) plus no frame locals, no `user`, and the scrubber over every text field;
  `release` = `GIT_SHA`. A job that ends `failed` is captured with tags `job_kind` / `job_id`.

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

- Every `/booking/*` route except `/booking/health*`, `/booking/ops/*` and `/booking/b2b/v1/*` (M2M, see "B2B API")
  needs `Authorization: Bearer <token>`,
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
  publish: not the draft's last writer, 403 `maker_checker`; hotels, room types, services, supplier rates and supplier
  photos (AA-835) use the same pair, and writing a rate makes you the contract's maker); catalog reads (`catalog.read`) = every role,
  content_creator included (without the price hint, channel and commercial notes, which need `pricing.read`); the
  trip plan commercial row (`trip_plan.write`) = admin, contracting; "Use for trip case" = `trip_case.write` +
  `version.write` (admin, sales); block / archive customers, staff admin, audit log read, retention runs and status
  (`retention.admin`, AA-825), B2B partners (`b2b.admin`, AA-972) = admin; a trip plan's bookable components (AA-972)
  are read with `supply.read` and replaced with `trip_plan.write`; its fixed departures (AA-973) the same pair; its
  price matrix and supplier (AA-996, bk-032) are read with `pricing.read` and written with `trip_plan.write`; B2B
  cancellation-fee policies (AA-974) are pricing data: `pricing.read` / `pricing.write`, maker-checker like rules.
  Operations (AA-682 S1, bk-026): supplier bookings and their documents `ops.read` / `ops.write` = admin, sales,
  contracting (`ops.read` is in the preview cap); confirming or declining a booking of an on-request B2B line also
  needs `b2b_desk.write`; net amounts in the responses need `pricing.read`. Trip checks and the lifecycle runs (AA-682
  S2, bk-027) use the same `ops.read` / `ops.write`; the customer name in the check list also needs `customer.read`.
  Billing (AA-682 S3, bk-028): invoices and payments `billing.read` / `billing.write` = admin, sales (contracting sees
  no customer money; the case detail hides its `money` badge without `billing.read`); verifying a payment
  `payment.verify` = admin and never the person who recorded it (403 `maker_checker`); voiding a verified payment needs
  `payment.verify` too. None of the three is in the preview cap (critic note 8: bill-to snapshots, payment refs).
  Supplier bills (AA-682 S4, bk-030): `bill.read` = admin, sales, contracting; `bill.write` / `bill.approve` = admin,
  contracting (never approving a bill they entered or whose amounts / lines they last changed, `facts_edited_by`: 403
  `maker_checker`); `payout.write` = admin (never on a bill they entered or last changed). The case cost vs billed panel also needs `pricing.read`. None of them is in the preview cap.
  Catalog geo state (AA-996, bk-029): the Booking status, reason, area and notes of a CIS country or place
  `catalog.admin` = admin only (UI spec §3 "A edit status"); reading Countries / Destinations is `catalog.read`.
  Notifications,
  settings, countries, the catalog place picker (`/booking/catalog/destinations`), own preferences and own staff
  detail = any active staff.
- Every staff write adds one `booking.audit_logs` row (`actor_ref` = the staff key: CIS user id or `cog:<sub>`).
- **API docs are gated (AA-876, CIS AA-577 pattern).** FastAPI's own docs routes are off; `app/routers/docs.py`
  serves `GET /booking/openapi.json` and `GET /booking/docs` (not in the schema). With `ENVIRONMENT` `test` / `local`
  they are open; anywhere else (Dev included) they need a staff Bearer with `staff.admin`, and every refusal (no token,
  bad token, auth unavailable, no role, inactive, not admin) is the same **403** `docs_forbidden`. The check goes
  through `current_staff`, so it may provision or sync the caller's `staff_users` row. The Swagger page fetches the
  schema without a Bearer, so on Dev the docs are for `curl` with a token. Local runs: `ENVIRONMENT=local`.
- The staff login BFF (`frontend/app/api/auth/login/route.ts`, CIS bridge) passes a CIS 429 (CIS login throttle or
  the API Gateway login throttle, AA-876) on as a 429 "Too many sign-in attempts…" with `Retry-After`, not a 502.
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

## B2B API (AA-972; root ADR 0005a, ADR 0006b decision 7; contract `booking.b2b.v1`)

- **Caller**: AA-Partner's API server, client credentials of the Cognito M2M pool `aa-m2m-<env>` (resource server
  `booking.b2b`, scopes `read`, `quote`, `hold`, `book`). `app/auth/m2m.py` builds a second verifier from the same
  vendored module (the `scope` claim read as groups). No staff token is accepted on `/booking/b2b/v1`, no M2M token
  on staff routes. Refusals use the contract's Problem body `{error, message}`: bad / missing token 401
  `unauthorized`; a validly signed token of another client, a missing scope or a client that is not the partner's
  `m2m_client_id` 403 `forbidden`; no active `b2b_partners` row for `X-AA-Tenant-Id` 403 `unknown_partner`; not
  configured, JWKS never loaded or pricing data missing 503 `unavailable`; bad input 422 `invalid`.
- **Routes (AA-972)**: `GET /products` (sellable trip plans still in `catalog.v_tour`, with their bookable components
  `rt:<room type>`, `sv:<service>`, and `tp:<plan>` for the whole plan), `POST /quotes`, `GET /fx-rates` (approved
  rates in force on a date, plain), `GET /price-parity` (the public "from" price per person from the bk-009 price hint,
  warn-only; `null` without a per-person hint or rate). **AA-973**: `GET /departures`, `POST /holds`,
  `GET` / `DELETE /holds/{id}`, `POST /holds/{id}/confirm` (below). **AA-974**: VND quotes, `POST /holds/{id}/extend`,
  the re-hold limit on `POST /holds`, `POST /bookings/{id}/cancel` (+ `?dry_run=true`) (below). **AA-975**:
  `GET /events` (below).
- **Departures and seats (AA-973)**: a departure is a start date of a trip plan with a seat `capacity`, a status
  (`scheduled` ↔ `guaranteed`, `cancelled` final) and `auto_confirm`; staff keep them under
  `/booking/catalog/trip-plans/{source_id}/departures` (capacity never below held + sold, audited, edits touch the
  plan's `updated_at`). **Held and sold are counted, never stored**: held = seats of `active` holds not past
  `expires_at`, sold = seats of `confirmed` holds whose line is not declined / cancelled; seats = adults + children.
  Holds lock the departure rows (`FOR UPDATE`, id order) before counting, so two holds on the last seat run one after
  the other and exactly one wins. Line availability (quotes and holds): no departure = `on_request`; a departure with
  room that auto-confirms = `available`, that does not = `on_request`; full, cancelled or started = `sold_out`. A
  quote leaves out the seats of the live `active` hold of its own `(partner, proposal_ref, proposal_version)` (AA-950:
  the set a new quote of that version replaces, released by the caller before `POST /holds`; the hold still counts
  every hold under the lock), so a client is never told the seats they hold are sold out. A
  `departure_id` only belongs to the `tp:` line of its own plan on its `start_date` (else 422 `invalid`). Products
  report `has_fixed_departures`.
- **Holds (AA-973)**: `POST /holds` (scope `hold`, `Idempotency-Key` required) holds the lines of one quote for one
  proposal version: one `active` or `confirmed` set per `(partner, proposal_ref, proposal_version)` (the same quote
  again returns that hold; another quote is 422 `invalid` until it is released); the same key for another request is
  422; a hash that is not the quote's, or a quote past `valid_until`, is 409 `price_changed` (no re-pricing within
  validity); a line that does not fit is 422 `sold_out` and nothing is held. Length = setting `b2b_hold_hours` (48),
  capped by the earliest departure start. An `active` hold past `expires_at` reads `expired` at once; the job
  `bk_b2b_hold_expire` writes the status. `DELETE` releases (released / expired: no-op; confirmed: 422). Error codes
  follow the contract: `sold_out` 422, `hold_expired` 410.
- **Confirm (AA-973, memo seam 1)**: `POST /holds/{id}/confirm` (scope `book`) is the only B2B path that creates a
  commercial record: one transaction opens a **B2B trip case** (`channel = 'b2b'`, `customer_id` NULL,
  `party_tenant_id` = the agency, `source_system = 'crm'`, `source_id` = the proposal ref, status `confirmed` inserted
  directly, `idempotency_key = b2b_hold:<hold id>`), **one approved version** (V1, channel `b2b`, the quote's total,
  currency and FX lock) with one item and one cost row per line (net from the quote), sets the hold `confirmed`, then
  the hold lines `available` → `confirmed` (so their `line.confirmed` events name the booking); `on_request` lines go
  to the B2B desk with their answer deadline (AA-975, below). Expired / released → 410
  `hold_expired`; wrong hash → 409; a departure cancelled since the hold → 422 `sold_out`; a replay returns the same
  booking. `booking_id` = the trip case id. Staff trip-case reads LEFT JOIN customers, so B2B cases show with no
  customer (`channel`, `party_tenant_id` in the case body; list filter `source=crm`). A B2B case is owned by the
  partner API: staff status moves, send, approve and edits of pax / dates / currency are 409
  `b2b_case_managed_by_partner_api` (notes, trip style, countries stay editable; the agency cancels through `POST
  /bookings/{id}/cancel`, AA-974). Confirm locks the departures and judges expiry on the clock read after the locks,
  so a confirm that waited past `expires_at` is 410 and never sells seats a newer hold took.
- **Extend once (AA-974, Fix A8)**: `POST /holds/{id}/extend` (scope `hold`, `Idempotency-Key`, reason
  `payment_claimed | client_recheck`) moves an active hold's expiry by setting `b2b_hold_extend_hours` (72), capped by
  the earliest departure start, at most once (`extended_count` 1, `extended_at`, `extend_reason`). A retry with the
  same key returns the hold; any other extend is 409 `extend_limit`, as is one that cannot move the expiry (the hold
  already runs to the departure start). Expired / released → 410 `hold_expired`; confirmed → 422. It locks the hold,
  then its departures, and judges expiry on the clock read after the locks (same race rule as confirm).
- **Re-hold limit (AA-974, Fix A3)**: at most setting `b2b_rehold_limit_per_day` (3) re-holds per UTC day, the next
  `POST /holds` is 429 `rehold_limit` with `retry_after_s` to 00:00 UTC, counted two ways. **Per proposal ref**
  (design §3.4): every hold after the ref's first one is a re-hold, any version; serialised per (partner, ref) by a
  transaction advisory lock. **Per departure** (lane brief; review round): a hold of a departure after a hold of it the
  partner placed the same day and let go (released / expired) is a re-hold, whatever the ref, so a fresh proposal ref
  per hold does not get around the limit; counted under the departure locks. Holds still active or confirmed do not
  count. Idempotent answers (same key, same version) never count. Per-departure counting awaits Nghiệp's confirmation
  (implementation notes AA-974).
- **Cancel with fee (AA-974, ADR 0005a d4, C12)**: `POST /bookings/{booking_id}/cancel` (scope `book`,
  `Idempotency-Key`, `reason_code`; `booking_id` = the trip case id) charges the fee of the booking's
  **cancellation policy**: a schedule of tiers `{min_days, fee_pct}` per trip plan or the B2B default
  (`booking.cancellation_policies`, append-only, in force once a second person approved it, staff
  `/booking/pricing/cancellation-policies`). The policy in force on the confirm day is locked on the hold
  (`b2b_holds.cancellation_policy_id`; bk-021 backfills the seeded default on holds confirmed before it; a hold
  without one resolves it among the rows approved by its `confirmed_at`, so a later or backdated row never re-prices
  it). Days before departure = trip case start − UTC day of the cancel; the tier
  with the largest `min_days` ≤ max(days, 0); fee = quoted total × % half-up to the currency unit; refundable = total
  − fee; a booking on a departure AA cancelled (staff status `cancelled`) costs no fee. `?dry_run=true` returns the
  same numbers and writes nothing. The cancel writes one `b2b_cancellations` row, sets the hold lines `cancelled` (the
  seats go back on sale at once), moves the B2B trip case `confirmed → cancelled` (status note names the client,
  reason and fee) and audits; a repeat returns the stored result. The hold
  itself stays `confirmed` (the contract's Hold status has no `cancelled`). Seeded default (proposed for Ms. Thư):
  60+ days 0 %, 30–59 25 %, 15–29 50 %, 7–14 75 %, 0–6 100 %.
- **Event feed (AA-975, bk-022, contract minor 1.1)**: `GET /events?after=<cursor>&limit=` (scope `read`; default
  200, max 500) returns the calling agency's events oldest first: `hold.expired`, `hold.extended`, `line.confirmed`,
  `line.declined` (+ `decline_code`), `booking.cancelled`, `departure.guaranteed`, `departure.cancelled`,
  `departure.incident` / `departure.incident_cleared` (+ `incident_code`). Ids and codes only (no payload, no free
  text: an event can never carry traveller data). Every event is a row of the **shared outbox** `booking.outbox`
  written by a trigger in the transaction of the change (holds, hold lines, cancellations, departures, incidents), so
  every path emits (requests, the expiry job, lazy expiry, the desk, AA-682 later; memo seam 8). Departure events fan
  out: one row per agency with a live hold or a confirmed, not declined / cancelled line on that departure. **Order**:
  readers page on `feed_seq`, which `app/outbox.py` stamps on **committed** rows in id order under one advisory lock
  (the feed stamps before it reads; the worker hook `OutboxSequencer` stamps on every maintenance pass), so a row that
  commits late is never skipped. Cursor `bk1.<feed_seq>` (opaque); `next_cursor` = the last item's, or `after` when
  nothing is new; a malformed cursor or one ahead of the feed is 422 `invalid`. At least once: a replay from an older
  cursor returns the same events (same `event_id`, order) again, each once; consumers dedupe on `event_id`.
  `departure.time_change` is in the contract but has no producer (departures carry no start time yet). No outbox
  retention window yet (no PII; a DM-7.5 decision when volume needs one).
- **B2B desk (AA-975, staff `/booking/b2b-desk`, permissions `b2b_desk.read` / `b2b_desk.write`: admin, sales,
  contracting)**: **on request** = lines of confirmed B2B bookings held `on_request` (no departure, or a departure
  without `auto_confirm`). At confirm each gets `answer_due_at` = `booking.b2b_answer_due(now(), n, tz)` with settings
  `b2b_on_request_answer_business_days` (1) and `b2b_ops_timezone` (Asia/Ho_Chi_Minh): Mon–Fri, a weekend start
  counts from Monday 00:00, no public holidays; one staff notification `b2b.on_request` per booking. Staff accept
  (`confirmed`) or decline with a code (`sold_out | supplier_unavailable | minimum_not_met | other`; the seats go back
  on sale); SLA badge `overdue` / `due_soon` (≤ 4 h) / `on_time`; the same answer again is a no-op, another answer 409
  `line_answered`; audited. Pre-confirm holds are not on the desk (no deposit yet), so the hold length stays
  `b2b_hold_hours`. **Incidents** (design Fix B3): "Flag incident on departure" with a contract code (`weather | road |
  health | political | supplier | other`, no free text), at most one open per departure (409 `incident_open`), a
  cancelled departure takes none (409), clear ends it; each emits `departure.incident(_cleared)` to the agencies on
  it; audited. `GET /summary` (open, overdue, due soon, open incidents), `GET /departures` (from 14 days ago to 90
  ahead: seats, agencies, open incident, CIS title). Staff page `/b2b-desk` (Operations → B2B desk).
  AA-682 (design D13, one action both ways): a line that a live supplier booking covers is answered by that booking
  (its confirm accepts the line, its decline declines it, through `desk.answer_line` in the same transaction); the desk
  answers such a line 409 `answer_from_suppliers_tab` and lists the booking code (`supplier_booking`).

## Trip checks and the case lifecycle (AA-682 S2, bk-027; S252 design §3.2, §4, §9)

- A **trip check** (`booking.trip_case_checks`) is one thing to do before departure. Entering `confirmed` (the B2C
  version approval, or the B2B confirm that inserts the case at `confirmed`) seeds three **standard** checks through
  the DB trigger `trip_cases_seed_checks`: `pre_trip_recheck`, `emergency_contact`, `insurance`, due `start_date -
  pre_departure_days` (their due date follows the start date while open); staff add **custom** checks (title). Life
  `open → done | not_applicable → open` (done ⇔ `done_at`; done ↔ not applicable goes through open in one call).
  **Derived** checks (`v_trip_check_board`, never stored): `suppliers_confirmed` (supply `booked` / `none`) and
  `payment_verified` (B2C: the invoice is paid by verified payments, bk-028; NULL on B2B).
- **Emergency contact = B2C only** (DM-6.8): on a B2B case the row stays `not_applicable` without contact fields (DB
  guard; the API answers 422 `contact_not_allowed_b2b`). A B2C contact check is done only with name + phone. Contact
  fields and notes are cleared at trip end + `emergency_contact_keep_days` (90) by `bk_case_lifecycle` (`cleared_at`,
  final). Audit rows of a check name the changed fields, never the values.
- **Case lifecycle after the sale** (job `bk_case_lifecycle`, daily): `→ pre_departure` at `start_date -
  pre_departure_days` when supply is `booked` / `none` and (B2C) the case is `paid` (its invoice paid by verified
  payments, bk-028; otherwise reported blocked `not_paid`), `→ on_trip` on the start date, `→ completed` the day after the end date;
  moves chain in one run, each a system UPDATE re-checked by the bk-026 guard. A due but blocked gate raises one staff
  notification `trip_case.gate_blocked` per case, target and reasons. Open checks never block a move. "Today" is the
  date in the ops time zone (`b2b_ops_timezone`, Asia/Ho_Chi_Minh) and the job's transactions run in that zone, so
  the guard's `current_date` agrees. First runs are a DRY RUN (see Jobs).
- Writes on a cancelled / completed case are 409 `case_closed`; an inactive assignee is 422 `staff_not_assignable`.
- `GET /booking/trip-checks` also returns `counts` (open / overdue / done / not applicable / all under the same owner,
  check, case and search filters; overdue = open and due before today, as the board) and, per row, the trip's
  `checks` (total / done / not applicable) and `sales_name` (AA-996: the page's status tabs, progress and drawer
  context).

## Supplier bookings and the supply track (AA-682 S1, bk-026; S252 design §3.1, §4, §5)

- A **supplier booking** (`booking.supplier_bookings`, code `SB-000123`) books one supplier for one room type /
  service item of the case's **approved version**, or an extra item. `POST …/supplier-bookings/generate` makes one
  `pending` booking per supply item that has no live booking (supplier = the hotel's / service's; date = case start +
  item day − 1; room nights on the convention the item was sold on: B2C = the pricing rule
  `supply_rates.quantity('room_night')` = max(day_to − day_from, 1) (check-out day = `day_to`), B2B = day_to −
  day_from + 1 (`holds._open_case` writes day_to = day_from + nights − 1); pax = case pax; net = the sum of the item's cost rows, 409
  `mixed_cost_currencies` when their currencies differ); idempotent, and concurrent generates are serialised by the case
  row lock and the unique index. Life: `pending → requested → confirmed | declined`, `→ cancelled` (reason), `confirmed
  → requested` (a change, reason; the old ref stays in the audit row). Edges in `booking.ops_transitions`, facts in
  CHECKs and `supplier_bookings_guard` (confirm needs the supplier's ref; decline a desk code; facts fixed once confirmed;
  notes always editable). **Live = pending / requested / confirmed**: one live booking per version item; a `declined`
  one is replaced with another supplier (B2C).
- **Supply state** (`booking.v_trip_case_supply_state`, in the case detail as `supply`): `none` (nothing to book),
  `confirming`, `booked` = the **"Confirmed & Booked"** badge (design D1; never a status).
- **Case statuses after the sale are system-managed** (critic B1): `change_status` refuses every post-sale target but
  `cancelled` (409 `status_system_managed`); `advance_case` moves `confirmed → supplier_confirming` on the first request
  and back when no live booking is left (empty actor, an "Auto: …" status note, a system audit row); the bk-026 guard
  `trip_cases_post_sale_guard` checks the fact behind each post-sale status whoever writes it (money statuses wait for
  bk-028; a B2C case reaches `pre_departure` only once paid, bk-028). A staff cancel cancels the pending bookings and
  sends one notification `supplier_booking.cancel_with_supplier` for requested / confirmed ones; reopening is 409
  `live_supplier_bookings` while one is live (also refused by the guard).
- **B2B cases** (memo seams 1, 4, 10): generate links each item held `on_request` to its hold line; confirm / decline of
  such a booking answers the line (events `line.confirmed` / `line.declined`, `booking.b2b.v1` unchanged); a line is
  answered once, so a linked booking whose line is confirmed is never cancelled or re-requested by staff (409
  `b2b_line_answered`; only the agency's cancel moves it) and a declined line is final (no replacement). The agency's
  `POST /bookings/{id}/cancel` also cancels `supplier_confirming` / `pre_departure` cases (critic B2) with the same
  cascade. No voucher or document on a B2B case (DM-6.8; 422 `document_not_allowed_b2b`, refused by bk-026 too).
- **Documents** (`booking.ops_files`, design D10): `POST /booking/ops-files` (base64 JSON, PDF / JPEG / PNG / WebP read
  from the bytes, at most 3 MB through the staff BFF; DB cap 10 MB), stored in the private ops-docs bucket (SSE-S3)
  under `ops-docs/<kind>/<id>.<ext>`; `GET …/{id}/download` = a 300 s presigned GET forcing an attachment, audited
  `ops_file.downloaded`; `POST …/{id}/remove` (reason, final; a removed voucher leaves its booking). Kinds of bk-028 /
  bk-029 (payment evidence, bills, payouts) answer 422 `kind_not_available` until those slices.
- Lock order hold → case → booking on every path (the B2B cancel locks the hold, then the case), so a staff confirm and
  an agency cancel of the same booking never deadlock (integration test).
- **Products sync**: `updated_since` filters on the latest of the plan row (touched by every component replace, also
  when the set is emptied), its component rows, the hotels / room types / services behind them and (AA-986 L5) the
  tour's `catalog.v_tour.updated_at` (follows the Master row, CIS 273, so a restored tour is listed again), and then
  also lists plans that stopped being sellable (`sellable = false`, no components). **Withdrawn** (AA-986 L5, root
  ADR 0015 decision 5): with `updated_since` and CIS 274, a plan that was offered to agencies (sellable, or with
  components) whose tour is `retired` in `catalog.v_tour_retired` is listed with `withdrawn: true`, `withdrawn_reason`, no components and `updated_at` = the retirement time; a
  `paused` tour is a temporary absence and is not listed (it is only missing). Without CIS 274 a removal shows only in
  a periodic full resync (no `updated_since`). A `product.withdrawn` event is not in the feed (the contract enum has
  none; the products delta carries it). The CRM should overlap its
  `updated_since` window by a few minutes (`updated_at` is the writing transaction's start time).
- **Tours that are not served (AA-986 L5, root ADR 0015 decision 5, Q4)**: `POST /quotes`, `POST /holds` and `POST
  /holds/{id}/confirm` refuse a `tp:` line whose plan's tour is not in `catalog.v_tour`, or is `retired` / `paused` in
  `catalog.v_tour_retired` (fail closed when the two disagree), with 409 `tour_not_served` + `tour`, `tour_state`
  (`retired` | `paused` | `not_in_catalog`) and `reason`; a catalog read error is 503 `unavailable`. `rt:` / `sv:`
  lines carry no tour and are not checked. Replays (same hold key, a confirmed hold's booking) still answer; confirmed
  bookings, holds and trip cases are **kept** (never cancelled automatically) with a staff flag and notification
  (below). Extending an active hold is not refused (its confirm is).
- **Quote**: net cost per line from the supplier rate in force on the service day (per night for rooms, by unit for
  services; infants not counted). Rooms are priced per entry of the line's room mix, the rate's pax band matched
  against that room's occupancy (single 1, double / twin 2, triple 3, family = the travellers left over shared by the
  family rooms, capped by `max_occupancy`); no `rooms` = doubles plus one single for an odd party; the rooms must hold
  the adults; a `rooms[].room_type_id` must name the line's room type (`rt:`) or the plan's only room type (`tp:`).
  Bounded work: a fixed number of queries per quote (items, plans, components, then one batched rate query,
  `app.supply.rates.rates_in_force_many`) and at most 1,000 rate lookups per quote (more = 422 `invalid` "quote too
  large", refused before any rate is read). Then the approved `b2b` rule in force today (seed 15 % / 10 % floor / 3 % card fee,
  policy data) and the approved FX rows in force today, through the AA-810 engine's functions (no second engine,
  `peak = false`). Each line's `b2b_price` is rounded up to the cent, the total is their exact sum. FX lock = the
  selling rate applied (rate x (1 + buffer)); `valid_until` = now + setting `b2b_quote_valid_hours` (48).
  `price_hash` = SHA-256 of a canonical JSON of currency, FX lock, rule, rounding, lines and total (not the id, time or
  proposal ref): same request on the same data → same hash. Quote currencies = the engine's five + **VND** (AA-974, ADR
  0005a d3; B2B only, the engine's B2C `SELLING_CURRENCIES` is unchanged): the approved VND row in force on the UTC
  quote day, each VND line rounded **up to 1,000 VND** (proposed default, to confirm with Ms. Thư), amounts without
  decimals; the rounding rule (`line_ceil_1000` / `line_ceil_0.01`) is stored and hashed.
  Stored append-only in `b2b_quotes` / `b2b_quote_lines` with the net (`net_cost_usd`, `conf:commercial`) that never
  leaves Booking. No column, field or key is named `supplier_net` (unit test over the B2B OpenAPI, integration test
  over the schema).

## Invoices and manual payments (AA-682 S3, bk-028; S252 design §3.3, §4, §5, critic B4–B6, B9)

- An **invoice** (`booking.invoices`, `INV-000123`) is a **proforma payment request** (design D7: prices tax-inclusive,
  no tax line; the legal VAT e-invoice stays outside Booking) for the customer of a **B2C** case, in the currency of
  the approved version, **one live per case**. Created from the case's approved, priced version (409
  `no_approved_version` / `version_not_priced` / `b2b_no_invoice` / `invoice_exists` / `case_not_invoiceable`, the
  last when the case is not `confirmed` / `supplier_confirming`). Lines: the version total (locked) + adjustments with
  a reason. Schedule: given rows that add up to the total (422 `schedule_mismatch`), or the default from settings:
  `deposit_pct` due `deposit_due_days` after the issue day + the rest `balance_due_days_before_start` before the trip,
  one payment when the trip is too close; amounts in the currency's minor units, the remainder on the last row.
- Life: `draft` (edit, soft-delete) → `sent` (bill-to snapshot, one customer notification row, case → `invoice_sent`)
  → `partially_paid` ⇄ `paid`; `sent` → `cancelled` (reason; only with net verified paid 0 and nothing waiting: 409
  `invoice_has_payments`; a partially paid invoice is refunded back to `sent` first). **The money states are derived by the database** from verified payments (`invoice_settle`,
  guarded by `invoices_guard`): no route, script or app bug can mark an invoice paid without verified money.
  **Amendment** (`POST …/amend`, critic B5): a sent invoice takes appended adjustment lines; rows the verified payments
  already cover stay, the rest is re-planned (or the difference goes on the last unpaid row); a paid invoice may drop
  to partially paid; the customer feed says "updated". It runs in the **amendment window**: one transaction sets
  `invoices.amending = true`, writes lines and rows, then sets it back to false, which is where the database checks
  that the schedule adds up to the new total (409 `invalid_amendment`). Outside the window the database refuses any
  change to a sent invoice's lines or schedule, and while it is open no payment is verified or voided. `POST …/remind` writes one customer row a day.
- A **payment** (`booking.payments`, `PMT-000123`) is recorded by hand (no provider, D6): receipt or refund, amount,
  method, `received_on`, bank / receipt reference (a card-like number is 422 `card_number_not_allowed`), note,
  optional amount that arrived in another currency (informational). **Maker-checker**: an admin who did not record it
  verifies it (403 `maker_checker`; CHECK in the DB), with the evidence file unless cash (422 `evidence_required`;
  `POST /booking/ops-files` kind `payment_evidence`, entity the payment, which becomes its `evidence_file_id`; bk-028
  re-checks at verify that it is still an active file, and removing the attached evidence of a recorded payment
  detaches it first). Only verified, non-voided payments count. Void = reason, final; a verified one needs `payment.verify`. A refund never
  takes the net paid below 0 (409 `refund_exceeds_paid`). Same method + reference again = 409 `duplicate_reference`
  unless `allow_duplicate_reference`.
- **Idempotency-Key** (8–200 chars) on `POST …/invoices` (unique per case) and `POST …/payments` (unique per invoice):
  same key + same body → the stored row (200, `replayed: true`); other body → 409 `idempotency_conflict`; missing →
  422 `idempotency_key_required`. The staff BFF forwards the header; the UI makes one key per opening of a form.
- **Case money track** (`app.ops.track.advance_case`, one function for both tracks): a B2C case follows its sent
  invoice (`sent` → `invoice_sent`, `partially_paid` → `payment_pending`, `paid` → `paid`), forward on a verify, back
  on a void / amendment, back to `supplier_confirming` / `confirmed` when the invoice is cancelled; the bk-028 guard
  refuses any other money status. B2C reaches `pre_departure` (lifecycle job) only once paid. Travel statuses never
  step back: a void after departure raises `invoice.balance_reopened` for staff instead (critic note 12). Lock order
  everywhere: case → invoice → payment (the settle trigger locks the invoice first, critic B4).
- **Case cancel / reopen** (critic B9): a staff cancel deletes a draft invoice and, for a sent one, raises
  `invoice.case_cancelled` (refund, then cancel it); reopening is 409 `live_invoice` while an invoice is not cancelled
  or a payment waits (the guard refuses it too).
- Settings (policy data; Ms. Thư's answers change values by migration or run-script): `deposit_pct` (bk-005),
  `deposit_due_days`, `balance_due_days_before_start`, `payment_due_soon_days`, `payment_overdue_reminder_days`;
  `invoice_issuer` and `payment_instructions` are **placeholders** in the public migration, set for real by a DB
  Migrate run-script (never in Git).
- KPIs (`GET /booking/billing/summary`): left to pay, overdue, received this month (verified, net of refunds),
  waiting for verification, per currency plus a USD total at each version's locked FX (`fx_rate_locked`, critic note
  5: the rate the quote was sold at, ADR 0005 d5).

## Supplier bills and payouts (AA-682 S4, bk-030; S252 design §3.4, §4, §5, critic B4, B6)

- A **supplier bill** (`booking.supplier_bills`, `BIL-000123`) is what a supplier asks AA to pay: its own bill number
  (unique per supplier among bills not cancelled), currency, amount, issue / due date, optionally one trip case (a
  statement bill spans several), the supplier's document (`POST /booking/ops-files` kind `supplier_bill`, entity the
  bill, while received; never on a bill that touches a B2B case, DM-6.8), and **lines**
  (`supplier_bill_items`, one per CONFIRMED supplier booking of that supplier, and of the bill's case when it has one).
- Life: `received` (working draft: facts, lines and document change freely) → `approved` by **another person** than
  the creator and than `facts_edited_by` (the last person who changed the money facts or the lines; set by the guard,
  checked on every line insert, CHECK + guard at approval) (≥ 1 line, lines = amount; checked at once by `supplier_bills_guard`, no deferred trigger) → `paid`, **derived by the
  database** from the live payouts (`supplier_bill_settle`, locks the bill first); `received` / `approved` →
  `cancelled` (reason, final; an approved bill only with no live payout: 409 `bill_has_payouts`). No case status moves.
- A **payout** (`booking.supplier_payouts`, `SPO-000123`) records money AA sent against an approved bill: amount in the
  bill currency, never above the balance (409 `payout_exceeds_balance`), method, `paid_on`, bank reference (no card
  numbers), note, bank slip (`payout_evidence`, replaced but never removed while the payout is live). It counts at once
  (no verify step); the recorder is never the bill's creator nor its `facts_edited_by`; a mistake is voided (reason, final) and the bill steps
  back to `approved`. `Idempotency-Key` on `POST /booking/supplier-bills` (unique) and `…/payouts` (unique per bill).
- **Cost vs billed** (`GET /booking/trip-cases/{ref}/supplier-costs`): per supplier booking the expected net next to
  the bill lines (bills not cancelled) and the variance once the booking is billed (no FX conversion; another currency
  is flagged; per currency the sum of the line variances, so unbilled nets stay out of it). The panel links to the
  bills of the case (`/billing?tab=supplier-bills&case=TC-…`) and enters a bill for it (`bill.write`).
- Notifications `supplier_bill.due` / `supplier_bill.approved` open the bill drawer
  (`/billing?tab=supplier-bills&bill=BIL-…`).
- `bk_ops_reminders` writes `supplier_bill.due` (to approve / to pay, due within `supplier_bill_due_soon_days`, then
  once more when overdue) to the shared staff queue. Staff UI: `/billing?tab=supplier-bills`, the supplier Bills tab,
  the bill drawer and payout modal, the case Suppliers tab's cost vs billed panel.

## My Payments: customer routes (AA-682 S5; S252 design §5, §7, §8, critic note 10; contract `booking.customer.v1`)

- **Route class**: `/booking/customer/v1/*` accepts only a Cognito EXTERNAL access token of the `tripplanner` or
  `booking-customer` client (`app.auth.customer`, its own verifier over the vendored module, keys prefetched in the
  lifespan like the staff and M2M ones). Anything else (no token, a staff WORKFORCE or bridge token, an M2M token, an ID
  token, the `cis-portal` client, a client-credentials shape) is 401 `sign_in_required`; keys never loaded is 503
  `auth_unavailable`. A traveller token never opens a staff route (unknown issuer for the staff verifier). The caller
  today is the TripPlanner BFF (`/api/me/payments`), which forwards the traveller's token; no gateway change (the REST
  API proxies `/booking/{proxy+}` with no auth, the app authenticates).
- **Scope** (`app.ops.my_payments`, all in SQL): `(iss, sub)` → `customer_identities` → a live customer; that
  customer's B2C cases (`trip_cases.customer_id`, so a merge that re-points the case carries its invoices); invoices
  not deleted and not `draft`. Everything else is 404 `invoice_not_found` (another customer's number, a draft, a
  malformed number); an unlinked sign-in (a guest lead is never linked) gets an empty list.
- **Fields** (allow-list = the router's response models, `extra="forbid"`): number, status, proforma, currency,
  total, paid (verified receipts - refunds), balance, next due, overdue amount, issue time; the trip's code, dates and
  TripPlanner trip id (`source_id` of a `tripplanner` case); schedule rows with the customer chip state
  (`scheduled`, `due_soon`, `paid`, `overdue`, `cancelled`); **verified, non-voided** payments (ref, kind, amount,
  currency, method, received date); `payment_instructions` only while money is owed (`sent`, `partially_paid`). Never
  a staff member, evidence, bank reference, note, reason, bill-to snapshot, internal id, net cost or supplier field
  (unit + integration tests walk every key; the root contract checker enforces the same policy on the contract and
  its samples). Read only, `Cache-Control: no-store` on every answer (401/404/503 included), nothing about the
  identity logged.

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
- **Deploy gate (AA-751 B3/B4; meaning changed by AA-993 G5, design D1.4).** `GET /booking/ops/deploy-gate`
  returns `{active, by_status, by_kind, safe_to_deploy, exempt_kinds, blocking_kinds, blocking_running, blocking_due,
  blocked_by}`.
  `safe_to_deploy` is false only while a row of a kind in `DEPLOY_BLOCKING_KINDS` (`app/jobs/kinds.py`: the retention
  kinds, which keep no checkpoint and are failed instead of released on shutdown) is **running**, or **queued and due
  within 15 min** (`blocking_due`, `DEPLOY_BLOCKING_HORIZON_SECONDS`: the preflight runs before the image build, and the
  old task would claim it before its SIGTERM). Queued / running rows
  of every other kind no longer block (the worker releases a running job on shutdown, the new task re-claims it); they
  stay in `active` / `by_status` / `by_kind` as information, kinds in `DEPLOY_GATE_EXEMPT` (`bk_inquiry_pull`, the
  always-queued pull; `bk_noop_sleep`, test-only) not counted there. Booking does not read CIS rerun waves. The
  `preflight` job of `deploy-dev.yml` (logic in
  `.github/scripts/booking-deploy-preflight.sh`, tested with stub `aws`/`curl` by
  `backend/tests/unit/test_deploy_preflight_script.py`) skips the gate only when the service has no task running,
  pending or desired; a task that is starting (none running, one pending or desired) blocks; otherwise it blocks the
  deploy unless `safe_to_deploy` (the error names `blocked_by`). Every block can be lifted by a `workflow_dispatch` `override_running_jobs` reason;
  an unreachable gate blocks too. Pushes that only touch `backend/tests/**`
  do not deploy.

| Kind | What |
|---|---|
| `bk_inquiry_pull` | Recurring singleton (every ~60 s): reads `tripplanner.v_inquiry_outbox`, anti-joins `booking.inbox_events`, inserts new rows `ON CONFLICT DO NOTHING`, queues one process job each. `booking.inbox_cursor.safe_id` is only a scan bound, advanced behind a 600 s window (contract rule 4). |
| `bk_inquiry_process` | Validates v1/v1.1/v1.2/v1.3/v1.4 (v1.3, AA-986 L6: each leg and day says `available` + `reason` + `reason_detail`; legs of stops whose tour AA no longer serves come last with `available: false`, parsed onto `Leg.available` / `reason` / `reason_detail` and kept in the snapshot (`days`, and per leg position under `legs`), not stored on `trip_case_legs` yet — the per-leg column + quote/hold guard are slice L5; until then the intake summary, audit row and staff notification carry `legs_unavailable` (count per reason) and the "new inquiry" e-mail marks each such tour); each leg gets the catalog tour key `(source_system, source_id)` from the v1.2 payload or by resolving `source_tour_id` against `catalog.v_tour` (null when unresolved; AA-830); creates a **lead** customer (`verified = false`, never merged into an existing customer; `needs_merge_review` + `merge_candidate_id` on an email/phone match) — except a **signed-in traveller** (v1.4, AA-908: `customer.identity` + `verified` + email, `Contact.signed_in`), who resolves to one **verified** customer: the identity's customer in `customer_identities` (bk-024), else the verified customer with that email (the identity is attached), else a new `verified = true`, `source = 'traveller_signin'` customer with name + token email only (no phone); an archived customer's identity is re-pointed; unverified `tripplanner_lead` / `manual` customers with the same email and no candidate get `needs_merge_review` + `merge_candidate_id` = that customer, never merged (summary `signed_in` = identity / email / created, `leads_flagged_for_review`, `customer_blocked`; nothing about the identity is logged; the identity must come from `COGNITO_EXTERNAL_ISSUER` over https, else the row is `failed`; one advisory lock per identity serialises concurrent sends; a blocked customer still gets the case and the notification says "Customer is blocked."), the trip case + legs, a staff `notifications` row (+ one `bk_notify_email` job for it, same transaction) and an audit row; marks the inbox row `processed`. `customer: null` or no email/phone → `parked`; invalid → `failed`. |
| `bk_retention_job` | AA-825 TTL (data standards §7, E3): deletes finished jobs past their window — `succeeded` 30 d; `failed` / `cancelled` 90 d; every batch of both parts archived to S3 first (`s3://aa-cis-bronze-<acc2>/archive/booking/job/yyyy/mm/`, gzipped JSON Lines, one object per part, batch and UTC month; a failed put deletes nothing); `queued` / `running` never. Rows referenced by a FK are never deleted. |
| `bk_retention_inbox_redact` | AA-825 / DM-6.6: 90 d after `received_at`, rebuilds `inbox_events.payload` from an allowlist of the contract's non-PII keys (`customer.name` / `email` / `phone` and `itinerary.custom_days[].note` nulled, any other key dropped, non-object parts nulled, so a failed non-conforming payload keeps no PII either) and replaces a failed row's `error`; sets `redacted_at` (bk-012). Row, `idempotency_key`, `contract_version`, status kept (dedupe unchanged); rows still `received` are skipped and counted. Nothing deleted, nothing archived. |
| `bk_tour_lifecycle_pull` | AA-986 L5 (root ADR 0015, Q4): queued by a maintenance hook only when `catalog.v_tour_lifecycle_outbox` (CIS 274, feature-detected) has a row `booking.inbox_events` lacks (idempotency key per UTC minute). One transaction per batch of 200: each new event goes into the inbox (source `cis`, contract `catalog.tour`, the producer key; `ON CONFLICT DO NOTHING`) and is applied: `retired` → one staff notification `tour.retired` (shared queue) per open trip case using the key (leg, the case's own key, or an item of a live version) and per trip plan (with its confirmed bookings on future departures, and its active B2B holds, which can no longer be confirmed: the text tells staff to contact the agency, `data.active_hold_list` names them); `restored` → `tour.reinstated` to the same, only when a retirement was notified; `paused` / `superseded` → nothing. Nothing is cancelled or changed. `inbox_cursor.safe_id` advances behind a 600 s window (contract rule 4). Not deploy-gate exempt (seconds). |
| `bk_case_lifecycle` | AA-682 S2 (bk-027): queued once per UTC day by the `OpsDailySchedule` hook (first pass at or after 01:00 UTC, key `bk_case_lifecycle:<UTC date>`). Moves open sold cases along the travel dates (`pre_departure`, `on_trip`, `completed`; see "Trip checks and the case lifecycle"), one gate notice `trip_case.gate_blocked` per blocked case / target / reasons (`notifications_reminder_uq`), and clears emergency contacts + check notes at trip end + 90 days. Batches of 200 cases, `FOR UPDATE SKIP LOCKED`, a savepoint per move. **DRY RUN first** (DM-7.2): a manual run is a DRY RUN unless `dry_run: false`; the scheduled run stays a DRY RUN until one live run over every case (no `case_ids`) has succeeded. Payload `{trigger, dry_run?, case_ids?}` (ids only, ≤ 50); result = mode, ops date + zone, move counts, moved / blocked case codes (≤ 50), cleared counts. Runs readable at `GET /booking/case-lifecycle/runs`. Not deploy-gate exempt (seconds). |
| `bk_ops_reminders` | AA-682 S2: same daily hook and key pattern. Notifications only, each once (`reminder_key`, `ON CONFLICT DO NOTHING`): `supplier_booking.waiting` (requested more than `supplier_answer_days` ago; to the requester, else the shared queue; again after a re-request) and `trip_check.due` (open check due within `trip_check_reminder_days` or overdue; to the assignee, else the shared queue; again when the due date changes). S3 (bk-028): `payment.due_soon` (a schedule row of a sent invoice due within `payment_due_soon_days`) and `invoice.overdue` (past due, at the steps of `payment_overdue_reminder_days` [1, 7], the highest step reached), each one staff row (the case's sales owner, else the shared queue) + one customer My Trips row (no e-mail); read in the ops time zone. S4 (bk-030): `supplier_bill.due` (a received or approved bill due within `supplier_bill_due_soon_days`, then once more when overdue; shared staff queue). Never writes money. Not deploy-gate exempt. |
| `bk_noop_sleep` | AA-993 G4 proof, **Dev only**: registered only when `JOB_TEST_KINDS` is truthy (`Settings.job_test_kinds`; AA-CIS-Infra sets it on the Dev task). Sleeps `payload.seconds` (1..3600) in 5 s steps, writes `progress.slept_seconds` each step and resumes from it after a release; a cancel request (`cancel_requested`, SQL only) ends its own row `cancelled`. Port of CIS `noop_sleep`. Deploy-gate exempt; never deploy-blocking. Touches nothing else; the serial worker runs nothing else while it sleeps. |
| `bk_b2b_hold_expire` | AA-973: queued by a worker maintenance hook only while a B2B hold is due (idempotency key per UTC minute, no row otherwise); moves due `active` holds to `expired` in batches of 500 (`FOR UPDATE SKIP LOCKED`, at most 20 batches). Idempotent; reads never wait for it. Not deploy-gate exempt (a run takes seconds). Each status write emits `hold.expired` (bk-022 trigger). |
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
The archive needs the task role to hold `s3:PutObject` on `archive/booking/*` (AA-CIS-Infra
`booking_ecs_task_retention_archive`, S251); without it a live run of `bk_retention_job` with any candidates fails
before deleting them. A **manual override** run (`POST /booking/retention/runs` with `older_than`, payload `trigger:
manual_override`, AA-825 S251, like CIS since S239) replaces every part's window by that UTC date so the delete /
archive / redaction path can be proven on Dev before rows age. The gate **fails closed**: it reads the raw process
environment (never `Settings.environment`, whose default is dev) and needs `ENVIRONMENT` dev / test / local **and**
`RETENTION_OVERRIDE_ENABLED=true`, which only the Dev task definition sets (AA-CIS-Infra `booking.tf`); unset or empty
refuses. Cutoff at least 7 days old, DRY RUN unless `dry_run: false` with `1 <= max_rows <= 1,000` (refused, not
capped; 422 from the API, a failed job otherwise); the status says `override.enabled` for this deployment. It is never "the last live run" (scheduled runs stay DRY RUN, the 2x rule ignores it); the
status shows it in its own `override` bucket and the audit summary starts with `override older_than <date>`.

## Health contract (ADR 0004 decision 9)

- `GET /booking/health` → 200 `{"status":"ok","service":"aa-booking-api","git_sha":"<sha>"}`.
  No DB call. Used by the ALB target group, so a DB outage never trips the ECS circuit breaker.
- `GET /booking/health/ready` → 200 `{"status":"ok","db":"ok","schema_version":"bk-005"}` when the
  DB answers and **every** `bk-NNN` in `REQUIRED_SCHEMA_VERSIONS` is registered (a gap, e.g. bk-005 in but
  bk-004 missing, is not ready); otherwise 503 with `db` = `ok` | `error` | `not_configured` (plus
  `missing_schema_versions` when the DB answers). Bodies never contain the DSN or error messages. Whenever the
  schema read succeeds (200 or 503) the body also carries `registered_keys`: every registered `bk-NNN`, sorted
  (AA-993 M4a, root ADR 0017 d7; read by the coming Deploy Dev `schema-gate`). Every body also carries `storage`
  (AA-682): `{"supply_media": …, "ops_docs": …}`, each `configured` or `not_configured` (the bucket env is set on the
  task or not; the bucket name is never shown). Informational only: it never makes the API not ready, since the
  routes without a bucket still work and the upload routes answer 503 themselves.
- **Post-deploy verify (AA-993 V2, ADR 0016 D3)**: after every Deploy Dev, `post-deploy-verify.yml` checks ECS rollout
  COMPLETED with desired >= 1 and the running image = `<registry>/<repo>:<sha>`, `/booking/health` `git_sha` + `version`
  of the deployed commit, and `/booking/health/ready` 200 with `schema_version` >= the newest `REQUIRED_SCHEMA_VERSIONS`
  key of that commit. Red, never a warning: desired count 0 is red (no automatic scale-up), and so is an unset
  `AWS_ROLE_ARN` (the `config` job fails). The **Deploy Dev result** job is red when any job did not succeed.

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
  contract views granted in bk-017 and bk-023. The RDS master `aa_cis_admin` still inherits `bk_owner` (rl-001; on
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
- **Column tags (data standards DM-6.1, bk-018, AA-877).** A column holding personal data carries
  `COMMENT 'pii:<class>; <free text>'` (`contact`, `name`, `address`, `profile`, `online_id`, `free_text`, `travel`,
  `payment`; further classes named later as `also pii:<class>`), commercial secrets `conf:commercial; …`, credentials
  and their hashes `conf:credential; …`. bk-008/009/010/012 tagged their own columns; bk-018 tagged the rest. A new
  column holding such data ships its tag **in the same migration file** that creates it (COMMENT replaces the whole
  comment, so keep any earlier text after the tag; a view rebuilt with DROP + CREATE must restate its tags).
  `tests/integration/test_bk018_pii_tags.py` fails on an untagged column named like personal data that is not on its
  allowlist; the schema reference section *Personal and confidential data tags (DM-6.1)* (AA-CIS-Infra
  `db/gen_schema_reference.py`, the authority) lists every tag and is the column inventory of the RoPA
  (AA-Ecosys `docs/legal/register.md`). bk-018 is COMMENT only: `REQUIRED_SCHEMA_VERSIONS` is unchanged.

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
`booking` has no DELETE / TRUNCATE). The supplier link came with `bk-032`.

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

### `bk-019` (AA-972 CRM-B1 — needs `bk-001`, `bk-003`, `bk-005`, `bk-009`, `bk-010`)

`b2b_partners` (agency CIS `tenant_id` unique soft key, `agency_name` `conf:commercial`, status `active|suspended`,
billing currency, `credit_terms_ref`, `m2m_client_id`; SELECT/INSERT/UPDATE, never deleted); `b2b_quotes` +
`b2b_quote_lines` (proposal ref/version, currency, FX lock: plain rate, buffer, date, source; rule `valid_from`, margin
and card fee `conf:commercial`, rounding, `total_b2b_price`, `price_hash`, `valid_until`; lines: component, kind,
service date, nights, pax counts by age band, rooms, unit, availability, `b2b_price`, `net_cost_usd` and
`supplier_rate_ids` `conf:commercial`) — **append-only** for role `booking` (SELECT, INSERT); `trip_plan_components`
(a hotel room type or a service on a day of a trip plan; SELECT, INSERT, DELETE for the replace-set PUT); settings row
`b2b_quote_valid_hours` = 48. No personal data. In `REQUIRED_SCHEMA_VERSIONS`.

### `bk-020` (AA-973 CRM-B2 — needs `bk-002`, `bk-003`, `bk-005`, `bk-009`, `bk-019`)

`departures` (trip plan, `start_date` unique per plan, `capacity` 0–1000, status `scheduled|guaranteed|cancelled`,
`auto_confirm`; SELECT/INSERT/UPDATE, never deleted; held / sold are NOT columns); `b2b_holds` (partner, client, quote,
proposal ref / version, `idempotency_key` unique per partner, status `active|confirmed|expired|released` with the
matching timestamp required by CHECK, `expires_at`, `extended_count` 0–1 for AA-974, `trip_case_id` on confirm,
`deposit_ref`; one `active`/`confirmed` set per partner proposal version, unique partial index) + `b2b_hold_lines`
(line ref, `departure_id`, `seats`, `held_as` `available|on_request`, `status` adding `confirmed|declined|cancelled`) —
SELECT/INSERT/UPDATE, no DELETE; `b2b_quote_lines.departure_id`; `trip_cases.channel` (`b2c` default, `b2b`) +
`party_tenant_id`, `customer_id` nullable with CHECK "b2c ⇔ customer and no agency, b2b ⇔ agency and no customer",
`source_system` + `crm`; settings row `b2b_hold_hours` = 48. No personal data. In `REQUIRED_SCHEMA_VERSIONS` (code PR).

### `bk-021` (AA-974 CRM-B2b — needs `bk-003`, `bk-005`, `bk-007`, `bk-009`, `bk-019`, `bk-020`)

`cancellation_policies` (channel `b2b`, `trip_plan_id` NULL = the default, `valid_from`, `title` / `note`
`conf:commercial`, `tiers` JSONB with a CHECK: 1–20 tiers, numbers, `min_days` 0–730 incl. 0, `fee_pct` 0–100; unique
per channel, plan and day, NULLS NOT DISTINCT; SELECT, INSERT only) + `policy_approvals.cancellation_policy_id`
(entity `cancellation_policy`, its own maker-checker trigger `cancellation_policy_maker_checker`; bk-007's function
unchanged); seed default B2B policy `00000000-0000-4000-8000-0000000bc021` approved `seed:bk-021` (proposed, to confirm
with Ms. Thư); `b2b_cancellations` (one per hold / trip case: policy, reason, Idempotency-Key, departure date, days
before, fee %, currency, total, fee, refundable with fee + refundable = total; SELECT, INSERT only); `b2b_holds`
`extended_at`, `extend_reason`, `extend_idempotency_key` (CHECK tied to `extended_count`), `cancellation_policy_id`,
index `(partner_id, proposal_ref, created_at)`, backfill of the seeded policy on holds already confirmed; settings `b2b_hold_extend_hours` = 72, `b2b_rehold_limit_per_day` = 3.
No personal data. In `REQUIRED_SCHEMA_VERSIONS` (code PR).

### `bk-022` (AA-975 CRM-B3 — needs `bk-003`, `bk-005`, `bk-019`, `bk-020`, `bk-021`)

`outbox` (the one Booking outbox: identity `id`, `event_id` unique, `stream` `b2b` (AA-835 adds `supply`),
`event_type` from the contract list, typed ids `partner_id` / `hold_id` / `booking_id` / `line_ref` / `departure_id`,
codes `incident_code` / `decline_code`, `occurred_at`, `feed_seq` (unique, from sequence `outbox_feed_seq`) +
`sequenced_at`; CHECKs tie each type to its ids and codes; no FKs (log); role `booking`: SELECT, INSERT of the
trigger columns only, UPDATE of `feed_seq` / `sequenced_at` only, no DELETE); event triggers on `b2b_holds`,
`b2b_hold_lines`, `b2b_cancellations`, `departures`, `departure_incidents` (+ fan-out function
`outbox_departure_event`); `departure_incidents` (code CHECK, flagged / cleared by + at, one open per departure;
SELECT, INSERT, UPDATE of the clear columns only); `b2b_hold_lines` `answer_due_at`, `answered_at`, `answered_by`,
`decline_code` (declined ⇔ a code; answered only once off `on_request`); `b2b_answer_due()`; settings
`b2b_on_request_answer_business_days` = 1, `b2b_ops_timezone` = Asia/Ho_Chi_Minh; backfill of `answer_due_at` on
older on-request lines. No personal data, no free-text column. In `REQUIRED_SCHEMA_VERSIONS` (code PR): apply bk-022
before the code deploys (readiness fails until then).

### `bk-023` (AA-835 supply content, root ADR 0004a — needs `bk-003`, `bk-010`, `bk-017`, `bk-022`)

`outbox` stream `supply` (columns `entity_type` `hotel|service`, `entity_id`, `supply_version`; event types
`supply.upsert` / `supply.deactivate`; CHECK `outbox_supply_chk`: a supply row names its entity and version and no b2b
id or code, a b2b row has none of the three; index on `feed_seq` for the supply stream); `supply_version` BIGINT on
`hotels` and `services` (1 at insert, +1 per material change of the contract facts, by the BEFORE triggers
`hotels_supply_version` / `services_supply_version`; any write of the column is one bump, never another value; notes,
links, source refs and audit columns are not material); a room type insert / material change bumps its hotel
(`hotel_room_types_supply_bump`); the AFTER trigger `supply_outbox` writes one supply outbox row per insert and per new
version (`supply.deactivate` while inactive, `supply.upsert` while active); `entity_media` (supplier photos: entity
type + id (no FK; the bump trigger refuses a missing entity), bucket `aa-booking-<env>-supply-media-<account>`, key
derived from the row `supply-media/<type>/<entity>/<id>.<ext>` by CHECK, JPEG/PNG/WebP ≤ 10 MB, sha256,
`usage_rights` default false, status `active|removed` (removed is final); a photo insert, rights flip or removal bumps
the entity; role `booking`: SELECT, INSERT, UPDATE of `usage_rights` / `status` / `updated_*`, no DELETE); views
(`security_barrier`, facts only, SELECT to `cis_app`, read-only for `booking`): `v_supply_hotels`,
`v_supply_services` (contract facts + id + version, room types and active photos as JSON), `v_supply_media` (active
photos with bucket / key / sha256 for the CIS copy), `v_supply_outbox` (stamped supply events; CIS pages on
`feed_seq`). Hotels and services that exist before bk-023 enter the stream on their next material change (or a touch
`SET supply_version = supply_version + 1`). In `REQUIRED_SCHEMA_VERSIONS` since the upload / read-back code (AA-835 S4):
apply bk-023 before that code deploys (readiness fails until then).

### `bk-024` (AA-908 traveller identities, root ADR 0006 Phase 4 — needs `bk-002`, `bk-014`)

`customer_identities` (`iss`, `sub` → `customer_id`; PRIMARY KEY `(iss, sub)`: one verified sign-in identity belongs to
one customer, a customer may hold several; `iss` https and ≤ 300 characters, `sub` 1–128 characters; both tagged
`pii:online_id`; FK to `customers` ON DELETE CASCADE; index on `customer_id`; role `booking`: SELECT, INSERT, UPDATE of
`customer_id` only (re-point from an archived customer), never DELETE; nothing for `cis_app`), and
`customers_source_chk` widened with `traveller_signin`. Filled by the inquiry intake from a signed-in hand-off
(`inquiry.submitted` v1.4, `customer.identity`); the intake code and `REQUIRED_SCHEMA_VERSIONS += bk-024` ship in the
AA-908 code PR, deployed after the apply (now in `REQUIRED_SCHEMA_VERSIONS`).

### `bk-025` (AA-992 data coverage — needs `bk-008`, `bk-009`, `bk-017`, `bk-019`–`bk-023` and Infra `rl-001`)

Two read-only views for the CIS admin page `/admin/data-coverage` (contract `ops.data_coverage.v1`, AA-Ecosys
`docs/contracts/ops.overview.v1.md`): `v_ops_data_totals (metric, n)`, counts as of now, metrics `trip_plans`,
`trip_plans_sellable`, `trip_plan_components`, `departures_open` (scheduled / guaranteed, start today or later),
`suppliers_active`, `contracts_published` (published and valid today), `rates_current`, `media_active`,
`media_active_with_rights`, `customers` (not soft-deleted), `b2b_partners_active`, `b2b_holds_active`; and
`v_ops_trip_plan_keys (source_system, source_id, sellable)`, row-level but catalog keys only (no plan id, none of the
`conf:commercial` columns), so CIS counts plans whose tour left the live catalogue. Owned by `bk_owner`; role
`booking` SELECT only; `cis_app` SELECT on both. Metric names and columns are append-only. Not in
`REQUIRED_SCHEMA_VERSIONS` (no Booking code reads them).

### `bk-026` (AA-682 S1 supplier bookings — needs `bk-003`, `bk-005`, `bk-008`, `bk-010`, `bk-022`)

`ops_transitions (entity, from_status, to_status)` + `ops_check_transition()` (TG_ARGV = entity; supplier-booking edges
now, invoice / supplier-bill edges in bk-028 / bk-030; read-only for `booking`); trip case edges `confirmed ↔
supplier_confirming`, `confirmed / supplier_confirming → pre_departure → on_trip → completed`, cancel from
`supplier_confirming` / `pre_departure` (money edges: bk-028) + the fact guard `trip_cases_post_sale_guard`;
`ops_files` (kind, entity, derived key in the ops-docs bucket, type / size / sha256 / name, `active | removed` final;
`booking`: SELECT, INSERT, UPDATE of the removal columns); `supplier_bookings` (above; unique live booking per version
item; `booking`: SELECT, INSERT, UPDATE, never DELETE); view `v_trip_case_supply_state`; settings
`supplier_answer_days` (2), `pre_departure_days` (14), `ops_docs_max_bytes` (10485760). Tags in the file (`net_amount`
conf:commercial; `notes`, `cancel_reason`, `file_name`, `removed_reason` pii:free_text; `s3_key` pii:payment). In
`REQUIRED_SCHEMA_VERSIONS` (code PR): apply bk-026 before the code deploys.

### `bk-027` (AA-682 S2 trip checks — needs `bk-003`, `bk-005`, `bk-026`)

`trip_case_checks` (code `pre_trip_recheck | emergency_contact | insurance | custom`, one standard row of each per case,
title, status `open | done | not_applicable`, due, assignee, done by / at, note, emergency contact name / phone on the
emergency row only, `cleared_at`; FK to the case ON DELETE CASCADE; `booking`: SELECT, INSERT, UPDATE of the working
columns, never DELETE) + guard `trip_case_checks_guard` (edges, fixed code / standard title, no emergency contact on a
B2B case, cleared final); trigger `trip_cases_seed_checks` on `trip_cases` (seed on entering `confirmed`, due dates
follow the start date) + one backfill of the open sold cases; view `v_trip_check_board` (stored counts + derived
`suppliers_confirmed` / `payment_verified`); index `notifications_reminder_uq` (moved forward from the bk-028 plan);
settings `trip_check_reminder_days` (3), `emergency_contact_keep_days` (90). Tags in the file (`emergency_contact_name`
pii:emergency_contact + name, `emergency_contact_phone` pii:emergency_contact + contact, `note`, `title`
pii:free_text). In `REQUIRED_SCHEMA_VERSIONS` (code PR): apply bk-027 before the code deploys.

### `bk-028` (AA-682 S3 invoices and payments — needs `bk-003`, `bk-005`, `bk-026`, `bk-027`)

`invoices` (INV- code, proforma, one live per B2C case, bill-to snapshot at send, `idempotency_key` + `request_sha256`
unique per case, soft delete of a draft; `booking`: SELECT, INSERT, column UPDATEs, never DELETE) + `invoices_guard`
(empty draft of a B2C case on its approved priced version in its currency; fixed facts; total = Σ lines; send needs
the version line and a schedule adding up; money states equal the verified payments; cancel only with net 0) + edges
in `ops_transitions`; `invoice_items` (version total locked, adjustments with a reason; after send append only;
`booking` may DELETE draft rows, the guard refuses the rest); `payment_schedule` (seq 1–10, kind, amount, due; covered
rows frozen after send); the amendment window `invoices.amending` (after send, lines and schedule rows change only
while it is true; `invoices_guard` checks Σ schedule = total immediately whenever a sent invoice is written with it
false: at send, when the window closes, on every settle; no verify / void / cancel while it is true; no deferred
constraint trigger, which the AA-CIS-Infra X1 lint refuses under the owner-role switch); `payments` (PMT-
code, receipt / refund, method, `received_on`, reference, evidence, maker-checker CHECK, void final, Idempotency-Key
unique per invoice; `booking`: SELECT, INSERT, column UPDATEs) + `payments_guard` + `payments_settle`;
`invoice_net_paid` / `invoice_money_state` / `invoice_settle` (locks the invoice first); views
`v_payment_schedule_status` (waterfall, scheduled / due / overdue / paid) and `v_invoice_balance`; the money edges of
`trip_case_status_transitions`; replaced `trip_cases_post_sale_guard` (money statuses equal the invoice, never B2B; B2C
pre_departure only once paid; reopen refused while an invoice / payment is live), `ops_files_guard` (payment
evidence) and `v_trip_check_board` (`payment_verified`); settings above. Tags in the file (`bill_to_name` pii:name,
`bill_to_email` pii:contact, `bill_to_address` pii:address, `reference` pii:payment, notes / terms / reasons
pii:free_text). In `REQUIRED_SCHEMA_VERSIONS` (code PR): apply bk-028 before the code deploys.

### `bk-030` (AA-682 S4 supplier bills and payouts — needs `bk-003`, `bk-008`, `bk-026`, `bk-028`)

`supplier_bills` (BIL- code, received → approved (maker-checker CHECK + guard, ≥ 1 line, lines = amount) → paid
(derived from payouts), cancel with a reason; `idempotency_key` + `request_sha256` unique; `booking`: SELECT, INSERT,
column UPDATEs, never DELETE) + `supplier_bills_guard` + `supplier_bill` edges in `ops_transitions`;
`supplier_bill_items` (PK bill + booking; confirmed bookings of the bill's supplier / case; insert / delete while
received only, never update; a bill with a document takes no B2B line); `supplier_payouts` (SPO- code, approved bill
only, bill currency, recorder ≠ bill creator, never above the amount, void final; Idempotency-Key unique per bill;
`booking`: SELECT, INSERT, UPDATE of evidence and void columns) + `supplier_payouts_guard` + `supplier_payouts_settle`;
`supplier_bill_paid` / `supplier_bill_settle` (locks the bill first); view `v_supplier_bill_balance`; replaced
`ops_files_guard` (bill documents, payout evidence); setting `supplier_bill_due_soon_days`. Tags in the file
(amounts, bill number, payout reference conf:commercial; notes / reasons pii:free_text). bk-029 is AA-996's geo
profiles. In `REQUIRED_SCHEMA_VERSIONS` (code PR): apply bk-030 before the code deploys.

### `bk-031` (AA-996 CIS tour countries in `countries` — needs `bk-005`)

Data only: `countries` gains `KG` Kyrgyzstan, `PK` Pakistan, `TW` Taiwan, `UZ` Uzbekistan (active, CIS
`shared.countries.name` spelling), so every AA tour country of CIS (CIS 279 list) is in Booking's reference list and
Catalog → Countries can keep state for them (`booking_reference` true by itself, no code change). `ON CONFLICT DO
NOTHING`; grants unchanged (`booking` reads only). Not in `REQUIRED_SCHEMA_VERSIONS`: the code works before and after
it (those countries are read-only until it is applied).

### `bk-032` (AA-996 trip plan supplier and price matrix — needs `bk-003`, `bk-008`, `bk-009`)

`trip_plans.supplier_id` (FK `suppliers`, nullable, `conf:commercial`; set or changed only to an active supplier, an
unchanged one may have been deactivated since) and `trip_plan_prices`: the per-person **selling** price list of a
plan (root ADR 0005 d3/d6), one row per channel × season label / validity (`valid_from`..`valid_to`) × pax band
(`pax_min`..`pax_max`, null = and more) × selling currency, with `peak` (the rule's peak uplift), `amount`, and
optionally the net cost per person it was priced from (`net_amount` + `net_currency`, never leaves Booking) with the
`effective_margin` and `priced_on` of the save; a price sent with a net cost is a sales override (`overridden`). Replaced as a set by `PUT /booking/catalog/trip-plans/{source_id}/prices`
(SELECT, INSERT, DELETE; no UPDATE, no trigger), one `trip_plan.prices_replaced` audit row per change (rows before and
after; an override below the floor needs `below_floor_reason`). Overlaps per channel + currency are refused by the
API. Current-state list (history in `audit_logs`): no DM-7.1 row. In `REQUIRED_SCHEMA_VERSIONS` (every trip plan
query reads `supplier_id`): apply it before the code deploys. The from price (cheapest B2C row covering 2 pax, still
valid, USD first) is derived on read; `booking.v_from_prices` (contract `price.from.v1`) is still to come, and the B2B
parity read keeps the bk-009 price hint until then.

Waiting for decisions: `trip_case_countries` / `areas` / `country_profiles` (O7,
ISO mapping), `v_from_prices` and catalog-backed version item types (AA-679 follow-up),
operations S5 (My Payments: AA-682).

## Out of scope today

A payment provider (money is marked by hand, AA-682 D6), tax lines / the legal VAT e-invoice (proforma only, D7),
agency settlement (B2B cases have no customer invoice, CRM O-10), a customer notifications feed (the customer rows
of AA-682 are written but `booking.customer.v1` serves payments only; critic note 11),
**customer e-mail** (proposal send, invoice send and reminders write customer notification rows only and say so; the
only e-mail today is the staff new inquiry e-mail, AA-811), Booking-side SES bounce/complaint suppression, a catalog
sync (D4: live reads instead; the only catalog outbox consumer is the tour lifecycle pull, AA-986), catalog titles on trip case legs (Q11),
`v_from_prices`, destinations / countries pages, rate-sheet upload (rates are entered one by one under a draft
contract), contract document upload, supplier hard delete,
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
  Offered only on the registered hosts with the build's client secret set: a Vercel production build uses the
  `booking-staff` client (`COGNITO_BOOKING_STAFF_CLIENT_SECRET`, Production target only) and accepts
  `booking.lumiguides.it.com` and `aa-booking-admin.vercel.app`; a preview build uses the Preview-only
  `booking-staff-preview` client (`COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_SECRET`, Preview target only; client id a code
  constant filled in after the Infra apply) and accepts only the fixed Preview host `booking-preview.lumiguides.it.com`;
  `next dev` uses `booking-staff` on the production hosts and `localhost:3001` (`VERCEL_ENV` picks client and list). Per-deployment `*.vercel.app` preview URLs get only the legacy form; the Preview
  host never shows the legacy form and answers 404 on `POST /api/auth/login`.
- **Preview sign-in (AA-911, root ADR 0006a A11):** `booking-preview.lumiguides.it.com` is a Vercel domain of
  `aa-booking-admin` (bound to the never-pushed branch `preview-signin`, so production deploys never claim it) and
  the only callback / sign-out URL of the Preview-only `booking-staff-preview` client (AA-911 decision D-1; the
  production `booking-staff` client does not list it). The Vercel Deploy workflow points it at a preview only on
  opt-in: the PR carries the label `preview-signin` when its preview deploys, or a dispatch with
  `alias_only_url=<https://aa-booking-admin-….vercel.app>` (checked over the Vercel REST API: this project, not
  production, READY). Preview builds send `X-Robots-Tag: noindex, nofollow`. Under Standard Deployment Protection the
  host is most likely behind Vercel Authentication (Hobby: only the owner passes; automation needs the project's
  protection-bypass value). Unmerged same-repo PR code on that host sees the Booking tokens of whoever signs in,
  but those are `booking-staff-preview` tokens, which the API caps at read-only (`app/auth/staff.py`: any
  non-GET/HEAD/OPTIONS is 403 `preview_read_only` before any DB access; permissions limited to `PREVIEW_PERMISSIONS`;
  `/booking/staff/me` says `preview_viewer: true`). Runbook: root
  `docs/ops/runbooks/preview-staff-sign-in.md`.
- **Legacy CIS password bridge (until AA-680 PR-4)**: `POST /api/auth/login` (CIS `/auth/admin-login` →
  `/auth/booking-token`) → `bk_cis_session` + `bk_access`; `proxy.ts` and the BFF re-mint the 15-minute Booking token
  from the CIS session. On hosts with company sign-in the form sits collapsed under the company button (E2E account).
  One session kind at a time: each sign-in clears the other kind's cookies.
- `/api/booking/<path>` → `<BOOKING_API_URL>/booking/<path>` with the access token as Bearer. Tokens live only in
  httpOnly cookies. Booking roles of company sign-in profiles are read-only on `/staff` (set in CIS → Platform →
  Staff, API 409 `managed_in_directory`); Activate / Deactivate stays.
- UI kit (AA-994 S4b, root ADR 0016): `frontend/ui/core/` is the kit core, a byte copy of AA-CIS-App
  `frontend/app/_kit/core/` written by the root `tools/kit/kit-sync.sh` and pinned in `frontend/kit.lock.json`; it is
  never edited here (the kit-sync bot opens the sync PR after a CIS core change). It carries the one `--aa-*` token set
  (`ui/core/tokens.css`, imported by `app/globals.css`), the primitives, Modal / ConfirmModal, Drawer, Toast, DataTable,
  PageFrame, Section, KpiTile, FilterBar + `useUrlFilters`, RowMenu, DateField, Money / DateText and the kit lint
  (`ui/core/eslint-kit.mjs`). `frontend/ui/local/` is the Booking staff shell (sidebar, topbar, bottom nav, ⌘K
  palette, staff context, nav, `shell.css`, the theme store bound to `bk_theme`; the sidebar state uses the core key
  `cis_sidebar`, carried over once from `bk_sidebar`). `frontend/ui/booking/` holds Booking components (Tailwind
  utilities on the same variables) and pure helpers; Booking-only CSS values are `--bk-*` (`ui/booking-tokens.css`).
  Pages import everything from `@/ui` (the barrel `ui/index.ts`; `ConfirmDialog` is the core `ConfirmModal`,
  `PageFrame` the core frame with the Booking wide column and gutter). Frontend Build checks the copy (integrity,
  `--aa-*` definitions only in the core tokens, provenance of the lock's CIS commit on CIS main) and lints `app/**`
  and `ui/booking/**` with the kit `tailwind` preset at error; violations from before S4b live in
  `frontend/eslint-suppressions.json`, which may only shrink.
- Case workspace and proposals (AA-996 audit slice 4, S254): the case header names the customer, the sales agent and
  the date range with its length; Overview puts the trip facts (two columns, Edit pencil, approved total) in the main
  column, a custom trip's empty legs card is one line, the TripPlanner inquiry panel shows the client's message,
  budget, travel window, style and party when the snapshot carries them, and Countries falls back to `leg_countries`
  (`GET /booking/trip-cases/{ref}`: the CIS tour country of each keyed leg, then the countries of the inquiry's
  route-day places; read live, fail soft). The Proposals tab rows have Preview / Send / Discard draft. A sent or
  approved version's price panel shows the saved total, channel, FX lock and dates and asks for no live quote; a
  read-only session's 403 on the quote (`e2e_read_only`, `preview_read_only`) is explained, not shown as a role
  problem. Approving a version with no items is 422 `version_empty`. Convert / Decline dialogs use the Modal footer.
- Compact density (AA-996, S254): pages import `DataTable` from `@/ui`, which is the Booking wrapper
  `ui/booking/DataTable.tsx` around the kit core table. From 768 to 1199 px CSS wide (zoomed laptops) cells are tighter, the
  sidebar is the icon rail, the page gutter is 20 px (`app/globals.css`, pure CSS) and columns marked
  `meta: { compact: "drawer" }` move into a row details drawer; at 1200 px and wider nothing changes.
- List and date pieces (AA-996 audit slice 1, S254): a list page passes its `FilterBar` to the table through the
  DataTable `filters` prop, which gives every list the Figma shape: row 1 search box + the table's Columns / CSV, row 2
  the filters (labels inline) + "Reset filters" (always drawn, disabled at the defaults). Every list page (a
  sidebar list and the Supplier bills and case Proposals tabs; not the small tables inside a detail page, tab or drawer)
  exports CSV (`enableCsv`; a display-only column gets an `accessorFn` with sorting off; `csv` on the wrapper when a
  cell groups two facts; the Countries grid uses `ExportCsvButton` in the bar's `tools` slot). Every Booking CSV goes
  through the wrapper's button: row tools (row menu, open link, details) are left out and every value passes the
  spreadsheet formula guard `ui/booking/csvCell.ts`. Dates are typed and shown en-GB through the kit
  `DateField` ("28 Oct 2026", day-first input; `ui/core/DateField.tsx` since AA-994 S4a/S4b) and the Booking
  `TimeField` (24 h, `ui/booking/TimeField.tsx`) instead of the browser's
  locale inputs; the box always shows the value the page holds (an out-of-range day is passed up and marked invalid so
  the page's own checks block it; unreadable text is dropped on blur); date-only values ("2026-10-09") are formatted as calendar days (`ui/booking/dates.ts` `formatDay`,
  `DateText`), and ISO dates inside notification text are localised (`localizeIsoDates`). `npm run test:unit` runs the
  pure helpers' tests.
- Light table (AA-996, S254): `LightTable` from `@/ui` (`ui/booking/LightTable.tsx`, an ARIA table on a CSS grid)
  is the sanctioned exception to "DataTable for tables", only for short, fixed lists inside one record's card (the
  invoice's lines, payment schedule and payments): no pager, search or Columns menu, stacked rows under 640 px. Every
  real list (anything that can grow, be filtered or paged) uses `DataTable`.
- Auth hardening: `lib/safeNext.ts` (post-login target stays same-origin), `lib/sameOrigin.ts` (BFF writes, login and
  logout refuse cross-origin requests), `ui/booking/QueryProvider.tsx` (a 401 anywhere goes to `/login?next=…`; 4xx is
  never retried). The kit core `ui/core/Providers.tsx` is not mounted.
- Every route of UI spec §3 exists. Wave 1 pages, `/audit-log` (S237, over `GET /booking/audit-logs`) and
  `/pricing/rules` (S237, AA-810: rules + FX tabs, new-row drawers, Approve), `/suppliers` (S237, AA-822: list with
  KPIs, add / edit form with the CIS place typeahead, detail with Profile + Contracts + Rates tabs (AA-823: rates per
  contract, editable while the contract is a draft); Bookings / Bills tabs are frames), `/catalog/trip-plans` (S237,
  AA-679: list, detail with CIS days / photos tagged "From CIS", commercial drawer, "Use for trip case" modal; CIS
  photos load from the CIS public API, `lib/config.ts` `CIS_MEDIA_URL`), `/hotels` (S237, AA-823: grid + hotel drawer
  with facts, room types, rates, activity; `?hotel=HTL-…`) and `/services` + `/services/{SVC-…}` (S237, AA-823) read
  the real API; AA-986 L5: Trip plans has a "Show: Orphaned plans" filter (KPI tile + table of plans whose tour left
  the catalog, with reason and what is attached; a paused tour is not listed), the case page flags each leg whose tour
  is retired / out of the catalog and shows an alert box for them (a pause carries no staff flag, ADR 0015), and the bell labels `tour.retired` / `tour.reinstated`; AA-975: `/b2b-desk` (Operations → B2B desk: KPI tiles, an "On
  request" tab with SLA badge, Accept / Decline (reason code), and a "Departures & incidents" tab with Flag incident /
  Clear; the bell's `b2b.on_request` opens it); the proposal
  item drawer can book a room type or service (its net defaults from the rate); AA-835 S4: the hotel drawer and the
  service Overview show "Customer content (From CIS)" (status, published text and photos, "Edit in CIS" = the
  `NEXT_PUBLIC_CIS_ADMIN_URL` origin + the path CIS serves; hidden while the API sends `content: null`) and "Supplier
  photos" (upload, usage rights, remove: `supply.write`; every photo is re-encoded in the browser first (no EXIF
  reaches S3; downsized to at most 3 MB), and the BFF lets only that upload route carry up to 4.4 MB of body, under Vercel's 4.5 MB limit); the proposal builder and
  preview show the published description under an item that books supply; AA-682 S1: the case page has a
  **Suppliers** tab (Generate from the approved version, Add booking, Mark as requested, Confirm with the supplier's ref
  and on a B2C case a voucher / confirmation file (PDF as is, photos re-encoded; the BFF lets `POST ops-files` carry the
  same 4.4 MB as photo uploads), Declined, Cancel, Change with supplier; documents download through a 5-minute link),
  the header shows the supply badge ("Suppliers n/m" or "Confirmed & Booked"), "Change status" never offers a post-sale
  status, the supplier **Bookings** tab lists its bookings (link to the case), the dashboard has a "Supplier answers
  pending" tile, and the B2B desk links a line covered by a supplier booking to the case instead of Accept / Decline;
  AA-682 S2: **/trip-checks** (checks across the open sold cases, filters status / due / owner / check / search, the
  check drawer, the latest daily lifecycle runs) and the case **Checks** tab (derived checks, standard + custom checks,
  Add check, the same drawer; emergency contact fields on a B2C trip only); the
  other wave 2/3 pages render their layout frame with "Coming in wave N". Each nav entry in
  `frontend/ui/nav.tsx` carries its own `shipped` flag (set it when that page reads the API), so pages ship one at a
  time. The UI spec lives in `docs/ui-spec.md` (AA-683).
- Environment (server): `BOOKING_API_URL` (default: the Dev execute-api stage), `CIS_API_URL` (default: the live CIS
  API), `COGNITO_BOOKING_STAFF_CLIENT_SECRET` (secret, Production target; unset = no company sign-in),
  `COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_SECRET` (secret, Preview target only, AA-911), optional
  `COGNITO_BOOKING_STAFF_CLIENT_ID` / `COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID` / `COGNITO_WORKFORCE_DOMAIN` /
  `COGNITO_WORKFORCE_ISSUER` (Dev defaults in code).
  Public: `NEXT_PUBLIC_CIS_ADMIN_URL`. Staff never sign in with Google (ADR 0006a A9): the AA-703 Google placeholder is
  removed.
- Frontend deploys (S241): CI-created via `.github/workflows/vercel-deploy.yml` (Vercel project `aa-booking-admin`),
  only when a PR or a push to `main` touches `frontend/**` — PR → preview + one PR comment with the URL (and whether
  the Preview sign-in host now serves it), `main` → production. The Vercel Git integration is off via `frontend/vercel.json` (`git.deploymentEnabled: false`) because
  the Hobby team cap (~100 deployments / 24 h for all projects, canceled ones included) was being used up. Needs the
  repo secret `VERCEL_TOKEN`.
- Security headers (AA-879): `frontend/next.config.ts` sends the same six headers as the CIS admin, the CIS portal and
  TripPlanner (HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, a minimal CSP `frame-ancestors 'none';
  base-uri 'self'; form-action 'self'; object-src 'none'`, `Permissions-Policy`). Every preview and production
  deployment is checked by the step "Check the security headers" (`.github/scripts/check_security_headers.sh`, GET
  `/login` through `vercel curl`, which uses the project's automation-bypass token with `VERCEL_TOKEN`).
- Public smoke (AA-993 V5): on a production run the job `smoke` of `vercel-deploy.yml` runs the Playwright project
  `smoke-public` (`frontend/e2e/smoke-public/`) against `booking.lumiguides.it.com` after the deploy: GET `/login` 200
  on the same host, the card and "Sign in with company email" render, no console error, no failed same-origin request,
  every `next.config.ts` header with its value. Signed out, no secret; screenshot artifact `booking-admin-smoke-public`.
  Job `result` is red on a production run unless deploy and smoke both succeeded; previews are not gated by it.
- Signed-in smoke (AA-910, design note only, no job yet): `frontend/e2e/SIGNED-IN-SMOKE.md`. E2E staff user
  `e2e-booking@e2e-mail.lumiguides.it.com`, group `booking-admin` only, email code read from the SES test mailbox
  (root ADR 0006a A10a); no password, no secret. Its session is read-only on every client: non-safe methods are
  403 `e2e_read_only` before any DB access (`app/auth/staff.py` `E2E_TEST_STAFF_EMAIL`), reads keep admin.
- Image scan (AA-879): Trivy in "Docker Build Check" fails on any fixable CRITICAL/HIGH finding (`ignore-unfixed`);
  accepted ones go into `.trivyignore` with a `# reason` line and `exp:YYYY-MM-DD` ≤ 90 days ahead.

## Working rules

- Trunk-based: branch → PR → 5 required checks (Lint, Security Audit, Unit Tests, Integration
  Tests, Docker Build Check) → human merge to `main`. Agents never merge.
- Commits: `feat|fix|chore|refactor|test|docs: <english>` + body `Refs AA-xxx` (never `Fixes` /
  `Closes`, never `[AA-xx]` in the subject).
- Everything in Git is English. Done = merged + CI green + Dev rollout COMPLETED + live
  `/booking/health/ready` verified (`aa-ship`).
- A new contract version: copy the root file byte-for-byte into `backend/app/contracts/`, pin its
  SHA-256 in `tests/unit/test_inquiry_contract.py`, accept every minor of the major (contract rule 2).
