# AA-681 slice 3 (+ AA-680 staff half) — staff auth, trip case workflow, versions

Scope (S236 lane bk-681): migration `bk-003`, staff authentication against the CIS staff JWT (AA-680, Booking
side only), staff profile + admin endpoints, trip case read/edit/status/assign endpoints, proposal versions with
free-text itinerary items (V1 pre-filled from the TripPlanner legs). The CIS side (JWT issuer, JWKS endpoint) is
built in parallel in AA-CIS-App.

## Decisions

- **Staff JWT contract (O1).** RS256 only, `kid` header required, `iss` = CIS base URL, `aud` = `booking`,
  `exp` required (CIS issues 15 min tokens), 30 s leeway, `sub` = CIS `shared.admin_users.id` as a string, claims
  `email`, `name`, `cis_role` (`admin|reviewer|content`). Any other `cis_role` → 403 (token is genuine, but Booking
  has no mapping for it).
- **JWKS cache.** Fetched from `CIS_JWKS_URL` (default `https://api-cis.lumiguides.it.com/auth/.well-known/jwks.json`),
  cached 10 min; an unknown `kid` forces a refetch, at most once per 30 s (so random `kid`s cannot make Booking
  hammer CIS). A failed fetch keeps serving the stale key set; with no key set at all the request is 503
  (`auth_unavailable`), not 401, so a CIS outage is not reported as "bad token". The fetch uses stdlib `urllib`
  in a worker thread — no new HTTP client dependency at runtime. New runtime dependency: `PyJWT` + `cryptography`
  (same library CIS uses: `PyJWT[crypto]`).
- **Settings.** `CIS_JWKS_URL`, `STAFF_JWT_ISSUER` (default `https://api-cis.lumiguides.it.com`). Both must be
  `https://` unless `ENVIRONMENT` is `test`/`local`. Audience is the constant `booking`, not configurable.
- **Thin staff profile (O1).** `booking.staff_users` keyed by `cis_user_id TEXT` (CIS `admin_users.id` is a UUID
  today, but the key is the JWT `sub` string, so Booking never breaks if CIS changes its id type). No password.
- **Auto-provisioning on the first valid token.** `cis_role = admin` → Booking `admin`, active. `reviewer` →
  `sales`, `content` → `content_creator` (ERD v2 note "admin / reviewer / content map to admin / sales /
  content_creator"), both **inactive** → 403 `staff_inactive` until a Booking admin activates them. Provisioning
  happens only on INSERT: later logins refresh `email`, `name`, `cis_role`, `last_seen_at` but never change the
  Booking `role` or `active` (a Booking admin owns those). `last_seen_at` is written at most every 5 minutes.
  Every provisioning writes an `audit_logs` row (`actor_type 'system'`, action `staff.provisioned`).
- **Permission matrix in code** (`app/auth/staff.py`, `PERMISSIONS`), same pattern as CIS `rbac.ts`:
  `trip_case.read` = admin, sales, contracting; `trip_case.write` / `trip_case.assign` / `version.write` = admin,
  sales; `staff.admin` = admin. `content_creator` has no trip case access. **Provisional** — the PRD matrix
  should confirm it (open question).
- **Status workflow.** `trip_cases.status` stays `TEXT + CHECK` (13 ERD values). Allowed edges live in the
  read-only table `booking.trip_case_status_transitions`; a `BEFORE UPDATE OF status` trigger rejects any edge
  not in it (`check_violation`), an `AFTER INSERT OR UPDATE OF status` trigger writes
  `booking.trip_case_status_history`. Who/why come from transaction-local settings `booking.actor` /
  `booking.status_note` (`set_config(..., true)`), so the intake job's INSERT (no actor) records a system row.
  Seeded edges (pre-sale only):
  `new_inquiry → discovery → building → proposal_sent`, back-steps `building → discovery` and
  `proposal_sent → building` (customer asks for changes), and `→ cancelled` from each of those four.
  The post-sale edges (`proposal_sent → confirmed` … `on_trip → completed`) are **not** seeded: they gate on
  approval, supplier bookings and payments (AA-682) and would let staff mark a case `paid` with no payment.
- **Cancel needs a reason** (API 422 without one); other transitions take an optional reason.
- **Existing cases** get one backfilled history row (`NULL → current status`, note `bk-003 backfill`).
- **Assignments.** `staff_assignments(trip_case_id, staff_user_id → staff_users.cis_user_id, role, assigned_by,
  assigned_at, unassigned_at)`, role in `sales | contracting | ops | guide` (ERD note + contracting). At most one
  open assignment per case and role (partial unique index); re-assigning closes the previous one. Only active
  staff can be assigned. `cis_user_id: null` unassigns that role.
- **Versions.** `trip_case_versions` per ERD (`version_number`, `notes`, `total_price NUMERIC(12,2)` nullable,
  `currency`, `is_sent_to_customer` + `sent_at`, `is_approved` + `approved_at`, `created_by`), unique
  `(trip_case_id, version_number)`, partial unique `(trip_case_id) WHERE is_approved`. An approved version is
  locked by trigger (no UPDATE/DELETE of the version, no INSERT/UPDATE/DELETE of its items). The API also refuses
  to edit items of a version already sent to the customer (the customer saw it: make Vn+1).
- **V1 prefill.** V1 copies `trip_case_legs` ordered by `day_from, position`; title = the first inquiry
  snapshot day name for that `source_tour_id`, else `AA tour <source_tour_id>` (real titles come with the
  catalog, AA-679). Currency = the case currency. **Vn** copies the latest version's notes, total, currency and
  items. The case row is locked (`FOR UPDATE`) while numbering, so two concurrent creates cannot collide.
- **Items** are free text until AA-679: `position`, `title`, `day_from`, `day_to`, `source_tour_id TEXT NULL`,
  `notes`. PUT replaces the whole list (max 200 items), positions follow the list order.
- **Terminal cases** (`cancelled`, `completed`) refuse new versions and item edits (409).
- **Audit.** Every write (case patch, status, assign, version create, items put, staff patch, provisioning)
  writes one `booking.audit_logs` row with `before`/`after`. `audit_logs.actor_id` is a UUID, so bk-003 adds
  `actor_ref TEXT` (always the CIS user id) and fills `actor_id` only when the id parses as a UUID.
- **Readiness** requires `bk-003`.

## Changed (vs. the issue / ERD v2)

- `staff_users`: PK `cis_user_id TEXT` (not `id uuid`), `active` instead of ERD's `is_blocked`, added
  `cis_role` (last seen) and `last_seen_at`; role adds `contracting` (task brief) to ERD's three values.
- `trip_case_version_itinerary`: free-text slice (`title`, `day_from/day_to`, `source_tour_id`, `notes`,
  `position`); ERD's `day_number`, `date`, `activity_id`, `hotel_id`, `destination_id`, `activity_period`,
  `location_type`, `transfer_type`, `travel_hours`, `unit_price` wait for AA-679 / ADR 0005.
- `trip_case_versions`: + `sent_at`, `updated_at`; `created_by` is `TEXT → staff_users`.
- `trip_cases`: + `notes TEXT` (the PATCH brief edits notes; the ERD has none).
- `trip_case_status_transitions` is a table (ADR 0004 decision 7 Tier B named it), seeded with pre-sale edges only.
- No approve / send endpoints yet (not in the brief); the DB lock is in place for when they ship.
- Migration guard: `UPDATE OR` (a trigger event list, `BEFORE UPDATE OR DELETE ON ...`) was read as a DML target
  named `or`. `or` joins `of`/`on` in the skip list (all reserved words, never a table) with a test that the
  trigger target is still checked.

## Tradeoffs

- Transition rules in a table + trigger vs. only in Python: the DB enforces them for every writer (jobs, ad-hoc
  scripts), at the cost of a migration per new edge. The API reads the same table to answer "allowed next".
- Session settings for actor/reason vs. an explicit history INSERT in the API: the trigger never misses a status
  change, whichever code path makes it; the settings are transaction-local so pooled connections do not leak them.
- JWKS stale-on-error vs. fail-closed: a CIS blip does not log every staff user out; keys are still only those
  CIS published, and a revoked key disappears at the next successful fetch (≤ 10 min).
- Inactive-by-default for non-admin CIS users vs. auto-activating them: one manual step per new staff member, but
  a CIS content reviewer never sees customer data by accident.

## Should know

- Tests: unit (fakes) cover the Python branches incl. JWT verification with a local RSA key + fake JWKS;
  `tests/integration/test_bk003_workflow.py` runs every SQL statement, both triggers, the partial unique indexes,
  the role `booking` privileges and one HTTP flow (local JWT → provisioning → status → versions) on real Postgres.

- **Apply order:** `bk-003` through AA-CIS-Infra DB Migrate (byte copy `db/migrations/booking/003_staff_trip_case_workflow.sql`)
  **before** merging/deploying: readiness requires `bk-003`, and the deploy smoke test checks readiness.
- Staff endpoints answer 401/503 until CIS serves the JWKS (`/auth/.well-known/jwks.json`) and issues tokens with
  `aud: booking`. Nothing else in Booking depends on it.
- Optional env vars (defaults are the Dev values; set only to override): `CIS_JWKS_URL`, `STAFF_JWT_ISSUER`.
- The first Booking admin is whoever logs in first with CIS role `admin`; every other CIS user needs activation via
  `PATCH /booking/staff/{cis_user_id}`.
