---
id: ARCH-06-booking
title: "AA-Booking — Repo Context"
description: Stack, boundaries, ownership, domain terms and working rules of AA-Booking (booking API, booking job worker and staff admin); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-booking-api, aa-booking-admin]
last_reviewed: 2026-11-01
review_every_days: 90
related: [ARCH-01, ARCH-05, ARCH-08, docs/adr/README.md]
---

# AA-Booking — Repo Context

AA-Booking (AAA) is the booking back office of the Adventure Asia ecosystem: trip cases, proposals,
suppliers and the B2B partner API for the partner agencies, and agency billing. (Its B2C half, customers, TripPlanner
inquiries, customer invoices and payments, is retired since AA-1046: see "The B2C part is retired".) It is built in-house from scratch (no vendor code was delivered). Today the repo holds
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
6. Root ADR 0017 — migrations ship in the app PR: an expand file is applied on Dev with no hand step, a contract file
   waits for a manual DB Migrate apply (see "Database rules").
7. Root ADR 0018 — lean AWS account model: Dev stays on acc2 (`aa-nonprod`); prod will be its own account `aa-prod`
   with its own Booking service, schema and roles, built at go-live.
8. Root ADR 0009 decision 13 — Booking makes no AI call; when it needs one it goes through CIS jobs or the shared AI
   gateway core (see "AI boundary").

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
  `v_destination_photo` feature-detected; AA-997: with `catalog.v_destination_redirect` (CIS 257) and
  `v_destination.geo_status`, a place CIS hides from customers or merged stays listed for staff, flagged
  `hidden_from_customers` + reason, and the picker never offers a merged place) and `catalog.v_tour` / `v_tour_day` /
  `v_tour_photo` (trip plan endpoints, AA-679, S237 decision D4). **Tour liveness** (AA-986 L5, root ADR 0015
  decision 5, contract `catalog.tour.v1.3`, CIS migration 274): `catalog.v_tour_retired` (tombstone: `retired` /
  `paused` + reason; a `restored` row of a key served again is no tombstone), read live next to `v_tour` (`app/catalog/liveness.py`), and `catalog.v_tour_lifecycle_outbox`,
  pulled by `bk_tour_lifecycle_pull` into `booking.inbox_events` (source `cis`, contract `catalog.tour`) for staff
  notifications. Both are feature-detected: without CIS 274 Booking degrades to `v_tour` alone. **Supply content
  read-back** (AA-835 S4, root ADR 0004a decision 4, CIS migration 278): `catalog.v_supply_content`, read live
  (`app/supply/content.py`): the hotel / service detail shows what CIS published (status, text, photo URLs, the CIS
  `edit_url` path for "Edit in CIS") and proposal items carry only **published** description and highlights. **AI mark**
  (AA-1040, EU AI Act Art. 50; CIS 358): the published text is drafted by the CIS writer `supply_content_writer`, so
  `content.ai` and `supply.ai` = `{ai_generated, generator, model: null, generated_at, human_edited}` from the view's
  `ai_generated` / `ai_generated_at` / `ai_human_edited` (detected on their own, `MARK_VIEW`: before 358 `ai` is null);
  the staff UI shows the kit `AiNotice` badge and `data-ai-*` attributes on that text (`ui/booking/aiMark.ts`: badge
  unless CIS says a person wrote it from scratch) in the hotel / service CIS panel, the service form and the
  proposal builder and preview. **Proposal
  item read-back** (AA-1013, `app/trip_cases/cis_readback.py`): every items response gives each item made from a
  CIS tour `cis` (tour title and `v_tour.updated_at`; for a tour copied day by day, the CIS day it stands for, today's
  day title and `changed`; B2B confirm items, TripPlanner legs, items titled with the tour and a lone one-day item of a
  multi-day CIS tour are never compared), read live (one `v_tour` read, one `v_tour_day` read by `tour_id = ANY`), fail soft
  (`cis` null). Stored titles never change by themselves: the builder offers "Refresh from CIS" on a draft (a normal
  items save by staff), a sent or approved version keeps what the customer saw. Also
  feature-detected: until CIS 278 is applied (or when the read fails with any database error, such as a column type
  CIS changed) `content` is null and the staff UI hides the panel; a CIS migration never fails a Booking request.
  **Agency billing read** (AA-1037, CIS 377, contract tenant.entitlements.v1 1.3): `catalog.v_tenant_billing_v1` (one row
  per tenant and subscription: plan price, unit, seats, status, and the bill-to fields CIS holds), read by
  `bk_saas_invoice_draft` and by an invoice issue (bill-to snapshot); feature-detected (`app/agency_billing/cis.py`).
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
  (granted in CIS → Platform → Staff). The CIS bridge token (ADR 0004 O1) is no longer accepted: AA-911 (S271) turned
  the bridge issuer off and CIS no longer mints it. Booking keeps only a thin staff profile
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
  `/admin/data-coverage` reads them. **Inquiry handoff** (AA-1003, `bk-035`): `booking.v_ops_inquiry_inbox` (source,
  hand-off key, status, received / processed time, has a trip case; no payload or customer), SELECT to `cis_app`; no CIS
  reader since 2026-11-03 (CIS retired TP-03 with the B2B-only model, root ADR 0019; the view goes with the B2C drop).
  **Public from price** (AA-1029, bk-039, contract `price.from.v1`): `booking.v_from_prices` (per catalog key, the
  cheapest suggested retail price per person for 2 travellers among the b2b rows still valid; no net, agency price or
  margin), SELECT to role `tripplanner` (+ USAGE on schema `booking`) for the TripPlanner tour page, and to `cis_app`
  (bk-040) for the agency Content studio Marketplace (CIS `GET /v1/tours/pool` attaches it to each pool row).
  **Agency billing status** (AA-1037, bk-042, root ADR 0019 d9): `booking.v_tenant_billing_status` (per agency: status
  ok | warn | past_due from the oldest overdue issued invoice against its warn / suspend days, oldest overdue days,
  overdue invoices and amount per currency, paid-through day of the software fee, the warn / suspend days), SELECT to
  `cis_app`: the CIS hourly `tenant_billing_tick` moves the account (CIS stays the only writer of account state).
  CIS Tenant 360 links to the staff page `/agency-billing?tab=invoices&tenant=<CIS tenant uuid>`: the Invoices tab sends
  `tenant_id` to `GET /booking/agency-invoices` and shows a removable chip with the agency's name; a value that is not
  a UUID is ignored (the unfiltered list).
  No other `booking.*` object is granted to CIS (isolation probe: `cis_app` →
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
| Frontend | Staff admin in `frontend/` (AA-683): Next.js 16 App Router, React 19, TypeScript, Tailwind v4 on the UI kit core tokens (`frontend/ui/core`, root ADR 0016), react-query, TanStack Table. On ECS as its own service `aa-booking-dev-admin` (AA-911, `deploy-admin-dev.yml`, see Staff admin frontend; the Vercel project was retired in S272). Customer surface (O8) not started |

## Layout

```text
backend/
  app/main.py              FastAPI app factory; every path is under /booking
  app/routers/health.py    GET /booking/health, GET /booking/health/ready
  app/routers/docs.py      GET /booking/openapi.json, GET /booking/docs (staff.admin outside test/local, AA-876)
  app/routers/staff.py     GET /booking/staff/me, admin GET/PATCH /booking/staff (PATCH `reason`: audit row only, AA-996)
  app/routers/trip_cases.py  staff trip case + version endpoints (/booking/trip-cases*, /booking/versions)
  app/routers/notifications.py  /booking/notifications* (staff feed, per-staff read state)
  app/routers/reference.py  /booking/settings, /booking/countries, /booking/audit-logs (admin; `q` text search, AA-996)
  app/auth/                identity_verifier.py (byte copy of AA-CIS-App api/core/identity_verifier.py, the multi-issuer
                           verifier; never edited here), identity_config.py (accepted issuers + clients, JWKS prefetch,
                           AA-680), profile / Cognito identity link / permissions (staff.py), admin ops (admin.py),
                           deploy workflow GitHub OIDC verifier (ci.py, AA-751) over the single-issuer cache (tokens.py)
  app/auth/m2m.py          aa-m2m client-credentials tokens for the B2B API (AA-972): IssuerConfig over the vendored
                           verifier (scope claim as groups), 401 / 403 split, scope constants
  app/b2b/                 booking.b2b.v1 (AA-972): access.py (scope + partner check), errors.py (contract Problem),
                           components.py (component ids, net cost of a line from rates in force), quotes.py (price,
                           price_hash, append-only store), reads.py (products, FX rates, price parity), partners.py and
                           plan_components.py (staff admin); AA-973: departures.py (capacity, held / sold counted under a
                           departure row lock, line availability, staff departure edits), holds.py (place / read /
                           release / confirm → B2B trip case + approved version), expiry.py (bk_b2b_hold_expire job + hook);
                           AA-974: holds.py also extend once + re-hold limit, cancellation.py (fee policies: tiers,
                           in force, staff admin; cancel / dry run → b2b_cancellations); AA-1030: agency_pricing.py
                           (partner tiers, tier discounts, contract rate cards: the discount a quote line gets,
                           maker-checker staff admin, partner tier assignment); AA-1073 (bk-046): departures.py also
                           the minimum / sales cutoff / decision date, the allotment seat count and the recurring
                           generator, allotments.py (agency seat allotments: propose, approve maker-checker, end,
                           bk_allotment_release), departure_watch.py (bk_departure_watch: guarantee at the minimum,
                           at risk on the decision date; DepartureDailySchedule; staff "Run checks now"); AA-1073
                           rest (bk-048): departure_board.py (the board over v_departure_occupancy + drill-down),
                           departure_ops.py (meeting point / time / note, departure_staff), departure_fees.py (fees
                           charged to agencies, maker-checker), price_adjustments.py (per-departure price changes,
                           maker-checker; applied by quotes.py with the early-booking discount)
  app/routers/b2b.py       /booking/b2b/v1/products, /quotes, /fx-rates, /price-parity (M2M only, AA-972); /departures,
                           /holds, /holds/{id} (GET, DELETE), /holds/{id}/confirm (AA-973); /holds/{id}/extend,
                           /bookings/{id}/cancel (AA-974); /bookings/{id}/confirmation, /terms (AA-1037, minor 1.3)
                           /tariff (AA-1030, 1.4); /invoices, /invoices/{number}, /billing-status (AA-1037, 1.5)
  app/routers/b2b_partners.py  /booking/b2b-partners (GET, POST, PATCH /{id}; b2b.admin = admin)
  app/routers/ops.py       GET /booking/ops/deploy-gate (machine route: Deploy Dev workflow OIDC token only, AA-751)
  app/ops/                 AA-682 operations after the sale (bk-026): supplier_bookings.py (generate / add / edit /
                           request / confirm / decline / cancel / re-request, supply state, case auto-advance, case-cancel
                           cascade, B2B line tie-in), files.py (ops-docs bucket: base64 upload, attachment download);
                           S2 (bk-027): trip_checks.py (list / case / custom / patch, derived checks, B2C-only emergency
                           contact), lifecycle.py (bk_case_lifecycle + bk_ops_reminders, OpsDailySchedule hook);
                           money.py (Decimal amounts, request hash, card refusal; the S3 customer invoices are retired,
                           AA-1046), track.py (case auto-advance over the supply track);
                           S4 (bk-030): supplier_bills.py (enter / edit / approve / cancel bills, payouts and their
                           void, billable bookings, KPIs, case cost vs billed)
  app/agency_billing/      AA-1037 R4 (bk-041 / bk-042): config.py (billing entity, tax rules, payment terms), invoices.py
                           (list, detail, draft lines, issue with the gap-free AAS- / AAT- number + snapshots, void,
                           e-invoice no), payments.py (record = maker, approve / reject = checker), drafts.py
                           (bk_saas_invoice_draft, bk_settlement_draft, DraftSchedule), cis.py (catalog.v_tenant_billing_v1);
                           AA-1037 extras (bk-044): reminders.py (bk_agency_invoice_reminders + ReminderSchedule, G4),
                           export.py (accountant CSV, G5), clauses.py (partner terms clauses, G6)
  app/routers/agency_billing.py  /booking/agency-invoices*, /booking/agency-payments*, /booking/agency-billing/status|runs,
                           /booking/billing-entities*, /booking/tax-rules*, /booking/billing-terms* (agency_billing.read /
                           .write, agency_payment.approve, billing_config.write = admin, finance);
                           /booking/agency-billing/export.csv, /booking/partner-terms-clauses* (terms_clause.approve = admin)
  app/routers/supplier_bookings.py  /booking/trip-cases/{ref}/supplier-bookings (GET, POST, …/generate),
                           /booking/supplier-bookings (list, summary, GET/PATCH /{id}, …/request|confirm|decline|cancel|
                           rerequest), /booking/ops-files (POST, /{id}/download, /{id}/remove) — ops.read / ops.write
  app/routers/trip_checks.py  /booking/trip-checks (GET, PATCH /{id}), /booking/trip-cases/{ref}/checks (GET, POST),
                           /booking/case-lifecycle/runs (GET) — ops.read / ops.write (AA-682 S2)
  app/trip_cases/          trip case service (list, summary, detail, activity, edit, status, assign), versions (items
                           upsert by id, approve, soft delete, cross-case list), sql.py fragments
  app/b2c.py               the retired B2C half (AA-1046): the 410 guards on b2c cases, the retired job kinds
  app/notifications/       staff feed + notify helpers (service.py, rows)
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
                           metrics.py (backlog sampler, AA-751) + ops_checks.py (S263 in-app job checks -> alerts
                           SNS topic, once per check per UTC day; ledger kind bk_ops_alert)
  app/retention/           AA-825 retention kinds bk_retention_job / bk_retention_inbox_redact: policy (windows),
                           redact (inbox payload PII), archive (S3 JSONL.gz), runner (batched count/delete/redact),
                           jobs (modes, handlers, daily schedule hook, admin status)
  app/routers/retention.py /booking/retention (status), POST /booking/retention/runs (DRY RUN default),
                           GET /booking/retention/runs/{job_id} — retention.admin (admin only)
  app/inquiry/             leg catalog tour key resolve / back-fill (leg_keys.py, `python -m app.inquiry.leg_keys`) of
                           the kept b2c cases; the intake and its contract parser are retired (AA-1046)
  app/pricing/             ADR 0005 pricing: pure Decimal engine (engine.py) + rule/FX reads (repository.py, approved
                           rows only) + rules/FX admin with maker-checker (policy.py, bk-007)
  app/routers/catalog.py   GET /booking/catalog/destinations (place picker, any active staff, AA-822);
                           GET /booking/catalog/trip-plans (+ /facets, /{source_id}), PUT …/{source_id}/commercial,
                           POST …/{source_id}/trip-cases (AA-679, bk-009); GET|PUT …/{source_id}/components (AA-972);
                           GET|POST …/{source_id}/departures, PATCH /booking/catalog/departures/{id} (AA-973);
                           POST …/{source_id}/departures:generate, GET|POST /booking/catalog/departures/{id}/allotments,
                           POST /booking/catalog/allotments/{id}/approve|end (AA-1073; b2b_desk.read / .write)
                           GET /booking/catalog/geo/destinations[/{id}], GET /booking/catalog/geo/countries[/{iso}]
                           (catalog.read), PATCH of both (catalog.admin) (AA-996, bk-029)
                           GET /booking/catalog/price-readiness?status=&country=&q=&format=json|csv (pricing.read,
                           AA-1029)
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
                           replaces the set (audited), no overlaps per channel + currency; AA-1029: computed rows,
                           the b2b RRP, store() shared with the cost sheet, from price = b2b RRP for 2 pax
  app/catalog/trip_plan_sync.py  AA-1029: bk_trip_plan_sync (catalog.v_tour keys -> a plan per tour, the CIS reference
                           price text when catalog.v_tour_price_reference_v1 is readable; AA-1091: the structured
                           reference of catalog.v_tour_price_reference_v2 when readable) + SyncSchedule
  app/catalog/cost_sheet.py  AA-1029: bk_price_recompute (components x rates in force -> computed matrix rows per
                           window x pax band, priced by the rules in force, hand rows kept) + RecomputeSchedule
  app/catalog/price_history.py  AA-1029 (bk-039): dated price history (close open rows, insert the new set; as_of)
  app/catalog/price_readiness.py  AA-1029: catalog-wide price readiness (one status per plan: priced, hand_priced, or the
                           cost sheet's reason: missing_rates / missing_fx / no_rule / no_components /
                           inactive_component; components without a published rate in the next 365 days by code, never
                           a net; summary per status and country; staff CSV) + the rate-sheet dry-run impact
  app/supply/rate_sheet.py AA-1029: supplier rate-sheet Excel import (openpyxl, read-only) into a draft contract
                           (the dry run adds `impact`: the plans that use the sheet's items, priced once it is published)
  app/catalog/liveness.py  AA-986 L5: served state of a catalog key (served | retired | paused | not_in_catalog) from
                           v_tour + v_tour_retired, feature detection of the CIS 274 views, strict / soft reads
  app/catalog/lifecycle.py AA-986 L5: bk_tour_lifecycle_pull (outbox → inbox → staff notifications) + PullSchedule
  app/b2b/tenant_pull.py   AA-1028: bk_tenant_pull (catalog.v_tenant_account_v1 → booking.b2b_partners) + TenantPullSchedule
  app/b2b/served.py        AA-986 L5: 409 tour_not_served for a tp: line of a tour that is not served
  app/b2b/confirmation.py  AA-1037 G2: the booking confirmation (JSON / printable HTML; no traveller names, no prices)
  app/b2b/terms.py         AA-1037 G6: GET /booking/b2b/v1/terms (payment terms, credit status, policy, approved clauses)
  app/b2b/invoices.py      AA-1037 (1.5): the agency's own AA invoices + billing status (partner-scoped, field allow-list)
  app/routers/pricing.py   POST /booking/pricing/quote-preview (pricing.read); GET/POST /booking/pricing/rules,
                           /booking/pricing/fx-rates and /booking/pricing/cancellation-policies (AA-974), POST
                           …/{id}/approve (reads pricing.read, writes pricing.write)
  app/routers/agency_pricing.py  AA-1030: /booking/pricing/partner-tiers (GET, POST, PATCH /{code}),
                           /partner-tier-discounts and /partner-rate-cards (GET, POST, POST /{id}/approve),
                           /partner-terms (GET, PATCH /{partner_id}: the partner's tier); pricing.read / pricing.write
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
                           presigned previews, usage_rights / removal; the hotel list's photo count + first photo,
                           one grouped read per page, AA-994 S6d) and the CIS content read-back (content.py)
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
.github/workflows/         ci.yml (5 required checks + non-required Frontend Build, Vendored CIS copies and Migration
                           preflight, AA-993 M4b: expand / contract class, number = main max + 1, no edit of a copied
                           file, AA-1024 l22: no added line naming a migration by an old number after a renumber,
                           .github/scripts/migration_refs.py), deploy-dev.yml (its Schema gate job waits until /booking/health/ready lists every
                           bk- key the commit needs), migration-sync-wake.yml (after a push to main touching
                           migrations/, rings the AA-CIS-Infra Migration Sync doorbell),
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
`B2C_INTAKE_ENABLED` is retired with the B2C half (AA-1046); a value left in the task definition is ignored.
Cognito (AA-680, all optional and **not set on Dev**, defaults are code constants in `app/auth/identity_config.py`):
`COGNITO_WORKFORCE_ISSUER`, `COGNITO_BOOKING_STAFF_CLIENT_ID`, `COGNITO_BREAKGLASS_CLIENT_ID` (same names as CIS).
E-mail (AA-811, all optional): `STAFF_NOTIFY_EMAIL` (staff inbox for the Dev copies of the agency invoice reminders;
empty = no e-mail outside prod),
`SES_FROM_ADDRESS` (must equal the Infra `ses_from_address`, the only From the task role may use; empty = no e-mail),
`SES_CONFIGURATION_SET` (Infra `aa-email-<env>-transactional`), `SES_REGION` (default `us-west-1`), `STAFF_ADMIN_URL`
(links in staff e-mail; default `https://booking.lumiguides.it.com`).
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
Signed-in travellers: `COGNITO_EXTERNAL_ISSUER`, `COGNITO_TRIPPLANNER_CLIENT_ID`, `COGNITO_BOOKING_CUSTOMER_CLIENT_ID`
are retired with the intake and the customer routes (AA-1046); old values are ignored.
Error tracking (AA-871, optional): `SENTRY_DSN` (Sentry off when empty; never logged) and `SENTRY_REQUIRED` (`true` =
start-up fails without a DSN, the missing-DSN guard; off until Infra wires the DSN secret).

## Releases (AA-891)

One version for the whole repo (API image and staff admin), kept by release-please in manifest mode
(`release-please-config.json`, `.release-please-manifest.json`; `CHANGELOG.md`; tags `vX.Y.Z`; pre-1.0 a `feat` or a
breaking change bumps the minor, a `fix` the patch). `.github/workflows/release-please.yml` keeps one release PR open on
`main`, opened with a token of the GitHub App `aa-ecosys-ci-bot` (environment `release`) so the required checks run on
it; merging it tags the release. The version also lives in `backend/pyproject.toml` (an `extra-files` entry, bumped by the
same PR). A release commit deploys nothing (Deploy Dev ignores those files, Deploy Admin Dev watches `frontend/**` only);
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
`accounts/aa365/ses.tf`; the task role may send only as `ses_from_address`, IAM `ses:FromAddress`). The staff "new
inquiry" e-mail (`bk_notify_email`) is retired with the B2C intake (AA-1046). **Agency invoice reminders** (AA-1037 G4,
`bk_agency_invoice_reminders`, daily, `app/agency_billing/reminders.py`): an overdue issued agency invoice gets one
e-mail per step it reaches (setting `agency_reminder_steps_days`, default 1 / 7 / 14 days overdue, plus the credit-stop
step at the agency's `suspend_after_days`; only the highest step reached), to the bill-to billing e-mail of the
invoice, recorded once per (invoice, step) in `booking.agency_invoice_reminder` (bk-044). Outside `prod` every
reminder goes to `STAFF_NOTIFY_EMAIL` instead (the agency address named in the body). Bounce/complaint suppression in Booking
tables is not built yet (the SES configuration set's account suppression list applies).

## The B2C part is retired (AA-1046; AA-1034 R0 and AA-1037 before it; root ADR 0019 d8)

Adventure Asia sells to partner agencies only. The B2C code is gone (AA-1046 part 1): the customers (`/booking/customers`,
traveller sign-in), the customer invoices and payments (`/booking/invoices`, `/booking/payments`,
`/booking/billing/summary`, `/booking/trip-cases/{ref}/billing`), My Payments (`/booking/customer/v1`, contract
`booking.customer.v1`, retired with the TripPlanner My Payments section), Send to the customer
(`…/versions/{n}/send`), the TripPlanner inquiry intake (`bk_inquiry_pull` / `bk_inquiry_process`) and its staff
e-mail (`bk_notify_email`), the customer payment reminders, customer payment evidence files, and the settings
`B2C_INTAKE_ENABLED`, `COGNITO_EXTERNAL_ISSUER`, `COGNITO_TRIPPLANNER_CLIENT_ID`, `COGNITO_BOOKING_CUSTOMER_CLIENT_ID`
(an old value in the task definition is ignored). B2B cases come only from the B2B confirm path
(`POST /booking/b2b/v1/holds/{id}/confirm`).

- **The tables go next**: the B2C drop file (planned as bk-045; bk-045 went to the finance file first, so the drop takes
  the next free bk- number) (`-- migrate: destructive`, DRY RUN + tech-lead approval, root
  `docs/audits/2026-11-05-S270-aa1037-b2c-drop-dryrun/`) drops the customer and customer money tables, archives the
  money rows whole into the owner-only `booking.retired_b2c_record` (10 years, DM-7.1 accounting exception), drops
  `trip_cases.customer_id`, and lets a new trip case in only as b2b. This build reads none of the dropped objects, so
  it runs on either side of the drop.
- **b2c trip cases are kept history, read-only**: every staff write on a `channel = 'b2c'` case answers 410
  `{"detail": "b2b_only", ...}` before any write (`app.b2c.refuse_b2c_case_write` and its siblings read only the
  case's channel; each route lists the 410 in `docs/api/openapi.json`): `PATCH /trip-cases/{ref}`, `POST .../status`,
  `.../assign`, the proposal versions (`POST .../versions`, `PATCH` / `DELETE .../versions/{n}`, `PUT .../items`,
  `POST .../price`, `.../approve`), a new version from a trip plan, supplier bookings (`generate`, create, `PATCH`,
  `request`, `confirm`, `decline`, `cancel`, `rerequest`), trip checks and operations files (customer payment
  evidence, or a b2c case's supplier booking). A staff-made case (`POST /booking/trip-cases`, a new case from a trip
  plan) was always a B2C case: 410 too (`app.b2c.refuse_new_case`).
- **Reads stay**: the trip case list, detail, versions, checks and supplier bills of the kept b2c cases (no customer
  columns any more: a b2c case names no traveller; the staff admin labels it "Traveller (B2C history)").
- **Stays open**: supplier bills and payouts (AA's own payables, even for a past B2C trip); the B2B API and desk;
  catalog, supply, pricing, agency billing.
- **Jobs**: the retired kinds (`app.b2c.RETIRED_KINDS`) have no handler; the worker cancels a leftover queued row.
  `bk_case_lifecycle` moves only B2B cases and `bk_ops_reminders` names only B2B cases (each result says
  `"b2c": "frozen"`). Retention keeps running on every row: the DM-7 clearing of trip-check emergency contacts,
  `bk_retention_inbox_redact` on the kept inquiry inbox rows (DM-6.6), and the DM-6.7 traveller erasure, a reviewed DB
  Migrate `run-script` (root `docs/legal/dsar-erasure-runbook.md`).
- **DB backstop**: the drop file swaps `trip_cases_channel_chk` for "b2b, or b2c created before the drop" (the kept rows stay
  valid and redactable). A strict `CHECK (channel = 'b2b')` would refuse every UPDATE of a kept b2c row, the erasure
  redaction included (even `NOT VALID`), so it is not added while b2c rows exist.
- **Staff admin**: `lib/config.ts` `B2C_INTAKE_OPEN` is `false` for good; `ui/booking/b2cFrozen.ts` takes the
  B2C-only permissions out of the staff profile and, inside a b2c case, the case, version and operations writes; a b2c
  case shows a "Read-only" pill. The Customers pages, the customer invoice and payment pages, the case Billing tab and
  money badge, Send to customer and `/cases/new` are gone; `/billing` is the supplier bills. Source guards:
  `ui/booking/b2cOff.test.mjs`, `ui/booking/b2cFrozen.test.mjs`.

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
  - **BRIDGE** (retired, AA-911 S271): `BRIDGE_ENABLED = False`, production accepts WORKFORCE only and a bridge
    token is 401. The bridge profile (`iss` = `STAFF_JWT_ISSUER`, `aud` = `booking`, role in `cis_role`) and its
    provisioning code stay only for the test issuer of `tests/staff_tokens.py` (`include_bridge=True`) until the
    endpoint tests move to WORKFORCE tokens; then they are deleted.
- **WORKFORCE identity (bk-016).** The Booking role is the first `booking-*` group in the order `booking-admin` →
  `admin`, `booking-sales` → `sales`, `booking-contracting` → `contracting`, `booking-content_creator` →
  `content_creator`, `booking-finance` → `finance` (AA-1035 R2, root ADR 0019 d3 / d9; bk-038 lets the row hold it),
  re-read on **every** request (a change is written and audited `staff.role_synced`); no
  `booking-*` group = 403 `no_booking_role`, nothing written. `cis_role` = the highest `cis-*` group (informational;
  the R2 CIS groups included, `cis-reviewer` read as `content`). **Finance** issues invoices and records payments
  (`billing.read`, `billing.write`; verifying a payment stays `admin`, design D6) and sees nothing else; it comes only
  from the group, which the CIS Staff page grants through `shared.staff_app_grant` role `finance` (CIS 363), never
  from the bridge (`ROLE_FROM_CIS` has no finance entry; CIS mints no bridge token for its R2 roles).
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
  cancellation-fee policies (AA-974) are pricing data: `pricing.read` / `pricing.write`, maker-checker like rules;
  agency price tiers, tier discounts and partner rate cards (AA-1030) the same pair and maker-checker, and moving a
  partner to another tier is `pricing.write` (audited, no second person: it changes no rate).
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
  Agency billing (AA-1037, bk-041 / bk-042): `agency_billing.read` / `agency_billing.write` / `agency_payment.approve` /
  `billing_config.write` = admin, finance; approving or rejecting a payment is never its recorder's (403 `maker_checker`,
  CHECK in the database). None of them is in the preview cap. `terms_clause.approve` = admin (AA-1037 G6, bk-044): a
  partner terms clause reaches agencies once approved, never by its last editor (403 `maker_checker`, CHECK).
  Agency booking operations (AA-1075, bk-047): entering a booking for an agency, cancelling or reducing one for it
  and asking for a lower cancellation fee `b2b_desk.book` = admin, sales; a price below the floor margin and a lower
  cancellation fee are approved with `b2b_desk.approve` = admin, contracting, never by the person who asked (403
  `maker_checker` + CHECKs). The departure manifest is `ops.read`; a tour's deposit terms are read with
  `agency_billing.read` and written with `billing_config.write`; a supplier's cancellation fee needs `ops.write` +
  `pricing.read`; the age bands of a plan `trip_plan.write`. None of the writes is in the preview cap.
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
  rates in force on a date, plain), `GET /price-parity` (the public "from" price per person: since AA-1029 the
  suggested retail price of the matrix, `booking.v_from_prices`, else the bk-009 per-person price hint; warn-only;
  `null` without either or a rate). **AA-973**: `GET /departures`, `POST /holds`,
  `GET` / `DELETE /holds/{id}`, `POST /holds/{id}/confirm` (below). **AA-974**: VND quotes, `POST /holds/{id}/extend`,
  the re-hold limit on `POST /holds`, `POST /bookings/{id}/cancel` (+ `?dry_run=true`) (below). **AA-975**:
  `GET /events` (below). **AA-1030 G3 (contract 1.4)**: `GET /tariff` (scope `read`; `?currency=`, `?source_id=`,
  `?format=csv`): the calling agency's own tariff (see "Agency price tiers").
- **Agency invoices (AA-1037, contract 1.5; root ADR 0019 d17, the data of the CRM "Bookings with AA" page)**
  (`app.b2b.invoices` over `app.agency_billing.invoices`, scope `read`): `GET /invoices` (`?kind=saas|settlement`,
  `?status=issued|paid|void`, `?limit` 1-100, `?offset`; newest first, no lines), `GET /invoices/{number}` (lines with
  `hold_id` / `booking_id`, the approved payments as date + amount, the issuer snapshot with the payment instructions)
  and `GET /billing-status` (the caller's `v_tenant_billing_status` row as `/terms` `credit` + `paid_through`). Every
  read is filtered in SQL by the `tenant_id` of the partner `app.b2b.access` resolved (never by a caller field); a
  draft is never listed or answered; a draft, another agency's number or anything that is not an `AAS-` / `AAT-` number
  is the same 404 `invoice_not_found`. Field allow-list: the functions copy contract fields key by key and the response
  models forbid extras, so the staff note, the void reason, every `*_by`, the payment bank `reference` / `document_ref`
  / note, the amount waiting for approval and the `bill_to` snapshot (the agency's billing contact, personal data the
  contract never carries) stay in Booking. `period_end` is the last day covered (the stored one is exclusive). Answers
  are `Cache-Control: no-store`. Consumer: the AA-Partner CRM page (not built in this repo).
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
- **Departure board, ops sheet, fees, price adjustments (AA-1073 rest, bk-048, contract minor 1.8)**: the staff page
  `/departures` (b2b_desk.read) reads `booking.v_departure_occupancy` (held, sold, allotted, unused allotment, free,
  occupancy %, minimum, at risk, open incident, agencies; counted at read time like `departures.py`) through `GET
  /booking/b2b-desk/departure-board` (month window, country / trip plan / status / at-risk filters, paged; the filter
  options come from every plan of the window) and `…/{id}` (seats by agency with their cases, ops sheet, fees, price
  adjustments). **Ops sheet**: `departures.meeting_point` / `meeting_time` (sent to the agencies: event
  `departure.updated`, `Departure.meeting_point` / `meeting_time` in `GET /departures`) and `ops_note` (staff only);
  `departure_staff` (guide / tour leader / driver: a staff member or a supplier; never sent, no names stored); writes
  `ops.write`. **Departure fees** (`departure_fees`): proposed with `b2b_desk.write`, approved or rejected by another
  person with `b2b_desk.approve` (403 `maker_checker`, CHECK); approval emits `departure.fee_charged` (`Event.fee`: id,
  amount, currency) and `bk_settlement_draft` bills it once as a `departure_fee` line (`agency_invoice_line.source_fee_id`);
  the agency invoice API shows that line as `adjustment` until the CRM reads the new role. The draft terms clause
  `departure_fees` reaches `GET /terms` once the business owner approves it. **Price adjustments**
  (`departure_price_adjustments`, `pricing.write` maker and a different checker): one in force per departure, a
  fraction or an amount per traveller; the quote engine applies it to `tp:` lines on that departure after the agency
  discount, and a b2b rule with `early_days` / `early_discount_pts` takes the points off the margin when the quote day
  is on or before `start - early_days`; the line's `price_note` records what applied (and `below_floor`: still sold).
- **Minimum, cutoff, decision date, allotments (AA-1073, bk-046, contract minor 1.6)**: a departure has
  `min_to_operate` (0 = no minimum: status by hand), `sales_cutoff_days` (sales close on `start_date - cutoff`, that day
  included; a line then is `sold_out`, "sales closed on …"; 0 = the start day, the old rule) and
  `decision_days_before` (21). The daily job `bk_departure_watch` moves a scheduled departure with sold >= minimum >= 1
  to `guaranteed` (event `departure.guaranteed`), and on the decision date stamps a scheduled one still short of it at
  risk (`at_risk_at`, event `departure.at_risk` to the agencies on it, one staff notification `departure.at_risk`);
  staff decide on the trip plan's Departures tab (run anyway = guaranteed, or cancel). Sold seats only, never held.
  **Allotments** (`booking.departure_allotments`, `app/b2b/allotments.py`): seats of one departure kept for one agency
  until `start_date - release_days_before`; proposed by one staff member, approved by another (`b2b_desk.write`, 403
  `maker_checker`), refused when the general free seats would go below zero (409 `allotment_exceeds_free`); a change is
  a new row replacing the one in force on approval; pending → rejected, in force → withdrawn, a cancelled departure
  ends them. **Seat count** (counted under the departure lock, never stored): an agency's unused allotment (seats minus
  its own held + sold) is kept from every other agency, so an agency can take `capacity - held - sold - unused
  allotments of the others` (its own holds draw on its allotment first); a capacity edit is refused below held + sold +
  unused allotments. `bk_allotment_release` stamps `released_at` on the release date (event `allotment.released` to that
  agency). `GET /departures` 1.6 adds `min_to_operate`, `seats_to_guarantee` (null without a minimum), `last_sale_date`
  and the caller's own `allotment` (`seats`, `used`, `available`, `release_date`); `available` is the caller's.
  The recurring generator `POST …/{source_id}/departures:generate` (`trip_plan.write`): weekly (ISO weekdays) or
  monthly (a day; a month without it is skipped), at most 400 days and 400 dates, `dry_run` preview, dates the plan
  already has are skipped (`ON CONFLICT DO NOTHING`), one audit row per run.
- **Holds (AA-973)**: `POST /holds` (scope `hold`, `Idempotency-Key` required) holds the lines of one quote for one
  proposal version: one `active` or `confirmed` set per `(partner, proposal_ref, proposal_version)` (the same quote
  again returns that hold; another quote is 422 `invalid` until it is released); the same key for another request is
  422; a hash that is not the quote's, or a quote past `valid_until`, is 409 `price_changed` (no re-pricing within
  validity); a line that does not fit is 422 `sold_out` and nothing is held. Length = setting `b2b_hold_hours` (48),
  capped by the earliest departure start. An `active` hold past `expires_at` reads `expired` at once; the job
  `bk_b2b_hold_expire` writes the status. `DELETE` releases (released / expired: no-op; confirmed: 422). Error codes
  follow the contract: `sold_out` 422, `hold_expired` 410. **Contract 1.2 (AA-952)**: every `Hold` answer (place,
  read, extend) also carries the quote it holds: `price_hash` (what the confirm presents), `currency`, `fx` (the
  quote's lock, as `POST /quotes` answered it) and each line's `b2b_price`; the CRM writes the agency's AA lines from
  them after its client accepts and confirms with that hash after the deposit. Additive and optional in the response
  model; never a net cost or a supplier rate.
- **Confirm (AA-973, memo seam 1)**: `POST /holds/{id}/confirm` (scope `book`) is the only B2B path that creates a
  commercial record: one transaction opens a **B2B trip case** (`channel = 'b2b'`, `customer_id` NULL,
  `party_tenant_id` = the agency, `source_system = 'crm'`, `source_id` = the proposal ref, status `confirmed` inserted
  directly, `idempotency_key = b2b_hold:<hold id>`), **one approved version** (V1, channel `b2b`, the quote's total,
  currency and FX lock) with one item and one cost row per line (net from the quote; AA-1013: the item title is what
  was sold, the CIS tour title of a trip plan line from `catalog.v_tour`, "hotel · room type" or the service title,
  falling back to the kind; the version title is the first trip plan line's tour; the agency proposal ref / version
  and each line ref go to the version / item notes), sets the hold `confirmed`, then
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
  `departure.incident` / `departure.incident_cleared` (+ `incident_code`); 1.6 (AA-1073): `departure.at_risk` (fan-out
  like `departure.guaranteed`) and `allotment.released` (the one agency whose allotment went back to sale); 1.8 (AA-1073
  rest): `departure.updated` (fan-out; the meeting point or time changed) and `departure.fee_charged` (the one agency,
  with `fee`: id, amount, currency). Ids and codes only (no payload, no free
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
- **Contract minor 1.3 (AA-1037 R4 extras, additive; root research note 2026-11-05 Asian Trails benchmark G1 / G2 / G4 /
  G6)**:
  - **G1 policy on answers**: every `Quote` and `Hold` answer carries `cancellation_policy` `{policy_id, basis, tiers}`:
    `in_force_today` = the policy a confirm today would lock (the plan of the first `tp:` line, else the B2B default),
    `locked` = the policy locked on a confirmed hold. Not part of the price hash; null without an approved policy.
  - **G2 booking confirmation**: `GET /bookings/{booking_id}/confirmation` (scope `read`, the agency's own bookings;
    `format=json` | `html`) and staff `GET /booking/b2b-desk/bookings/{ref}/confirmation` (`b2b_desk.read`, case id or
    code, HTML by default): reference, agency proposal ref / version, status (`confirmed` | `cancelled`), dates, the
    most travellers of a line by age band, each line (title, kind, date, nights, pax, rooms, status, supplier
    confirmation refs of the confirmed supplier bookings covering it) and the locked policy. No traveller names, no
    prices. HTML = one self-contained file (inline CSS, no script, every value escaped, CSP `default-src 'none'`),
    downloaded as an attachment; Booking has no PDF renderer, so the page prints to PDF from the browser. Staff:
    "Confirmation" on a B2B trip case.
  - **Trip length (AA-1088, S278 audit M13)**: a `tp:` line covers the tour's days = the larger of the CIS catalog
    `duration_days` (`catalog.v_tour`) and the plan's highest component `day_no` (`app/b2b/plan_span.py`; a catalog
    read error falls back to the components). The confirm stores the case end from it (was: the highest priced day,
    so a 6-day tour priced on day 1 stored end = start) and the tour item's `day_to`; the confirmation and the
    vouchers show the later of the stored end and that span, so a case stored before AA-1088 prints the whole tour.
    A B2B case has no TripPlanner legs: its `trip_title` (case detail, case list, proposals list) is its first
    version's title, the tour sold (`trip_cases/sql.py` `B2B_HEADLINE`), never "Custom trip".
  - **G4 credit stop**: `POST /holds` is 409 `credit_hold_overdue` while the agency's `booking.v_tenant_billing_status`
    is `past_due` (oldest overdue agency invoice past `suspend_after_days`); replays, confirms, extends and cancels of
    existing holds still work; the stop lifts once the overdue invoices are paid (payment approved).
  - **G6 terms**: `GET /terms` (scope `read`): the agency's payment terms (deposit %, balance days, settlement due
    days), credit status (`holds_paused`), the default cancellation policy in force and the **approved** partner terms
    clauses (bk-044 `partner_terms_clause`, e.g. `price_error` with its free-cancel window). Drafts never reach it.

## Tour price chain (AA-1029; root ADR 0005 d3 / d6 / d7, ADR 0019 b15)

Loading supplier rates makes prices flow with no hand step after the plan's components are set:

1. **Bridge**: `bk_trip_plan_sync` gives every `catalog.v_tour` key a trip plan (not sellable: selling stays a staff
   decision) and keeps the CIS source price text as a reviewed **reference** (never a price).
2. **Rates**: a supplier rate sheet (`GET /booking/rate-sheets/template`, `POST /booking/suppliers/{ref}/rate-sheets`
   with the .xlsx as base64, `?dry_run=true` to check) becomes a draft contract with its rates (`create_contract` /
   `create_rate`, so the same checks and audit); a second person publishes it (maker-checker). Items are matched to
   the supplier's existing hotels (code + room type name) and services (code); every problem is reported by sheet, row
   and column and nothing is written while one is left.
3. **Cost sheet** (`app/catalog/cost_sheet.py`): net per person per start-date window x pax band (1, 2, 3–4, 5–6,
   7–10, 11+; the band's smallest party) from the plan's components (rooms: doubles + a single when odd; services per
   unit) and the rates in force (the B2B quote's choice), windows cut at rate validity edges shifted by the day offset,
   anchored on the rate validity (never on today: the same inputs the next day write nothing; windows already over are
   left out) and merged when equal; priced by the engine per channel with a rule in force (`b2b`, `fixed_departure`;
   USD); `peak` when a rate's season says "peak". Computed rows replace the computed rows; hand rows and overrides are
   kept; a computed row that overlaps a hand row is left out (or starts after one already over); at most 200 rows
   (latest windows dropped first). The job covers every plan with a component, a computed row or a b2b row, so hand
   priced plans get their RRP too.
4. **RRP**: every b2b row carries `rrp_amount` = round up(amount x (1 + `rrp_markup_pct` of the b2b rule in force)),
   the suggested retail price (ADR 0019 b15: net + 25 %). Agency tier discounts: see "Agency price tiers" below.
5. **From price**: `booking.v_from_prices` (price.from.v1) and every staff / parity read: the cheapest RRP for 2
   travellers still valid, USD first.
6. **History**: every matrix change (staff or cost sheet) closes the open rows of `trip_plan_price_history` and inserts
   the new set (`GET …/prices/history?at=`).

Staff: the commercial drawer shows the from price, the reference (Mark reviewed) and "Recompute from rates"
(`POST …/prices/recompute`); cost sheet rows carry a badge; the b2b rule takes the RRP markup; suppliers have
"Import rate sheet".

**Price readiness** (`app/catalog/price_readiness.py`, `GET /booking/catalog/price-readiness`, `pricing.read`; staff
page Pricing → Price readiness, `/pricing/readiness`): one status per trip plan so staff see which tours price and what
the others still need. `priced` = computed rows still valid; else `hand_priced` = hand or overridden rows still valid
(an expired hand row counts for nothing); else the cost sheet's own reason in its order (`cost_sheet.blocking_reason`:
`no_components`, `inactive_component`, `no_rule`), then its `no_rates` split into `missing_rates` (a component without
a published rate on any day from today to +365, listed by HTL- / SVC- code, room type and supplier code) and
`missing_fx` (every rate of a component in a currency without an approved FX rate today). A plan with no reason and no
computed row yet is decided by `cost_sheet.compute` itself (no write, at most 50 a read): rows = `priced` +
`pending_recompute`, none = `missing_rates` with a note. One plan query (counts and `v_from_prices` joined; the CIS
catalog join fails soft), then components and rate spans (`cost_sheet.SPANS_SQL`) in batches: no N+1. The summary
counts every plan per status and per country; filters `status`, `country`, `q`; `?format=csv` is the staff file
(`price_readiness.CSV_COLUMNS`, formula-safe, never a net or margin; the from price is the public RRP). The rate-sheet
dry run (`?dry_run=true`) adds `impact`: the plans whose components use the sheet's room types or services, for each
whether every component would have a rate once the draft is published, its status after, and `would_move_to_priced`
(the import drawer lists them under the Check result). Read only; the publish path is unchanged.

**Price setup workbench (AA-1091)** — everything the customer-service team needs to make tours sellable, on one
staff page (Pricing → Price setup, `/pricing/workbench`; admin, sales, contracting read, admin and contracting edit).
No migration: it reads and writes the existing bk-009 / bk-019 / bk-020 / bk-032 / bk-039 tables through the single
forms' services.

- **Workbench** (`app/catalog/price_workbench.py`, `GET /booking/catalog/price-workbench`, `pricing.read`): the price
  readiness rows (the same statuses and reasons, never a second classifier) plus per plan its upcoming departures,
  its b2b rows still valid, the CIS reference and the price hint; seven checks (components, rates, fx, rule, price,
  sellable, departures; departures are optional for tailor-made tours), the **next step** (`add_components`,
  `fix_inactive`, `load_rates`, `approve_fx`, `approve_rule`, `recompute`, `make_sellable`, `done`; hand prices alone
  are never `done`: an agency quote prices the components), counts per step and per missing check, filters `status`,
  `country`, `q`, `missing`, `next_step`. **Bulk actions** (`POST …/price-workbench/actions`, `trip_plan.write`, at most
  200 plans, 50 for `recompute`, one transaction, one audit row per changed plan; a plan that does not qualify is
  `skipped` with its reason): `make_sellable` (only with components and in the catalog), `make_not_sellable`,
  `apply_provisional`, `clear_provisional`, `recompute`.
- **Provisional price** (item 4): `apply_provisional` copies the reviewed CIS reference (`reference_amount` /
  `_currency`) into the **price hint** (`price_hint_*`, per person) of a plan with no b2b row still valid and no hint
  typed by staff, and marks the reference reviewed. The hint is a display price only: the public price parity read
  (`app.b2b.reads.price_parity`, the RRP agencies must not undercut) falls back to it while no b2b row exists; quotes,
  holds, the agency tariff and `booking.v_from_prices` (TripPlanner "From $X") never read it. The workbench flags it
  "Provisional: no supplier price yet". `clear_provisional` clears only a hint equal to the reference.
- **Structured reference** (`app/catalog/trip_plan_sync.py`, `STRUCTURED_VIEW`): when the CIS contract view
  `catalog.v_tour_price_reference_v2` (CIS 402: one row per live tour, `price_raw` plus `reference_amount` /
  `_currency` / `_basis` / `_status`; feature-detected) is readable, the sync reads it instead of v1. A value with
  status `confirmed` or `parsed` is taken as it is (basis `per_person` or `from` = the per-person reference;
  `per_group` = no per-person amount, the text only); `needs_review` is not trusted and the parse of `price_raw`
  decides. An amount or currency change alone clears the review. The run reports `references_source`
  (`structured` | `text`) and `references_structured`. Without v2 the sync parses the v1 text as before.
- **Components suggested from the CIS itinerary** (`app/catalog/component_suggest.py`,
  `GET …/trip-plans/{source_id}/component-suggestions`, `supply.read`): rules only, no LLM. Reads `catalog.v_tour` by
  key, `catalog.v_tour_day` **by `tour_id` only** (perf guard) and `catalog.v_tour_day_route` by `tour_id`
  (feature-detected: the night's place and CIS destination; without it the day's last place), and Booking's active
  hotels / room types / services of the tour's country (the item's, else its supplier's) or of its overnight
  destinations, plus services made from the tour in CIS. A hotel per night and a service per type the day's text names
  (transfer, guide, entrance, activity; a meal for lunch or dinner, never breakfast), each with an explained score
  (named in the text, name words, the night's CIS destination, the place in its name or address, the country; for a
  service: made from this tour, title words, a day's destination, the country), the best as `suggestion`, up to 3
  `alternatives`, the double / twin room type by default with every room type to change to; a need a component of
  that day already meets is `covered`, one with no candidate `unmatched` (the UI links to create the supplier, hotel
  or service). Nothing is applied: staff accept, change or reject each suggestion in the staff drawer, which saves the
  whole set with `PUT …/components`.
- **Bulk workbook** (`app/catalog/price_import.py`, `GET /booking/catalog/price-import/template` (`pricing.read`,
  empty with `blank=true`, else prefilled with the workbench's filtered plans, at most 300) and
  `POST /booking/catalog/price-import` (`trip_plan.write`, the .xlsx as base64 JSON like rate sheets, `?dry_run=true`
  checks only)): sheets `Tours` (Sellable), `Components` (a listed tour gets exactly these: Hotel code + Room type, or
  Service code, per day), `Prices` (a listed tour gets exactly these hand prices per channel, season, dates and pax
  band, with child / infant / single supplement; rows priced from a cost (computed, or typed with a net cost on the
  single form: their net cost, margin and override are not in the file, so the export leaves them out) are kept unless
  a hand row overlaps them, and the dry run counts those it replaces: `computed_rows_replaced`, `net_rows_replaced`),
  `Departures` (new dates only; an existing date is skipped). Every problem by sheet, row and column, nothing written
  while one is left; the dry run lists per tour what changes and its readiness status after. The import writes through
  `plan_components.replace_components`, `trip_plan_prices.store` (audit `trip_plan.prices_imported`),
  `departures.create_departure` and `price_workbench.set_sellable`, adds one `trip_plan.bulk_imported` audit row and
  recomputes the cost sheet of up to 50 touched plans in the same transaction. Idempotent: the same file again writes
  nothing. Maker-checker: the single forms it replaces have none (supplier contracts and pricing rules do, and stay
  in the rate sheet import and the rules page). The staff BFF allows the 2 MB upload on `catalog/price-import`.
  Formula-safe export: a prefilled text cell (CIS title or country, room type name, season) that starts like a formula
  (`tariff.FORMULA_START`) is written as a text cell with Excel's quote prefix, the value unchanged (the round trip
  still matches room types and seasons).
- **Staff UI**: `/pricing/workbench` (KPIs, the checks per tour, next step links, row selection for the bulk actions,
  the import drawer with Check then Import, the hotels & services drawer) and the trip plan's "Hotels & services" tab
  (the same drawer). The page is the first Booking page with the kit language switch: English by default,
  Vietnamese on the page's own switch (cookie `bk_lang`, `ui/local/lang.ts`; its text in
  `app/(staff)/pricing/workbench/_parts/messages.ts`); other pages stay English until they get their messages, so no
  page mixes the two.

## Agency price tiers (AA-1030, bk-043; root ADR 0019 b15)

Every B2B partner has a **tier** (`b2b_partners.tier_code`, default `standard`; seeded standard / silver / gold). A
tier's **discount** is a dated, append-only row (`partner_tier_discounts`), in force once a second person approved it
(`partner_price_approvals`): the newest approved row dated on or before the day; none = 0 %. Starting values 0 / 3 /
6 % (ADR 0019 b15, policy data, changed by a new row + approval in Booking admin, Pricing → Agency tiers). An optional
**contract rate card** (`partner_rate_cards`) gives one partner its own discount for the whole catalogue or one trip
plan, `valid_from` .. `valid_to`, approved the same way; while in force it overrides the tier (a plan card beats a
catalogue card; a 0 % card still overrides). Cards are append-only, so a card is **ended** by an **end card**
(`discount_pct` NULL, same partner and scope, approved the same way): from its `valid_from` that scope has no contract
rate (a plan line falls back to the catalogue card, then the tier); on one day an end card beats a discount card.

A B2B quote line's `b2b_price` = round up(the b2b rule's price x (1 - discount)) (`app.b2b.agency_pricing`,
`app.b2b.quotes`); the quote stores the tier and its discount, each line its discount and card. A line with a discount
adds it to the `price_hash`; a 0 % line hashes as before. The **RRP and the public "from" price** (`price.from.v1`,
`/price-parity`) stay the same for every agency: the RRP is the public price, the discount is the agency's margin.
**Floor (warn, never block)**: a discount can take AA's margin under the b2b rule's floor; nothing is capped, the
Agency tiers page shows the margin left per tier under the rule in force and flags it.

**Agency tariff (AA-1030 G3)** (`app.b2b.tariff`): one partner's own price per tour, season and pax band, the grid
of the price matrix (every `b2b` row of a sellable, served plan still valid today; one row per window x band, the USD
row first), each window from today at the earliest and **cut wherever a supplier rate of a component starts or ends**
(`cost_sheet.rate_cuts`, the cost sheet's own cuts: hand and overridden rows keep the window staff gave them and a
computed row is only cut as of its last recompute), neighbouring pieces with the same price joined back, so every
departure date of a tariff row prices the same; priced by the **quote engine itself**: the plan's `tp:` line for the
band's smallest party (all adults, default room mix) on the row's first day (`priced_for_date` = `valid_from`), through `components.price_lines` and `quotes.price_line` with
the partner's `quotes.PriceBasis` (b2b rule, FX, agency terms in force today). So `price` = what `POST /quotes`
answers for that party and day; `price_per_person` = it / party, rounded up like a line. Not the matrix `amount`
(rounded to 5 USD, peak uplift; quotes never read it); the row's public RRP is shown as it is. Rows a quote cannot
price (no components, no rate in force, no FX rate for a cost currency) are left out of the agency's tariff and listed
with the reason for staff. The asked currency must price today (S270): a quote currency without an approved FX rate in
force is refused before any row is read as **422 `invalid`** (`tariff.check_currency`, the message lists the
currencies that do price, USD always), the caller's choice and not an outage; `POST /quotes` keeps its 503
`unavailable` for the same gap, and a supplier cost currency without a rate stays AA's data problem (rows unpriced in
the tariff, 503 on a quote). Staff `GET /booking/pricing/partner-tariffs/{partner_id}` (`pricing.read`; JSON with
`partner`, `unpriced`, each row's `rate_card_id`; `?format=csv`) and the agency `GET /booking/b2b/v1/tariff`; both CSVs
are the same file (`tariff.CSV_COLUMNS`, formula-injection safe). Staff page: Pricing → Agency tiers → Agencies →
"View tariff" (`/pricing/agency-tiers/tariff?partner=<id>`: currency, Download CSV, Print; the print stylesheet in
`app/globals.css` hides the shell).

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

## Invoices and manual payments (AA-682 S3, bk-028): retired (AA-1046)

The customer proforma invoices, their schedule and the manual payments (with maker-checker) were the money of the B2C
half. Their routes, services (`app/ops/invoices.py`, `app/ops/payments.py`), reminders and staff pages are gone; agency
money is agency billing (bk-041..044, below). The B2C drop file archives their rows whole into `booking.retired_b2c_record`
and drops the tables. The pure money helpers (`app/ops/money.py`: minor units, line amounts, the request hash, the
card-number refusal) stay for supplier bills and agency billing. The case money track is gone too: no case takes
`invoice_sent` / `payment_pending` / `paid` any more (the drop file makes the guard refuse them), and a kept b2c case left in
one never moves (`app.ops.track.next_status`).

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

## Finance: FX-normalised costs, departure shared costs, purchase orders, profit (AA-1074, bk-045)

S275 study items bk-05 / bk-06 / bk-07 / bk-08 / bk-10 (root `docs/research/2026-11-10-odoo-tour-travel-plugin-comparison.md`;
our own code). Staff page `/finance` "Costs & profit" (tabs by permission), the case Suppliers tab's cost panel.

- **FX** (`app/pricing/fx.py`, SQL twin `booking.fx_plain_rate`): every native amount converted to USD at the plain
  APPROVED rate in force (no buffer; USD 1), **per line, half up to the cent**; totals are sums of cents. Snapshots:
  a supplier booking takes the rate of its net currency when **confirmed** (cleared by a re-request), a supplier bill
  when **approved** (set once). No approved rate: the snapshot stays empty and the views flag `fx_missing`.
  `fx_difference_usd` = the billed native amount at the bill's rate minus at the booking's rate.
- **Departure shared costs** (`app/finance/departure_costs.py`, `booking.departure_costs`): a coach / guide / group
  entrance of the whole departure; `rate` = the supplier rate band for the departure's **sold seats** (confirmed holds,
  line not declined / cancelled), basis = the rate unit (group / vehicle / day / person; person x seats), or `manual`.
  `bk_departure_cost_recompute` re-prices the unfrozen rate costs when the sold seats or a rate move (worker hook, a
  watermark like `bk_price_recompute`; it carries each departure's newest hold change, so seats that go back to an
  earlier count still re-price). *Order* makes one **departure-level supplier booking** (`departure_id`, no trip
  case; it moves no case status); the cost **freezes** when that booking is requested. Split to cases by seats
  (`v_departure_cost_share`, the remainder stays with the departure). A bill can name a departure (`departure_id`).
- **Purchase orders** (`app/finance/supplier_orders.py`, `PO-000123`): *Create orders* puts the pending bookings of
  the named B2B cases / departures on one draft per supplier + currency + service month; *Send* fixes the snapshot,
  total and the supplier's `contact_email`, then `bk_supplier_order_send` e-mails it (outside prod to
  `STAFF_NOTIFY_EMAIL`, `redirected`) and requests its pending lines (cases advance as for a hand request); a refused
  e-mail puts it back to draft with `send_error`. Confirmed / partly confirmed come from the lines' answers. The order
  names services, dates, pax, nets and AA references only (DM-6.8). Printable: `GET /supplier-orders/{ref}/document`.
- **Planned vs actual** (`v_cost_variance`): per booking, planned (net at the snapshot) against billed (approved / paid
  bill lines), variance only once billed. `GET /departures/{id}/costs`: shared costs, the sold cases with their share,
  the departure's bills, profit (revenue, profit and margin there and in `GET /finance/departures` are `null` without
  `profit.read`: sales and contracting see the costs only). The case cost panel adds USD per line, totals and the departure share;
  with the USD summary it keeps a per-currency line only for currencies other than USD. Every finance departure read
  (`GET /finance/departures`, `GET /departures/{id}/costs`, `GET /finance/profit/departures`) carries `tour_title`
  (`catalog.v_tour` by `source_id`, one read per page, the B2B desk lookup; `null` when the catalog has none or
  cannot be read); the UI shows it, else `Tour <key prefix>`, never the raw key.
- **Profit** (`v_case_profit`, `v_departure_profit`, `v_agency_profit_month`; `GET /finance/profit/cases | departures |
  agencies`, permission **`profit.read`** = admin, finance; proposed default): revenue = the confirmed quote total (or
  the cancellation fee), costs = billed, else planned, else the quote while nothing is booked; margin = profit /
  revenue. Never in `booking.b2b.v1`.
- Permissions: costs read `bill.read`, orders read `ops.read`, writes `ops.write`; both also need `pricing.read`.

## Agency booking operations (AA-1075, bk-047; S275 study items bk-11..bk-15, bk-18)

- **Staff-entered bookings** (`app.b2b.desk_bookings`, staff `/booking/b2b-desk/entries`, page B2B desk → "Agency
  bookings"): an agency booking that arrives by e-mail or chat is keyed in for the agency (`b2b_partners`). One
  transaction runs the agency API's own quote → hold → confirm (`quotes.quote_in`, `holds.place_in`,
  `holds.confirm_in`) with the partner's M2M client on the rows, so every rule applies (served tours, seats and
  allotments, credit stop, re-hold limit, the policy locked at confirm). The trip case says `entered_via = 'staff'`
  and `entered_by`; the bk-047 hold trigger emits `booking.created_by_aa` before the `line.confirmed` events so the
  agency's CRM imports it. The agency reference is the proposal ref (next free version). The form sends the tour's
  room mix (empty = the engine default) and any extra hotel rooms or services from supply as their own lines, for the
  same travellers (`ui/booking/deskEntry.ts`). **Price override**: staff
  may set the total with a reason; the quote lines are scaled to it before hashing (`quotes.scale_prices`). At or above
  the floor (the rule's floor margin, `quotes.floor_price`; never above the rule price when the agency discount is
  lower) it books at once; below it the row waits `awaiting_approval` (the hold keeps the seats for
  `b2b_hold_hours`) until a second person approves (confirm → `booked`) or rejects (hold released); a hold that
  expires first reads `lapsed`. Rows: `booking.b2b_desk_bookings` (facts fixed, status moves once). Same
  `Idempotency-Key` = same row.
- **Vouchers** (`app.b2b.vouchers`; agency `GET /booking/b2b/v1/bookings/{id}/vouchers`, contract 1.7; staff `GET
  /booking/b2b-desk/bookings/{ref}/vouchers`, case header "Vouchers"): one generated voucher per confirmed supplier
  booking (service, date, local time, pick-up, drop-off, voucher note, supplier name, confirmation ref, travellers of
  the line by age band, AA's ops desk line from the setting `b2b_ops_contact`). No traveller names, no prices and no
  contact field (the contract field policy keeps phones and e-mails out); the CRM prints its lead traveller on its
  copy. Staff fill the voucher fields on the Suppliers tab ("Voucher details", live bookings only).
- **Manifest, step 1** (`app.b2b.manifest`, `GET /booking/b2b-desk/departures/{id}/manifest`, page
  `/b2b-desk/manifest/{id}` from the desk's Departures tab, `format=html` prints): confirmed bookings on a departure
  (agency, its ref, case, travellers by age band less partial cancellations, rooms, line status) and the supplier
  bookings of those cases and of the departure. Counts only. Step 2 (a rooming list with names pushed by the CRM)
  needs the DM-6.8 / ADR 0019 amendment first; nothing stores names today.
- **Cancellation** (`app.b2b.amendments`): staff cancel for the agency (`POST /booking/b2b-desk/bookings/{ref}/cancel`,
  dry run, `entered_via = 'staff'` on `b2b_cancellations`), optionally asking a lower fee % or a waiver with a reason;
  `…/cancellation/override` asks later, `…/approve` (a second person) lowers `fee_amount` / `refundable_amount` (the
  only update the bk-047 guard lets through) and emits `booking.fee_changed`; `…/reject` clears the request. One open
  request at a time (409 `override_pending`: a new value needs the pending one rejected first) and the approver sends
  the fee % they saw (`{fee_override_pct}`; a different stored value is 409 `override_changed`), so a second person
  never approves a value they did not see. Erasure: `waive_reason` / `override_reason` may be set to `[redacted]` (the
  guards allow only that move, as `bk_owner` in a reviewed run-script). On any
  case cancel, pending and requested supplier bookings are cancelled (a request is withdrawn: staff tell the
  supplier) and confirmed ones raise "cancel with supplier"; the supplier's fee is recorded on the cancelled booking
  (`supplier_cancel_fee` + currency, `POST /booking/supplier-bookings/{id}/cancel-fee` or with the cancel) and is the
  booking's cost in `v_cost_variance` / `v_case_profit` until a bill of it is approved.
- **Partial cancellation** (`amendments.reduce_line`; agency `POST /booking/b2b/v1/bookings/{id}/lines/{ref}/reduce`,
  contract 1.7; staff `POST /booking/b2b-desk/bookings/{ref}/lines/{line_ref}/reduce`, case "Fewer travellers"): fewer
  travellers on one per-person line (tour, activity, entrance, meal; rooms, transfers and guides are re-quoted), at
  least one adult stays. Removed share = current line price x removed seats / current seats (infants hold no seat);
  fee = that share x the policy % of today (the locked policy; none on a departure AA cancelled). The hold line's
  seats go back on sale, the case pax follows the biggest line, staff are told to update live supplier bookings, the
  agency gets `booking.line_reduced`. Rows: `booking.b2b_line_reductions` (append-only, the amendment record: the
  approved version stays as sold because bk-003 locks it). A later cancel charges its fee on the rest; the settlement
  draft takes the refundable part off the total and bills the fees; `v_case_profit` revenue follows.
- **Child, infant and single prices**: the b2b rule may carry `child_pct` (a child pays that share of the adult price
  of a tour line) and `infant_flat_usd` (net per infant, priced by the rule): the quote engine applies them to `tp:`
  lines (`quotes.pax_sell_usd`; component lines and the tariff price as before). The cost sheet fills
  `child_amount`, `infant_amount` and `single_supplement_amount` (+ nets) on computed matrix rows (single supplement
  per night = single rate - half the double rate); hand rows may carry them. Plans carry `child_age_max` /
  `infant_age_max` (`PUT …/trip-plans/{source_id}/age-bands`; defaults in settings `b2b_child_age_max` 11,
  `b2b_infant_age_max` 1). The agency reads them (contract 1.7): `GET /products` `age_bands` (the plan's, else the
  defaults) and three sell-side tariff fields priced by the quote engine (`tariff._by_age_and_room`):
  `child_price_per_person`, `infant_price` (what one infant adds) and `single_supplement_per_person` (the party in
  single rooms less twin share; null when a single room has no rate), also the last three CSV columns. Never a net.
- **Per-tour terms** (`booking.trip_plan_billing_terms`, staff `/booking/tour-terms/{source_id}`, trip plan page →
  "Agency terms"): a tour's deposit % and balance days for every agency or one. Most specific wins: agency + tour >
  tour > agency > default (`config.plan_terms`), on the settlement draft (the tour of the hold's first `tp:` line) and
  on the agency's `GET /terms?source_id=` (`PaymentTerms.scope`). A separate table so `v_tenant_billing_status` (read
  by CIS) and the reminder job keep reading `billing_terms` per agency.

## My Payments: customer routes (AA-682 S5, contract `booking.customer.v1`): retired (AA-1046)

`/booking/customer/v1` and its traveller verifier (`app.auth.customer`) are gone with the B2C half; TripPlanner
removed My Payments in the same change. The root contract file is marked retired.

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
- **Backlog checks (AA-751 `app/jobs/metrics.py`; S263 alert diet, AA-1025, `app/jobs/ops_checks.py`).** A second
  lifespan task, next to the worker and only when it runs, reads two values from `booking.job` every 300 s:
  `oldest_queued_age_s` = age of the oldest due queued row without a cancel request (any kind), but **0 while a
  non-overdue job is running** (the worker is serial); `overdue_running` = running rows whose current attempt is older
  than 2 x `EXPECTED_SECONDS[kind]` (`app/jobs/kinds.py`, default 300 s; every handled kind must be listed, a unit test
  checks it). It reads `booking.job` directly (never through the queue), never raises, and is cancelled on shutdown
  before the pool closes. Until S263 these were CloudWatch metrics `AA/Jobs/Booking` with three Infra alarms; now the
  task checks them itself: `jobs_stuck` (> 900 s) and `jobs_overdue` (> 0) publish one e-mail per check per UTC day to
  the platform alerts SNS topic (env `ALERTS_TOPIC_ARN`, task role `sns:Publish`, both from AA-CIS-Infra). The dedupe
  is a ledger row in `booking.job`: kind `bk_ops_alert`, status `succeeded`, idempotency key
  `bk_ops_alert:<check>:<UTC day>` (`queue.record_alert`; a failed publish deletes it, `queue.forget_alert`). No
  worker claims those rows (never queued). The service being down is the Infra alarm `aa-booking-<env>-no-healthy-task`.
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
| `bk_retention_inbox_redact` | AA-825 / DM-6.6: 90 d after `received_at`, rebuilds `inbox_events.payload` from an allowlist of the contract's non-PII keys (`customer.name` / `email` / `phone` and `itinerary.custom_days[].note` nulled, any other key dropped, non-object parts nulled, so a failed non-conforming payload keeps no PII either) and replaces a failed row's `error`; sets `redacted_at` (bk-012). Row, `idempotency_key`, `contract_version`, status kept (dedupe unchanged); rows still `received` are skipped and counted. Nothing deleted, nothing archived. AA-969 (root ADR 0019 d16): a row whose trip case belongs to the pilot tenant is left out (`app/pilot.py`, result `pilot_tenants_skipped`). |
| `bk_tour_lifecycle_pull` | AA-986 L5 (root ADR 0015, Q4): queued by a maintenance hook only when `catalog.v_tour_lifecycle_outbox` (CIS 274, feature-detected) has a row `booking.inbox_events` lacks (idempotency key per UTC minute). One transaction per batch of 200: each new event goes into the inbox (source `cis`, contract `catalog.tour`, the producer key; `ON CONFLICT DO NOTHING`) and is applied: `retired` → one staff notification `tour.retired` (shared queue) per open trip case using the key (leg, the case's own key, or an item of a live version) and per trip plan (with its confirmed bookings on future departures, and its active B2B holds, which can no longer be confirmed: the text tells staff to contact the agency, `data.active_hold_list` names them); `restored` → `tour.reinstated` to the same, only when a retirement was notified; `paused` / `superseded` → nothing. Nothing is cancelled or changed. `inbox_cursor.safe_id` advances behind a 600 s window (contract rule 4). Not deploy-gate exempt (seconds). |
| `bk_tenant_pull` | AA-1028 R1 slice 2 (root ADR 0019 d1 / d9): queued by a maintenance hook once per 5-minute slot while `catalog.v_tenant_account_v1` (CIS 357, feature-detected) is readable; `not_configured` without `COGNITO_M2M_CRM_CLIENT_ID`. One transaction: every CIS tenant except kind `internal` gets one `booking.b2b_partners` row (name, `active` only while in service with `trade.book`, else `suspended`; `m2m_client_id` = the AA-Partner client; USD on insert); a changed row is updated; a row it projected before whose tenant left CIS is suspended (never deleted); adoption guard: a staff-registered row (`projected_at` NULL) whose tenant CIS does not hold (CRM-seeded agencies, the AA-972 synthetic partner) is left untouched and staff-editable, counted `awaiting_cis`, and adopted once CIS holds the tenant; `projected_at` (bk-037) marks every row it derived from a CIS tenant; one audit row per change (`system:bk_tenant_pull`). Not deploy-gate exempt (seconds). |
| `bk_trip_plan_sync` | AA-1029 R4 (root ADR 0005 d3 / d7): queued by a maintenance hook once per 15-minute slot while `catalog.v_tour` is readable. One transaction: every catalog key without a plan gets one (`sellable = false`, `synced_at`, `created_by` NULL; `INSERT … ON CONFLICT DO NOTHING`); a staff plan of a key in the catalog gets `synced_at` once; a plan whose tour left the catalog is left alone. With `catalog.v_tour_price_reference_v1` readable (feature-detected), the CIS source price text is kept on the plan with the parsed amount / currency (reference only), a changed text clears the review; with `catalog.v_tour_price_reference_v2` (CIS 402, AA-1091) readable it is read instead, and CIS's confirmed or parsed structured reference replaces the parse (needs-review values fall back to it). One audit row per run that changed something (`system:bk_trip_plan_sync`, `trip_plan.synced`). Not deploy-gate exempt (seconds). |
| `bk_price_recompute` | AA-1029 R4 (root ADR 0005 d3, ADR 0019 b15): queued by a maintenance hook whenever the watermark of its inputs moves (approvals, contract / rate / plan edits, component count, the UTC day; key = its hash). For every plan with a component or a computed row, one transaction each: the cost sheet (see "Tour price chain"), hand rows kept, the set stored only when it changed (audit `trip_plan.prices_recomputed`, dated history `cost_sheet`). A plan that fails is counted and the run raises at the end (retried; the others are stored). Payload `{plan_id?}`. Not deploy-gate exempt (seconds to a minute). |
| `bk_case_lifecycle` | AA-682 S2 (bk-027): queued once per UTC day by the `OpsDailySchedule` hook (first pass at or after 01:00 UTC, key `bk_case_lifecycle:<UTC date>`). Moves open sold cases along the travel dates (`pre_departure`, `on_trip`, `completed`; see "Trip checks and the case lifecycle"), one gate notice `trip_case.gate_blocked` per blocked case / target / reasons (`notifications_reminder_uq`), and clears emergency contacts + check notes at trip end + 90 days. Batches of 200 cases, `FOR UPDATE SKIP LOCKED`, a savepoint per move. **DRY RUN first** (DM-7.2): a manual run is a DRY RUN unless `dry_run: false`; the scheduled run stays a DRY RUN until one live run over every case (no `case_ids`) has succeeded. Payload `{trigger, dry_run?, case_ids?}` (ids only, ≤ 50); result = mode, ops date + zone, move counts, moved / blocked case codes (≤ 50), cleared counts. Runs readable at `GET /booking/case-lifecycle/runs`. Not deploy-gate exempt (seconds). AA-969 (root ADR 0019 d16): the clearing never touches a case of the pilot tenant (`party_tenant_id`). |
| `bk_ops_reminders` | AA-682 S2: same daily hook and key pattern. Notifications only, each once (`reminder_key`, `ON CONFLICT DO NOTHING`): `supplier_booking.waiting` (requested more than `supplier_answer_days` ago; to the requester, else the shared queue; again after a re-request) and `trip_check.due` (open check due within `trip_check_reminder_days` or overdue; to the assignee, else the shared queue; again when the due date changes). S3 (bk-028): `payment.due_soon` (a schedule row of a sent invoice due within `payment_due_soon_days`) and `invoice.overdue` (past due, at the steps of `payment_overdue_reminder_days` [1, 7], the highest step reached), each one staff row (the case's sales owner, else the shared queue) + one customer My Trips row (no e-mail); read in the ops time zone. S4 (bk-030): `supplier_bill.due` (a received or approved bill due within `supplier_bill_due_soon_days`, then once more when overdue; shared staff queue). Never writes money. Not deploy-gate exempt. |
| `bk_saas_invoice_draft` | AA-1037 R4 (root ADR 0019 d9): the software-fee drafts of a month (payload `period` YYYY-MM, optional `tenant_id`): every live subscription (active, past_due) of `catalog.v_tenant_billing_v1` (CIS 377; `skipped` without it) gives `subscription` / `extra_seats` lines on the agency's draft (one live invoice per agency, kind, period start and currency); a plan without a price gives a zero line (`price_missing`); a re-run replaces the job lines of a draft and leaves an issued invoice alone. Queued daily by `DraftSchedule` for the current month (key per month) and by staff (`POST /booking/agency-billing/runs`). One transaction per agency; a failing agency is counted and the run raises at the end. Not deploy-gate exempt (seconds). |
| `bk_agency_invoice_reminders` | AA-1037 extras G4: once a day (`ReminderSchedule`, key per day; payload `day`), the overdue issued agency invoices with a balance (at most 500): per invoice the highest step reached (`agency_reminder_steps_days` + the credit stop at `suspend_after_days`) not yet in `booking.agency_invoice_reminder` is e-mailed (outside prod to the staff inbox) and recorded, or recorded `skipped` without a billing e-mail. No `SES_FROM_ADDRESS` (or no staff inbox outside prod): `skipped`, nothing written. A failed send is counted, the others are kept and the run raises (retried); a sending pause fails it. Not deploy-gate exempt (seconds). |
| `bk_settlement_draft` | AA-1037 R4: the tour settlement drafts of a month (default: the closed month): per confirmed B2B hold, `deposit` (deposit % of the B2B total) or `full` (booked inside the balance window), then `balance` once the balance date (departure - `balance_days_before_departure`) is before the period end, or one `cancellation_fee` line (the fee less what was billed; a credit when negative) for a cancelled booking; terms from `billing_terms` (agency override, else default); one live line per (hold, role), so nothing is billed twice and an unbilled earlier event lands on the next run. Queued daily by `DraftSchedule` for the previous month and by staff. Not deploy-gate exempt. |
| `bk_departure_cost_recompute` | AA-1074 (bk-045): queued by a maintenance hook whenever the watermark (per departure with an unfrozen rate-priced shared cost: sold seats and newest hold / hold line change; the newest rate change) moves; key = its hash. One transaction per departure: each active, unfrozen `rate` cost re-reads the band for the sold seats; a pending booking behind it follows (net, pax, day). Audit `departure_cost.repriced`. Payload `{departure_id?}`. Not deploy-gate exempt (seconds). |
| `bk_departure_watch` | AA-1073 (bk-046): queued once per UTC day by `DepartureDailySchedule` (first pass at or after 01:00 UTC, key `bk_departure_watch:<UTC date>`) and by staff (`POST /booking/b2b-desk/departure-checks/runs`, key per UTC minute; `dry_run` reports only). Scheduled departures with a minimum, not started, at most 500 per run, `FOR UPDATE SKIP LOCKED`: sold >= minimum → `guaranteed` (audit, event); else on or after the decision date and not stamped → `at_risk_at` (event `departure.at_risk`, one staff notification per departure, audit). Not deploy-gate exempt (seconds). |
| `bk_allotment_release` | AA-1073 (bk-046): same daily hook and staff run. Stamps `released_at` on the allotments in force whose release date (`start_date - release_days_before`, UTC day) is today or earlier (at most 500, `SKIP LOCKED`); the trigger emits `allotment.released`; one audit row each. Not deploy-gate exempt (seconds). |
| `bk_supplier_order_send` | AA-1074 (bk-045): one per Send of a purchase order (key per order and press). E-mails the order's snapshot through SES (outside prod to the staff inbox), then marks it `sent` and requests its pending lines; a permanent SES refusal (or no sender / staff inbox) puts the order back to `draft` with `send_error`; throttling retries. At least once (a worker dying after SES accepted re-sends). Not deploy-gate exempt. |
| `bk_noop_sleep` | AA-993 G4 proof, **Dev only**: registered only when `JOB_TEST_KINDS` is truthy (`Settings.job_test_kinds`; AA-CIS-Infra sets it on the Dev task). Sleeps `payload.seconds` (1..3600) in 5 s steps, writes `progress.slept_seconds` each step and resumes from it after a release; a cancel request (`cancel_requested`, SQL only) ends its own row `cancelled`. Port of CIS `noop_sleep`. Deploy-gate exempt; never deploy-blocking. Touches nothing else; the serial worker runs nothing else while it sleeps. |
| `bk_b2b_hold_expire` | AA-973: queued by a worker maintenance hook only while a B2B hold is due (idempotency key per UTC minute, no row otherwise); moves due `active` holds to `expired` in batches of 500 (`FOR UPDATE SKIP LOCKED`, at most 20 batches). Idempotent; reads never wait for it. Not deploy-gate exempt (a run takes seconds). Each status write emits `hold.expired` (bk-022 trigger). |
| `bk_notify_email` | One per new inquiry notification (idempotency key `bk_notify_email:<notification_id>`): reads the notification, trip case, lead customer, legs and (live, soft) the CIS `catalog.v_tour` titles/countries, sends one e-mail to `STAFF_NOTIFY_EMAIL` through SES; result `{outcome: sent, message_id, ...}`. Not configured → `skipped` with the reason; an SES refusal that cannot change (rejected, sender not allowed, paused) fails at once, throttling / network errors retry with backoff. At least once: a worker that dies after SES accepted the message re-sends it. |

**The pilot tenant** (AA-969 part 2, root ADR 0019 d16, `app/pilot.py`): the one real agency allowed on Dev for at
most 8 weeks, read from the CIS contract view `catalog.v_tenant_account_v1` (`is_pilot`, feature-detected). The two
clean-up paths that change tenant data on a schedule skip it: `bk_case_lifecycle`'s clearing of trip-check contacts
and notes, and `bk_retention_inbox_redact` (an inbox row whose trip case is the pilot tenant's). Fail-closed (logged
`pilot_view_unreadable`) when the view is unreadable: the pilot is unknown, so the clearing is skipped that run and the
inbox redaction stays a DRY RUN (`pilot_unknown`). The other
kinds do not clean up tenant data (job-row retention, hold expiry, projections, reminders, the case moves).

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
  (AA-993 M4a, root ADR 0017 d7; read by the Deploy Dev `schema-gate` job). Every body also carries `storage`
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
  `AA-CIS-Infra/db/migrations/booking/` (Migration Sync opens the copy PR) and applied on Dev. Never applied from a
  laptop. Root ADR 0017 sorts each file into one of two classes: **expand** (first line `-- migrate: expand`; new
  tables, nullable columns, plain indexes and the like) ships in the code PR and is applied by Infra's Migration Apply
  with no hand step (bk-034 was the first); **contract** (anything else: constraints on existing tables, cross-owner
  grants, data rows) merges as a migration-only PR first and waits for a manual DB Migrate apply before the code that
  needs it merges (MIG-4). Add a key to `REQUIRED_SCHEMA_VERSIONS` only when the code reads its shape; Deploy Dev's
  Schema gate then waits until Dev has it.
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

`staff_users` (thin profile keyed by `cis_user_id` = JWT `sub`; role `admin|sales|contracting|content_creator`
(`|finance` since bk-038, AA-1035),
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
billing currency, `credit_terms_ref`, `m2m_client_id`; SELECT/INSERT/UPDATE, never deleted; since AA-1028 the
projection of every CIS tenant written by `bk_tenant_pull`, bk-037 `projected_at`: on a projected row staff edit only
`billing_currency` / `credit_terms_ref`, the projected fields answer 409 `projected_from_cis`); `b2b_quotes` +
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

### `bk-029` (AA-996 Booking state of CIS countries and destinations — needs `bk-001`, `bk-003`, `bk-005`)

`country_profiles` (one row per ISO country of `countries` once staff change something: `status` active | inactive,
`area`, `internal_notes`, `status_reason`) and `destination_profiles` (one row per CIS place, keyed by
`catalog.v_destination.destination_id`, checked against the view on every write; no FK across schemas). No row =
active. Content stays in CIS: names, coordinates, photos and descriptions are read live, nothing is copied. Role
`booking` never deletes them (soft state only); the status is informational for staff (lists, filters, badges) and
does not yet block a supplier place pick or a proposal item. Current-state rows (history in `audit_logs`): no DM-7.1
row. Not in `REQUIRED_SCHEMA_VERSIONS` (AA-996's call).

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

### `bk-033` (AA-996 trip plan margin width — needs `bk-032`)

Widens `trip_plan_prices.effective_margin` from NUMERIC(6, 4) to NUMERIC(12, 4), the width the API and its tests were
written for (a high override could overflow the old column). It ships as its own file because bk-032 had already been
copied and applied with the old width: an applied file is never edited. Changes no shape the code reads, so not in
`REQUIRED_SCHEMA_VERSIONS`.

### `bk-034` (AA-993 itinerary source tour index — needs `bk-003`)

An index on `trip_case_version_itinerary (source_tour_id)`, the one lookup of the catalog lifecycle "affected trip
cases" read without one. The first **expand** file of this repo (root ADR 0017): it shipped in the code PR and was
applied on Dev by Migration Apply with no hand step. In `REQUIRED_SCHEMA_VERSIONS`.

### `bk-035` (AA-1003 R2 inquiry inbox key view — needs `bk-001`, `bk-003`, `bk-017` and Infra `rl-001`)

`v_ops_inquiry_inbox (source, idempotency_key, status, received_at, processed_at, has_trip_case)`: one row per
`inbox_events` row of contract `inquiry.submitted` received in the last 30 days, with whether a trip case carries the
same `idempotency_key` (a soft-deleted case counts). Keys, states and timestamps only: no payload, no `error` text, no
customer, no case id. The CIS TP-03 handoff check matched it (until 2026-11-03: TP-03 is retired with the B2B-only
model, root ADR 0019, and CIS reads the view no more; it is dropped with the B2C tables)
with `tripplanner.v_ops_inquiry_keys` (tp-022) to name
each inquiry not received, parked or failed with its age, and each one received with no trip case after 24 h
(contract `ops.overview.v1`, key view; columns append-only). Owned by `bk_owner`; role `booking` SELECT only; `cis_app`
SELECT. A **contract** file (the cross-owner grant is not expand, root ADR 0017): manual DB Migrate apply. Not in
`REQUIRED_SCHEMA_VERSIONS` (no Booking code reads it).

### `bk-036` (AA-1034 R0 B2C freeze — needs `bk-001`, `bk-024`)

Root ADR 0019 d8 (B2B-only model): `REVOKE INSERT` from role `booking` on the four B2C traveller tables
`customers`, `customer_identities`, `customer_otps`, `customer_sessions`. SELECT, UPDATE and DELETE stay (staff read
the frozen rows; retention and erasure redact and delete them). Guard: refuses while INSERT is still granted on a table
that got a row in the last hour (the intake is still on), on the first apply only (keyed on the registry row, so a
full replay that grants INSERT again next to recent test rows still applies). A **contract** file
(privileges of existing tables): migration-only PR, applied by Migration Apply when the job tables are idle. Not in
`REQUIRED_SCHEMA_VERSIONS`. The frozen B2C code keeps its integration tests through the conftest fixture
`b2c_insert_regranted` (INSERT given back for the test only). The tables are dropped later under the destructive-ops
DRY RUN procedure.

### `bk-043` (AA-1030 R4 agency price tiers — needs `bk-009`, `bk-019`)

Root ADR 0019 b15. `partner_tiers` (code, label, sort order; seeded standard / silver / gold), `partner_tier_discounts`
(dated discount of a tier on the b2b price, append-only; seeded 0 / 3 / 6 % from 2026-11-01, approved as
`seed:bk-043`), `partner_rate_cards` (a partner's contract discount for the whole catalogue or one trip plan,
`valid_from` .. `valid_to`, append-only), `partner_price_approvals` (the second-person approvals of both, with a
maker-checker trigger; a table of its own because bk-007 / bk-021 re-add the `policy_approvals` entity CHECK when
replayed), `b2b_partners.tier_code` (default `standard`, FK), and what a quote was priced with
(`b2b_quotes.partner_tier_code` / `tier_discount_pct`, `b2b_quote_lines.agency_discount_pct` / `rate_card_id`). DM-7.1
rows in the root data standards. A **contract** file: migration-only PR, applied on Dev by Infra Migration Apply when the
job tables are idle; the code PR adds it to `REQUIRED_SCHEMA_VERSIONS`.

### `bk-039` (AA-1029 R4 tour price chain — needs `bk-004`, `bk-009`, `bk-032` and the role `tripplanner`)

Root ADR 0005 d3 / d6 / d7 as amended by root ADR 0019 b15. `pricing_rules.rrp_markup_pct` (b2b rows only, CHECK;
0.2500 = the suggested retail price is the agency net price + 25 %; policy data, approved like every rule).
`trip_plans.synced_at` (the bridge job `bk_trip_plan_sync` created or matched the plan from `catalog.v_tour`) and the
reviewed **reference price** kept from the CIS source text (`reference_price_raw`, parsed `reference_amount` /
`reference_currency`, `reference_reviewed_by` / `_at`; reference only, never a selling price). `trip_plan_prices.computed`
(written by the cost sheet job `bk_price_recompute`; a recompute replaces computed rows that are not overridden and
keeps every hand row) and `rrp_amount` (b2b rows). `trip_plan_price_history`: the dated price list (each change closes
the open rows, `effective_to`, and inserts the new set; role `booking` SELECT, INSERT and UPDATE of `effective_to`
only; DM-7.1 row in the root data standards). The contract view `booking.v_from_prices` (price.from.v1, root
`docs/contracts/price.from.v1.md`): per catalog key, the cheapest RRP per person for 2 travellers among the b2b rows
still valid, USD first; never a net, an agency price or a margin; SELECT to `booking` and `tripplanner` (+ USAGE on
schema `booking`, consumer grant in Infra `db/owners.yaml`). A **contract** file (ALTERs, CHECKs, a cross-owner grant):
migration-only PR, applied on Dev by Infra Migration Apply when the job tables are idle; the code PR adds it to
`REQUIRED_SCHEMA_VERSIONS`.

### `bk-040` (AA-1029 portal slice — needs `bk-039` and the role `cis_app`)

`GRANT SELECT ON booking.v_from_prices TO cis_app` (USAGE on schema `booking` held since bk-017): the CIS pool read of
the agency Content studio Marketplace shows the public "from" price (contract `price.from.v1`). The view reads its base
tables with `bk_owner`'s rights, so `cis_app` still reads no price table (guarded in the file). A **contract** file (a
cross-owner grant, Infra `db/owners.yaml` consumer grant + isolation probe cell): migration-only PR, applied on Dev by
Infra Migration Apply when the job tables are idle. No Booking code reads it: not in `REQUIRED_SCHEMA_VERSIONS`.

### `bk-041` (AA-1037 R4 agency billing tables — needs `bk-019`, `bk-020`)

The first expand file with new tables (root ADR 0017): `billing_entity`, `tax_rule`, `billing_terms` (default row
seeded: 30 % deposit, balance 30 days before departure, due 14 / 14 days, warn 1, suspend 30, USD), `invoice_sequence`
(gap-free AAS- / AAT- numbers per year, taken in the issue transaction), `agency_invoice` (party_tenant_id ->
`b2b_partners.tenant_id`, kind saas | settlement, draft | issued | paid | void, snapshots, `e_invoice_no`; one live
invoice per agency, kind, period start and currency), `agency_invoice_line` (composite FK on (invoice, kind): software
fees and tour money never mix, b17; one live line per (hold, role)), `agency_payment` (maker-checker CHECK, document
reference NOT NULL). Seed rows go only into tables the file creates; personal / payment columns carry their DM-6.1 tag.
DM-7.1 rows in the root data standards (S269).

### `bk-042` (AA-1037 R4 agency billing guards + the CIS status view — needs `bk-041`, `bk-017`)

A **contract** file: `REVOKE DELETE` on payments, the number series, tax rules and entities; guard triggers (only a draft
is deleted or edited; issued facts are frozen; draft -> issued -> paid | void; paid only when approved payments cover the
total; e-invoice number once; lines change only on a draft; payments: one final decision, never above the total) and
`agency_payment_settle` (the database marks the invoice paid); `updated_at` triggers; the view
`booking.v_tenant_billing_status` SELECT to `booking` and `cis_app` (Infra `db/owners.yaml` consumer grant). The code
raises readiness to bk-042.

### `bk-044` (AA-1037 R4 extras G4 / G6 — needs `bk-041`)

An **expand** file: `booking.agency_invoice_reminder` (one row per overdue agency invoice and reminder step, PK
`(invoice_id, step_days)`; `sent` with the address (`pii:contact`) and the SES message id, or `skipped`
`no_billing_email`) and `booking.partner_terms_clause` (clauses of AA's terms to agencies as data: code, heading (the title), body,
`free_cancel_days`, `draft` | `approved`, version; approved by a person who is not its last editor, CHECK), one seed row:
`price_error`, DRAFT wording for the business owner to approve. SELECT / INSERT (and UPDATE on clauses) to `booking`.
The code raises readiness to bk-044.

### `bk-045` (AA-1074 finance: FX-normalised costs, departure shared costs, purchase orders, profit — needs `bk-007`, `bk-019`–`bk-021`, `bk-026`, `bk-030`)

A **contract** file (MIG-4: migration-only PR, applied by Infra Migration Apply once every job table is idle, then the
code PR raises readiness to bk-045). `booking.fx_plain_rate(currency, day)` (the approved plain rate in force, no
buffer; 1 for USD). FX snapshots `fx_rate_per_usd` + `fx_as_of` on `supplier_bookings` (taken at confirm, cleared by a
re-request) and `supplier_bills` (taken once, with the approval). New tables `booking.departure_costs` (a cost shared by
a departure: rate band for the sold seats or manual, frozen when its departure-level supplier booking is requested)
and `booking.supplier_orders` (`PO-000123`: draft → sending → sent, → cancelled; snapshot of what the supplier received).
`supplier_bookings.trip_case_id` becomes nullable with `departure_id` (exactly one of the two) and `supplier_order_id`;
`supplier_bills.departure_id` (a bill of one departure's shared costs; a case or a departure, never both). The three
supplier guards are replaced (bk-026 / bk-030 bodies plus the departure, order and FX rules). Views (`conf:commercial`,
staff only, never in `booking.b2b.v1`): `v_cost_variance`, `v_departure_cost_lines`, `v_departure_cost_share`,
`v_case_profit`, `v_departure_profit`, `v_agency_profit_month`. USD rule: each native amount converted per line and
rounded half up to the cent; totals are sums of cents. DM-7.1 rows for the two tables are in the root data standards.
No row of `booking` can be deleted from either table; `cis_app` reads none of it.

### `bk-046` (AA-1073 departures: minimum to operate, sales cutoff, at-risk stamp, agency allotments — needs `bk-019`, `bk-020`, `bk-022`)

A **contract** file (MIG-4: migration-only PR, applied by Infra Migration Apply once every job table is idle, then the
code PR raises readiness to bk-046). `departures` gains `min_to_operate` (0..capacity, 0 = none), `sales_cutoff_days`
(0..90, default 0), `decision_days_before` (0..365, default 21) and `at_risk_at` (only with a minimum). The outbox takes
the event types `departure.at_risk` and `allotment.released` (CHECK `outbox_allotment_chk`: it names its departure);
`departures_outbox` also emits `departure.at_risk` when `at_risk_at` is first stamped on a scheduled departure (trigger
on `status, at_risk_at`). New table `booking.departure_allotments` (departure, partner, `seats` 1..1000
`conf:commercial`, `release_days_before` 0..365, `replaces_id`, created / approved / released / ended by + at,
`end_reason` `replaced|withdrawn|rejected|departure_cancelled`): append-only facts (column UPDATE grants on the
approve / release / end columns only, no DELETE), maker-checker CHECK, one in force and one pending per departure and
agency (partial unique indexes), guard trigger (starts pending on a departure not cancelled, approved / released / ended
once) and the release outbox trigger. DM-7.1 row in the root data standards. `cis_app` reads none of it.

### `bk-047` (AA-1075 booking ops: staff agency bookings, vouchers, cancellation override, partial cancellation, child prices, tour terms — needs `bk-021`, `bk-022`, `bk-026`, `bk-041`, `bk-045`, `bk-046`)

A **contract** file (MIG-4). `trip_cases` gains `entered_via` (`api|staff`, default `api`) and `entered_by`;
`supplier_bookings` gains `service_time`, `pickup_point`, `dropoff_point`, `voucher_note` (`pii:free_text`) and
`supplier_cancel_fee` + currency (cancelled bookings only); `b2b_cancellations` gains `entered_via` / `entered_by` and
the override columns (`fee_override_pct` below `fee_pct`, `waive_reason`, requested / approved by + at; CHECK
approver <> requester; column UPDATE grants; guard trigger: facts fixed, amounts change once to a lower fee in the
approving update; `booking.fee_changed` trigger); `trip_plan_prices` gains child / infant / single supplement amounts
and nets; `trip_plans` gains `child_age_max` / `infant_age_max`; `pricing_rules` gains `child_pct` /
`infant_flat_usd`. New tables `b2b_desk_bookings` (maker-checker CHECKs, guard), `b2b_line_reductions` (append-only,
`booking.line_reduced` trigger) and `trip_plan_billing_terms`. Outbox: event types `booking.created_by_aa`,
`booking.line_reduced`, `booking.fee_changed` (hold / booking / line CHECKs widened); `b2b_holds_outbox` also emits
`booking.created_by_aa` when a staff-entered case's hold is confirmed. `v_cost_variance` / `v_case_profit` replaced
with the same columns (recorded supplier cancel fees, partial cancellations). Settings `b2b_ops_contact` (empty),
`b2b_child_age_max`, `b2b_infant_age_max`. DM-7.1 rows in the root data standards. `cis_app` reads none of it.

### `bk-048` (AA-1073 rest: departure board view, ops sheet, departure fees, price adjustments, early booking — needs `bk-022`, `bk-041`, `bk-044`, `bk-046`, `bk-047`)

A **contract** file (MIG-4). `departures` gains `meeting_point`, `meeting_time`, `ops_note` (`pii:free_text`); new
`departure_staff` (role guide / tour_leader / driver, a staff user XOR a supplier); `departures_outbox` also emits
`departure.updated` on a meeting change (trigger on status, at_risk_at, meeting_point, meeting_time). New
`departure_fees` (maker-checker, decision final, guard; `departure.fee_charged` trigger) and outbox columns `fee_id`,
`amount`, `currency`; `agency_invoice_line` gains `source_fee_id` and the settlement role `departure_fee` (role / source
CHECKs replaced, one live line per fee). New `departure_price_adjustments` (append-only, maker-checker, one in force and
one pending per departure); `pricing_rules` gains `early_days` / `early_discount_pts` (b2b and fixed_departure only,
points at most the target margin); `b2b_quote_lines` gains `price_note` (jsonb object). View `v_departure_occupancy`.
A draft partner terms clause `departure_fees`. DM-7.1 rows in the root data standards. `cis_app` reads none of it.

Waiting for decisions: `trip_case_countries` / `areas` (O7, ISO mapping; the Booking state of CIS countries and
places shipped as `country_profiles` / `destination_profiles`, bk-029) and catalog-backed version item types (AA-679
follow-up). `v_from_prices` shipped with bk-039 (AA-1029). Operations S5 (My Payments) shipped: see "My Payments: customer routes".

## Out of scope today

A payment provider (money is marked by hand, AA-682 D6), tax lines / the legal VAT e-invoice on the B2C invoices
(proforma only, D7; agency invoices carry a tax rule and the manually recorded e-invoice number, AA-1037), the
e-invoice provider API (later; phase 1 = manual `e_invoice_no`), invoice PDF and e-mail of agency invoices (only the
overdue reminders are e-mailed, AA-1037 G4; documents are printable HTML, no PDF renderer),
maker-checker on billing settings (audited only; the R4 "editable Settings" slice), a customer notifications feed (the customer rows
of AA-682 are written but `booking.customer.v1` serves payments only; critic note 11),
**customer e-mail** (proposal send, invoice send and reminders write customer notification rows only and say so; the
only e-mail today is the staff new inquiry e-mail, AA-811), Booking-side SES bounce/complaint suppression, a catalog
sync of content (D4: live reads instead; the only catalog outbox consumer is the tour lifecycle pull, AA-986; the
AA-1029 bridge copies catalog keys and the reference price text only), catalog titles on trip case legs (Q11),
supply facts from a rate sheet (the import only prices existing hotels / services), the tax table (ADR 0005 d10),
agency tier discounts (ADR 0019 b15, AA-1030), contract document upload, supplier hard delete,
editing or withdrawing a pending pricing rule / FX row (append-only: enter a new row with another date), any use of
`shared.job`, the `trip_case.status_changed` back-channel, CIS grant migrations, the frontend's custom domain (the ECS
service `aa-booking-dev-admin` serves `booking.lumiguides.it.com` since AA-911, root `frontend/`).

## AI boundary for high-risk advice (AA-820)

AA-Booking must not use AI to advise travellers on insurance (choice, underwriting or claims), financing or
instalments, refunds or claims decisions, or visas and immigration, unless a qualified staff member reviews each
recommendation, with authority to change it, before it is sent, **and** the traveller is clearly told that AI was
used. AI-drafted proposals and quotes stay behind the existing staff review before sending. Any traveller-facing chat
must disclose AI at the start of each session. Source: Anthropic Usage Policy high-risk use case requirements
(effective 12/11/2026). The same rule binds agencies through clause .3(8) of the tenant AI Schedule v1.0 (root
`docs/legal/tenant-ai-schedule.md`, approved by the tech lead under root ADR 0019 b20; counsel reviews it before the
first paying agency, and a rewrite there is mirrored here). Today Booking makes no AI calls, so this is a rule for
future features. When a feature
needs AI it goes through CIS jobs (as the supply content writer does, AA-835) or the shared AI gateway core of
AA-CIS-App (`shared/aa_llm/`, vendored), so every call is budgeted and written to the shared ledger (root ADR 0009
decision 13); never a provider SDK called directly.

Booking has no AI calls; any future call goes through the shared AI core gateway (root ADR 0009: route, budget, one
ledger row per call), never a direct provider call. `backend/tests/unit/test_gateway_bypass.py` (AA-642 L6) fails CI
on a Bedrock / vendor SDK call, a vendor SDK import or a vendor endpoint in `backend/app`, `backend/scripts` or
`frontend/`.

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
  Offered only on the registered host with the client secret set: the `booking-staff` client
  (`COGNITO_BOOKING_STAFF_CLIENT_SECRET`, from the task's secret `booking/dev/admin-web`) on `booking.lumiguides.it.com`;
  `next dev` adds `localhost:3001`. Any other host gets no company e-mail sign-in.
- **Preview sign-in: retired (AA-911, S272; root ADR 0006a A11 superseded by A13).** The fixed Preview host
  `booking-preview.lumiguides.it.com`, the Vercel alias `aa-booking-admin.vercel.app`, the Vercel project
  `aa-booking-admin` and the Preview-only `booking-staff-preview` client are gone. The API keeps the read-only cap for
  a client set through `COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID` (`app/auth/staff.py`: any non-GET/HEAD/OPTIONS is
  403 `preview_read_only`; permissions limited to `PREVIEW_PERMISSIONS`), but the default is empty, so a token of the
  deleted client is 401.
- **Legacy CIS password bridge: retired (AA-911, S271).** `POST /api/auth/login` and the password form are gone;
  `proxy.ts` sends a browser that still holds `bk_cis_session` (or a non-Cognito `bk_access`) to `/login` with the
  cookies cleared, and the BFF answers 401 without a company e-mail session.
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
  `PageFrame` the core frame with the Booking wide column and gutter; `PageFrame bleed` for a detail page with an
  edge-to-edge header band). Frontend Build checks the copy (integrity,
  `--aa-*` definitions only in the core tokens, provenance of the lock's CIS commit on CIS main) and lints `app/**`
  and `ui/booking/**` with the kit `tailwind` preset at error, plus the staged rule `no-raw-form-control` (AA-994
  S6d: every form uses the kit `Field` / `Input` / `Select` / `Textarea` / `Checkbox` / `Radio` / `DateField`); no page
  writes a raw `<main>` (the pages outside the staff shell use `ui/local/StandaloneFrame.tsx`). Violations from before
  S4b live in `frontend/eslint-suppressions.json`, which may only shrink (S6d: hand-styled buttons and four hex shadows
  are what is left).
- Case workspace and proposals (AA-996 audit slice 4, S254): the case header names the customer, the sales agent and
  the date range with its length; Overview puts the trip facts (two columns, Edit pencil, approved total) in the main
  column, a custom trip's empty legs card is one line, the TripPlanner inquiry panel shows the client's message,
  budget, travel window, style and party when the snapshot carries them, and Countries falls back to `leg_countries`
  (`GET /booking/trip-cases/{ref}`: the CIS tour country of each keyed leg, then the countries of the inquiry's
  route-day places; read live, fail soft). AA-1013: the builder and the customer-style preview show "CIS has newer
  text for this tour" when an item's CIS day title moved on (the builder: "CIS now: …" per item and Refresh from CIS on
  a draft); an item that is not a day copy (a leg, a B2B line, one-day ones included) names its CIS tour; the preview's day column is a `minmax(0, 1fr)` track (it
  scrolled sideways by 257 px at 390). The Proposals tab rows have Preview / Send / Discard draft. A sent or
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
  `DateField` ("28 Oct 2026", day-first input; `ui/core/DateField.tsx` since AA-994 S4a/S4b; `DateField` from `@/ui` is
  `ui/booking/FormDateField.tsx`, the kit field with the kit control look, `size="sm"` next to small Inputs) and the Booking
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
- Frontend on Vercel: retired (AA-911, S272). `vercel-deploy.yml`, `frontend/vercel.json`,
  `.github/scripts/check_security_headers.sh`, the per-PR previews and the `preview-signin` label are gone. Per-PR UI
  evidence is the required check `Visual (next start, mocked API + kit gallery)` (root ADR 0019 d23 R7).
- Frontend on ECS (AA-911, root ADR 0019 d23): the app runs as the `booking-admin` container (port 3001) of
  its own service `aa-booking-dev-admin` (AA-CIS-Infra `modules/environment/staff_web.tf`, S271 amendment of ADR 0019
  d23: one service per staff app; the CIS admin is a separate service). `.github/workflows/deploy-admin-dev.yml` builds `frontend/Dockerfile` (Next standalone, only when
  `NEXT_OUTPUT=standalone`) on an arm64 runner, pushes `aa-booking-dev-admin:<sha>`
  (IMMUTABLE), copies the latest revision of family `aa-booking-dev-admin` with only the image of `booking-admin` swapped (env and secrets from that revision;
  any other container of the family is kept as it is: AA-1060, the non-essential WordPress test site sidecar `test-wp` that
  AA-CIS-Infra `modules/environment/test_wp.tf` adds; the deploy requires exactly one essential `booking-admin`), rolls the service
  and checks `https://booking.lumiguides.it.com/api/healthz` answers this commit from ECS (resolved to the ALB on the
  runner through `vars.STAFF_WEB_ALB_DNS`); `booking.` points at the shared ALB since the cut-over (root runbook
  `docs/ops/runbooks/staff-web-ecs-cutover.md`). Behind the ALB the standalone server's `request.url` /
  `nextUrl` name its bind address (`https://0.0.0.0:3001`), so the auth redirects use a relative Location
  (`lib/redirect.ts`) and `lib/sameOrigin.ts` takes the public origin from `Host` when `X-Forwarded-Proto` is https.
- Security headers (AA-879): `frontend/next.config.ts` sends the same six headers as the CIS admin, the CIS portal and
  TripPlanner (HSTS, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, a minimal CSP `frame-ancestors 'none';
  base-uri 'self'; form-action 'self'; object-src 'none'`, `Permissions-Policy`). Every deploy checks them on the
  production host through the public smoke below.
- Public smoke (AA-993 V5): after a verified deploy the job `smoke` of `deploy-admin-dev.yml` runs the Playwright project
  `smoke-public` (`frontend/e2e/smoke-public/`) against `booking.lumiguides.it.com` after the deploy: GET `/login` 200
  on the same host, the card and "Sign in with company email" render, no console error, no failed same-origin request,
  every `next.config.ts` header with its value. Signed out, no secret; screenshot artifact `booking-admin-smoke-public`.
  Job `result` is red unless config, deploy, verify and smoke succeeded (verify and smoke may skip only when the
  service runs no task). Job `visual-production` then runs `visual.yml` (public pages, report-only).
- Signed-in smoke (AA-910 design, built for AA-1073 / AA-1074; agent-run, no CI job yet):
  `frontend/e2e/SIGNED-IN-SMOKE.md`, specs `frontend/e2e/signed-in/` (Playwright projects `signed-in-setup` +
  `signed-in`, `npm run test:signed-in` with `AWS_PROFILE=aa365-admin`). E2E staff user
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
