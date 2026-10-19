# AA-858 — Booking part: bk-017 ops overview contract views

Session S243 (19/10/2026), builder lane AA-858 step S1. The CIS admin overview (`/admin/overview`) shows a Booking
funnel. CIS must not read `booking.*` tables (X1 target, isolation probe), so Booking publishes aggregate views in its
own schema and grants them to the CIS runtime role (root ADR 0008 §5, same pattern as `tripplanner.v_inquiry_outbox`
→ `booking`).

## Decisions

- **Number bk-017**, not bk-016: bk-016 is AA-680 (`staff_users` Cognito columns, branch `feat/aa680-cognito-staff`),
  decided by the main session. `RESERVED_ON_OTHER_BRANCHES = {16: …}` in `tests/unit/test_migration_guard.py` keeps the
  contiguity test green on this branch; remove the entry when rebasing onto a main that has bk-016.
- **Two views, long format.** `v_ops_funnel_daily (day date, metric text, source_system text, n bigint)` and
  `v_ops_case_status (status text, n bigint)`. Long format so a new metric is a new row value, not a column change
  (contract `ops.overview.v1`, append-only metric names).
- **Milestones count first reach.** `proposal_sent` / `confirmed` / `cancelled` come from
  `trip_case_status_history` (trigger-written, never rewritten): one row per (case, status) at `min(changed_at)`. A case
  sent, reworked and sent again counts once; a cancelled, reopened and cancelled-again case counts once. A funnel asks
  "how many cases got this far", not "how many status writes".
- **Soft-deleted cases are left out everywhere** (`deleted_at IS NULL`), as in the staff list.
- **Inquiries** = `inbox_events` rows of contract `inquiry.submitted` (the only contract today; the filter keeps a
  future contract out). `inquiry_parked` / `inquiry_failed` use the row's current status, dated by `received_at`, so the
  overview can say "N inquiries parked / failed".
- **UTC days, last 400 days** in every branch: enough for "last 8 weeks" and a year-on-year glance, bounded so the
  view never scans unbounded history. Day boundary in ICT is a later choice for Ms. Thư (plan risk list).
- **Grants.** `GRANT USAGE ON SCHEMA booking TO cis_app` + `GRANT SELECT ON` the two views `TO cis_app`. Nothing to
  `aa_cis_admin`: the CIS runtime logs in as `aa_cis_admin` until X1 S18 and is a member of `bk_owner` (rl-001), so it
  already reads them as the owner's member; after S18 it is `cis_app` and the grant applies. A view reads its base
  tables with its owner's rights, so `cis_app` holds nothing on any table: `cis_app` → `booking.customers` stays 42501
  (probe cell `cross.cis_app.booking.customers`, and the integration test checks it through `SET ROLE cis_app`).
- **Role booking** keeps SELECT on the views (default privileges FOR ROLE `bk_owner` gave it DML; the file revokes
  INSERT/UPDATE/DELETE/TRUNCATE as the guard requires). The probe cell `runtime_grants.booking` (SELECT on every
  relation of `booking`) stays green.
- **Guard first-line check** raises when not run as `bk_owner` or when role `cis_app` is missing (rl-001).

## Changed (vs. the plan)

- The plan named the file `016_ops_overview_views.sql`; it is `017_ops_overview_views.sql` (session decision).
- The migration guard (`backend/tests/migration_guard.py`) forbade any grantee but `booking`. Loosened, narrowly:
  `CONSUMER_ROLES = {"cis_app"}` may get only `USAGE ON SCHEMA booking` or `SELECT ON booking.v_*` (rule
  `consumer-grant`; a table, another privilege, `ALL TABLES IN SCHEMA`, a function or another role still fail). 14 new
  parametrised guard cases.
- The integration bootstrap (`tests/integration/bootstrap_roles.py`) now also creates `cis_app` (NOLOGIN in CI; LOGIN
  on Dev) and grants it to `aa_cis_admin`, which mirrors Dev (`aa_cis_admin` is a member of `cis_app` in the schema
  reference); `conftest.py`'s role-shape check asks for that membership, so a stale local database fails with the
  "run bootstrap_roles" hint instead of the bk-017 guard error. CI runs the bootstrap every time, no workflow change.

## Tradeoffs

- **Views vs. an HTTP ops endpoint.** Rejected HTTP: Booking accepts only GitHub OIDC as a machine caller, and a
  CIS-minted machine token would collide with AA-680 removing the CIS → Booking bridge. Views cost nothing to run.
- **Plain views vs. a materialised table.** Plain: Dev has a handful of rows; CIS caches the section for 300 s.
  `EXPLAIN ANALYZE` on the local replay (empty tables): planning 0.9 ms, execution 0.2 ms; all branches are bounded
  by the 400-day filter and use existing indexes where the planner wants them (`trip_cases_status_idx`,
  `trip_case_status_history_case_idx`). Revisit with a materialised table only if the overview gets slow.
- **First reach vs. every transition.** First reach under-counts repeat sends on purpose; the Booking admin activity
  page still shows every status write.

## Should know

- **Apply order on Dev: bk-016 (AA-680) first, then bk-017.** DB Migrate applies the copies in AA-CIS-Infra
  `db/migrations/booking/` (branch `chore/aa858-copy-bk017-tp015`, made from this file byte for byte).
- No app code reads the views, so `REQUIRED_SCHEMA_VERSIONS` and `/booking/health/ready` are unchanged; no Booking
  deploy is needed.
- When the AA-CIS-Infra copy PR lands, the isolation probe gains the cell `consumer.contract_grants.bk-017` (pending
  until `bk-017` is registered, expected after).
- Local checks (S243): `ruff check .` + `ruff format --check .` clean; unit suite 725 passed (guard file 98 incl. the 14 new
  cases); integration suite on a throwaway PostgreSQL 16 with `bootstrap_roles`: 271 passed (5 new in
  `tests/integration/test_bk017_ops_views.py`). CI runs PostgreSQL 15 (the RDS major).
