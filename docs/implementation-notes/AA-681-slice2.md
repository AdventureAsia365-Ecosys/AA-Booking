# AA-681 slice 2 — Inquiry intake (TripPlanner `inquiry.submitted` → trip case)

Scope (S235, decisions approved by Nghiep 14/10/2026: ADR 0004 O3/O6/O8/O9, `AA-Ecosys/docs/contracts/README.md`,
contract `inquiry.submitted.v1.1`, ecosystem blueprint "Lát 2 Nhận yêu cầu"): migration `bk-002`, a `booking.job`
worker inside the API process, jobs `bk_inquiry_pull` and `bk_inquiry_process`, lead customers, trip cases + legs,
a staff notification row. The TripPlanner grant is `tp-009` in AA-TripPlanner-Web (separate PR).

## Decisions

- **Own job table `booking.job` (O3)**, same columns and claim/lease/reap/backoff semantics as CIS `shared.job`
  (`shared/jobs/queue.py`), reimplemented in `backend/app/jobs/` (about 150 lines) rather than imported: CIS's
  library is not a package and hard-codes `shared.job`. Left out until a Booking job needs them: `progress`,
  `cost_usd`, `cancel_requested`, `parent_job_id`, the `job_worker` registry and per-kind concurrency caps.
- **Kinds are `bk_*` (O9)**, enforced by `CHECK (kind ~ '^bk_[a-z0-9_]+$')`.
- **Worker runs inside the API process on Dev** (lifespan background task, `WORKER_ENABLED`, default on when
  `ENVIRONMENT=dev` and `DATABASE_URL` is set). It is not in a request path (ADR 0004 decision 8). One job at a
  time. Two overlapping tasks during a rolling deploy are safe: `FOR UPDATE SKIP LOCKED` claims, a singleton index
  for the recurring pull, and idempotent handlers. A separate worker service (`python -m ...`) is a later Infra
  change if the API ever needs to scale independently.
- **Recurring pull = singleton row.** A partial unique index allows at most one queued/running `bk_inquiry_pull`;
  every 15th worker tick (about 30 s idle) the worker runs maintenance: reap expired leases, `INSERT ... ON CONFLICT
  DO NOTHING` the next pull 60 s ahead, prune finished pull runs older than 7 days. So intake latency is about
  60–90 s.
- **Cursor (contract rule 4).** The pull anti-joins `tripplanner.v_inquiry_outbox` against `booking.inbox_events`
  (the correctness mechanism) and uses `booking.inbox_cursor.safe_id` only as a scan bound. `safe_id` advances only
  after a drained scan, and only over rows with `created_at` older than `SAFE_AFTER_SECONDS` = 2 × 300 s. Reason:
  `created_at` is the producer transaction's start, its id is drawn later; if a hand-off transaction lasts < 300 s,
  any row older than 600 s drew its id before any still-invisible row began, so no lower id can still appear. Scan
  and advance run in one REPEATABLE READ snapshot.
- **Pull stores raw, process validates.** The pull never rejects a row (every outbox row lands in the inbox, keyed
  by `idempotency_key`), and queues one `bk_inquiry_process` job per new row in the same transaction
  (job idempotency key `bk_inquiry_process:<inbox_event_id>`).
- **Inbox outcome column** `inbox_events.status`: `received` → `processed` | `parked` | `failed`.
  `processed_at` is set for every final state; `error` holds the reason for `parked`/`failed`.
- **Validation** with `jsonschema` against byte copies of the root contract files in `backend/app/contracts/`
  (SHA-256 pinned in a unit test). Version `1.1` → v1.1 schema. Version `1` → v1 **or** v1.1 schema, because
  TripPlanner main (PR #69) already emits the v1.1 shape (`verified`, nullable `customer_id`) labelled `version: 1`.
  Any other version / shape → `failed`, job succeeds (the inbox row is the record staff read).
- **Leads (O6).** Every processed inquiry creates a **new** `booking.customers` row with `verified = false`,
  `source = 'tripplanner_lead'`, consent copied, `external_ref` = TripPlanner `customer_id` when present.
  - It is never merged into an existing customer (contract v1.1). When its email (case-insensitive) or phone
    matches a live customer, the lead gets `needs_merge_review = true` and `merge_candidate_id` (a verified match is
    preferred). Staff, or the future OTP sign-in (AA-680), decide the merge.
  - Only `customer.verified = true` **and** a verified customer with the same email links the trip case to that
    customer. A `verified: true` without such a customer still creates an *unverified* lead: verification is
    Booking's OTP, a producer claim cannot make a Booking customer verified.
  - Repeat inquiries from the same person create one lead each (no lead-to-lead merge either); all of them are
    flagged against the first match.
- **Email/phone uniqueness now covers verified live customers only** (bk-001 covered all live rows). Leads must be
  able to repeat an email — that is the case that must not be merged.
- **Parked, not trip cases:** `customer: null` (legacy guest, v1) and a contact with neither email nor phone.
- **Trip case:** `code` = `TC-` + 6 digits from `booking.trip_case_code_seq`; `status = 'new_inquiry'`;
  `source_system = 'tripplanner'`, `source_id` = TripPlanner `trip_id`, `idempotency_key` UNIQUE (second guard
  after the inbox row lock); `inquiry_snapshot` keeps `days` + `custom_days` for staff; legs normalised into
  `trip_case_legs` (sorted by `position`, UNIQUE per case).
- **Staff notification** = one `booking.notifications` row (`recipient_type 'staff'`, `recipient_id NULL` = the
  shared sales queue until staff identity O1) + an INFO log line with ids only (no e-mail/phone in logs) + an
  `audit_logs` row (`actor_type 'system'`). No e-mail yet.
- **No `GET /booking/trip-cases` endpoint** in this slice. Staff authentication (O1: staff use the CIS login/JWT)
  does not exist in Booking yet, and an endpoint that rejects every caller adds nothing. Staff read trip cases
  through the DB until the staff surface ships.
- **`REQUIRED_SCHEMA_VERSION` = `bk-002`**: readiness is 503 until bk-002 is applied, which is the signal that the
  worker's tables exist.

## Changed (vs. the issue / ERD v2)

- ERD v2 types `trip_case_legs.source_tour_id` as `uuid`; it is `TEXT` here because the contract types it as a
  string and an inquiry must never be rejected over an id format.
- Status columns are `TEXT + CHECK`, not enum types (bk-001 rule: cheap to change while the vocabulary is open).
  `trip_cases.status` allows ERD v2's 13 values; only `new_inquiry` is written.
- `notifications` was planned for bk-003; a minimal version ships now (ERD columns + `entity_type/entity_id/data`,
  `recipient_id` nullable for the staff queue).
- Not in bk-002: `trip_case_countries` (needs `country_profiles`, O7), `trip_case_status_history` + transition
  trigger (status vocabulary), `trip_plan_id` (AA-679), staff tables (O1).

## Tradeoffs

- In-process worker vs. a worker service: no Infra change and no extra Fargate task on Dev, at the cost of sharing
  the API's 5-connection pool and lifecycle. The pull is one short transaction per minute; acceptable for Dev.
- One lead per inquiry vs. reusing an existing unverified lead with the same email: duplicates are visible and
  reviewable, while an automatic reuse could attach someone else's trip to the wrong contact (the S235 identity bug
  TripPlanner fixed in PR #69).
- Invalid payload → job *succeeds* with outcome `failed` on the inbox row, instead of a failed job: the inbox row is
  the durable record and retrying a deterministic validation error is useless.
- Vendored schema copies vs. reading the root repo at runtime: the image is self-contained; drift is caught by the
  SHA-256 test, but a new contract version needs a manual copy.

## Should know

- **Apply order:** TripPlanner `tp-009` (grant; needs role `booking`, which exists since bk-001) and Booking
  `bk-002` through AA-CIS-Infra DB Migrate (byte copies: `db/migrations/tripplanner/009_*.sql`,
  `db/migrations/booking/002_inquiry_intake.sql`), **then** merge/deploy this code. Without tp-009 the pull job
  fails with `InsufficientPrivilegeError` and is retried (then rescheduled) — no data is lost, it catches up once the
  grant exists.
- `customers` rows created before bk-002 (none expected on Dev) become `verified = false`, `source = 'manual'`.
- New env var for the task definition (optional, has a default): `WORKER_ENABLED`. Nghiep sets it in Infra only
  to turn the worker off on Dev (`false`).
- TripPlanner should bump `SCHEMA_VERSION` in `backend/assembly/handoff.py` to `1.1` (it already sends that shape);
  Booking accepts both.
- Local tests: `BOOKING_TEST_ADMIN_DSN=postgresql://aa_cis_admin:x@localhost:55432/booking_ci` against a throwaway
  `initdb -U aa_cis_admin --auth=trust` cluster (Postgres 16 locally; CI uses 15).
