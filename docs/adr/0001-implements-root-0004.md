# ADR 0001 — Implement root ADR 0004 (AA-Booking stack and bootstrap) in this repo

- **Status:** Accepted (14/10/2026, S235), with root ADR 0004 (accepted the same day). Was "Proposed; becomes
  Accepted with it" until the S243 index (AA-888) recorded the change.
- **Implements:** `AA-Ecosys/docs/adr/0004-aa-booking-stack-and-bootstrap.md` (decisions 1–9, the
  "Bootstrap scope" section), within ADR 0001 (ecosystem target architecture) and ADR 0002 (only CIS
  writes content).
- **Linear:** AA-678.

## Context

Root ADR 0004 decides the cross-repo parts: stack, repo layout, CI checks, deploy path, runtime in
AA-CIS-Infra, DB role and grants, the `bk-001` first slice and the health contract. Reversing it
touches this repo, AA-CIS-Infra and the CIS/TripPlanner grant migrations, so it lives in the root
repo. This ADR records only the choices made **inside** AA-Booking while implementing it.

## Decisions

1. **Layout** as in ADR 0004 decision 2, minus `frontend/` and `docs/erd/` for now (the frontend
   waits for O1/O8; ERD v2 stays in the root repo until it is moved there).
2. **Health contract** exactly as ADR 0004 decisions 4 and 9: `/booking/health` without a DB call;
   `/booking/health/ready` = `SELECT 1` + newest registered `bk-NNN` ≥ `REQUIRED_SCHEMA_VERSION`
   (in `backend/app/db.py`). The pool is lazy, so the process starts without a database.
3. **Readiness reads `booking.v_schema_versions`**, a `security_barrier` view in schema `booking`
   over `shared.schema_versions WHERE version LIKE 'bk-%'`, owned by the migration role. Role
   `booking` therefore needs no grant on schema `shared`, and the bootstrap needs no CIS migration.
   The view is read-only for the app (INSERT/UPDATE/DELETE revoked; a simple view is otherwise
   auto-updatable through its owner's rights).
4. **`bk-001` = ADR 0004 "Foundation" + "Tier A"** (customers, customer_otps, customer_sessions,
   media_files, audit_logs, inbox_events), no enum types. Tier B waits for O1/O6/O7.
5. **Role `booking` gets `USAGE` on schema `booking`, not `USAGE, CREATE`.** All DDL goes through
   `bk-NNN` applied by DB Migrate; an app role able to create objects would own them outside the
   migration path. `audit_logs` is append-only for the app.
6. **Migration guard** (`backend/tests/migration_guard.py`, run by the required Unit Tests check)
   enforces ADR 0004 decision 3 plus: no LOGIN/PASSWORD/role attributes, no extensions, no
   `SECURITY DEFINER`, grants only to `booking`, default privileges only
   `FOR ROLE aa_cis_admin IN SCHEMA booking`, every view revoked for writes.
7. **Integration Tests** run as `aa_cis_admin` on Postgres 15 with a stub `shared.schema_versions`,
   apply `migrations/*.sql` twice (idempotency), simulate the out-of-band LOGIN step with a CI-only
   password, assert role `booking`'s privileges, then call `/booking/health/ready` as `booking`.
   A skipped integration test fails the check.
8. **Security Audit check = pip-audit (failing) + gitleaks over the full git history.** Chosen over
   a type checker for the fifth check because this repo carries the DB credential flow.
9. **Deploy never changes `desired_count`** and is skipped (warning, not failure) until the `dev`
   environment variable `AWS_ROLE_ARN` exists. Images are tagged with the git SHA only (no
   `latest`), and `/booking/health` must report that SHA after rollout.
10. **SSL:** asyncpg always gets `ssl="require"` unless `ENVIRONMENT` is `test`/`local` and
    `DB_SSL=disable` (CI's Postgres has no TLS); any other combination refuses to start.

## Consequences

- The bootstrap is deployable with Infra phase 1+2 and `bk-001` alone — no CIS or TripPlanner
  migration is on its critical path.
- Every future view needs an explicit write revoke (the guard enforces it), and every new
  migration needs `REQUIRED_SCHEMA_VERSION` bumped in the PR whose code depends on it.
- Granting `CREATE` later, if ever needed, is a one-line migration.
