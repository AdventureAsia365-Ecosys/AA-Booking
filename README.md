# AA-Booking

Booking back office of the Adventure Asia ecosystem (AAA): customer identity (OTP), trip cases from
TripPlanner inquiries, proposals, suppliers, invoices and payments. Runs on the shared acc2
infrastructure (ECS on `aa-cis-dev-cluster`, schema `booking` on the shared RDS).

**Status:** bootstrap health slice (AA-678), inquiry intake (AA-681 slice 2, `bk-002`), staff
authentication with the CIS staff JWT + trip case workflow and proposal versions (AA-680/AA-681 slice 3,
`bk-003`), Wave 1 staff admin backend (`bk-005`: customers, notifications, send / approve, dashboard). Start with [CONTEXT.md](CONTEXT.md); the design is
root ADR 0004 (`AA-Ecosys/docs/adr/0004-aa-booking-stack-and-bootstrap.md`), implemented as described
in [docs/adr/0001-implements-root-0004.md](docs/adr/0001-implements-root-0004.md).

## Endpoints

| Path | Purpose |
|---|---|
| `GET /booking/health` | Liveness, no DB. ALB health check. `{status, service, git_sha}` |
| `GET /booking/health/ready` | DB reachable + required `bk-NNN` registered. 200 or 503. `{status, db, schema_version}` |
| `GET /booking/staff/me` | Staff profile + permissions (CIS staff JWT; first token provisions the profile) |
| `GET /booking/staff`, `PATCH /booking/staff/{cis_user_id}` | Admin: list staff, activate / set Booking role |
| `GET /booking/staff/me/preferences`, `PUT …` | Own notification toggles `{notify: {type: bool}}` |
| `GET /booking/staff/directory?role=` | Active staff `{cis_user_id, name, role}` for pickers (admin, sales, contracting) |
| `GET /booking/staff/{cis_user_id}` | Profile + open assignments (admin, or self) |
| `GET /booking/trip-cases` | List (`status` comma list, `assignee` = id \| `me` \| `none`, `q`, `source`, `customer_id`, `created_from/to`, `sort`, `limit`, `offset`); rows carry `trip_title`, `leg_titles`, `countries` |
| `POST /booking/trip-cases` | Staff-created case (`customer_id` xor `new_customer`, pax split, currency, countries, assignee, `initial_status`) |
| `GET /booking/trip-cases/summary` | Dashboard counts (open, new inquiries, mine, proposals awaiting, by status, month-over-month) |
| `GET /booking/trip-cases/{id_or_code}` | Case + customer + legs + inquiry snapshot + versions (badge) + status history + assignments |
| `GET /booking/trip-cases/{id_or_code}/activity` | Status history + audit rows, newest first (`limit`, `before`) |
| `PATCH /booking/trip-cases/{id_or_code}` | Edit `pax`, `pax_adults`, `pax_children`, `start_date`, `currency`, `trip_style`, `notes`, `countries` |
| `POST /booking/trip-cases/{id_or_code}/status` | Status transition `{to_status, reason}` (cancel and reopen need a reason; `confirmed` only via approve) |
| `POST /booking/trip-cases/{id_or_code}/assign` | `{cis_user_id \| null, role}` |
| `POST /booking/trip-cases/{id_or_code}/versions` | New version (V1 from the TripPlanner legs, Vn copies the latest except its total: a new version starts unpriced) |
| `GET`/`PATCH`/`DELETE /booking/trip-cases/{id_or_code}/versions/{n}` | Version header; edit `title`/`notes` (draft); soft-delete a draft |
| `GET`/`PUT /booking/trip-cases/{id_or_code}/versions/{n}/items` | Read / save items (upsert by `id`, removed items deleted) with their net cost lines (`costs[]` replaces an item's lines; omit to keep them). A change clears the version price |
| `POST /booking/trip-cases/{id_or_code}/versions/{n}/price` | Price a draft from its stored cost lines `{channel, currency, peak, override_total?, override_reason?}` (same engine as quote-preview); saves total, currency, channel and the selling FX rate. Below the floor needs a reason |
| `POST /booking/trip-cases/{id_or_code}/versions/{n}/send` | Mark sent `{to?, subject, message, valid_until?}`: locks the version, case → `proposal_sent`, customer notification row (no e-mail) |
| `POST /booking/trip-cases/{id_or_code}/versions/{n}/approve` | Record the customer's approval `{note?}`: version approved, case → `confirmed` |
| `GET /booking/versions` | Cross-case versions (`q`, `badge`, `assignee`, `updated_from/to`, `sort`) |
| `GET`/`POST /booking/customers` | Customers with derived status (`q`, `status` lead\|active\|past\|blocked, `merge_review`, `source`, `sort`); new manual lead |
| `GET`/`PATCH`/`DELETE /booking/customers/{id_or_code}` | Detail (cases, inquiries, activity); edit / block (admin); archive (admin, soft) |
| `GET /booking/notifications`, `POST /booking/notifications/read` | Staff feed (`unread`, `type`, `q`, `before`) + `unread_count`; mark `{ids}` or `{all: true}` read |
| `GET /booking/settings`, `GET /booking/countries` | Config rows (`deposit_pct`, `quote_validity_days`), ISO country list |
| `GET /booking/audit-logs` | Admin audit log (`actor`, `entity_type`, `entity_id`, `from`, `to`, `before`) |

Staff routes need `Authorization: Bearer <CIS staff JWT>` (`aud=booking`); see
[CONTEXT.md](CONTEXT.md#staff-authentication-and-permissions-aa-680-adr-0004-o1).

## Local development

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest tests/unit
```

Integration tests need a throwaway PostgreSQL 15 database whose name ends in `_test` or `_ci`. They log
in as `aa_cis_admin` (the identity DB Migrate uses on Dev) **without superuser**, like the RDS master, so
first give the database the RDS role shape with a superuser DSN (creates `aa_cis_admin` NOSUPERUSER
CREATEROLE, member of `bk_owner`; `bk_owner` NOLOGIN; `booking` LOGIN; X1 plan S5). The setup **drops**
schemas `booking`, `tripplanner` and `catalog` in that database:

```bash
.venv/bin/python -m tests.integration.bootstrap_roles \
  postgresql://postgres:<pw>@localhost:5432/booking_ci postgresql://aa_cis_admin:<pw>@localhost:5432/booking_ci
BOOKING_TEST_ADMIN_DSN=postgresql://aa_cis_admin:<pw>@localhost:5432/booking_ci .venv/bin/pytest tests/integration
```

Run the API locally (liveness works without a database):

```bash
ENVIRONMENT=local .venv/bin/uvicorn app.main:app --port 8000
curl -s localhost:8000/booking/health
```

## Migrations

`migrations/NNN_name.sql`, version key `bk-NNN`. Review here, copy byte-for-byte to
`AA-CIS-Infra/db/migrations/booking/`, then run DB Migrate (`check`, then `apply`) on AA-CIS-Infra
`main`. Rules and the guard: [CONTEXT.md](CONTEXT.md#database-rules).

## CI / deploy

- `.github/workflows/ci.yml`: Lint (ruff), Security Audit (AA-902 resource-ID check + pip-audit, failing), Unit
  Tests (incl. migration guard), Integration Tests (Postgres 15, applies `migrations/`), Docker
  Build Check (image starts, Trivy report, SBOM).
- `.github/workflows/secret-scan.yml` (AA-981 follow-up): check **gitleaks** on every PR (lines the PR adds, rules and
  baseline from the base branch) and **gitleaks full history** (`secret-scan-history.yml`) nightly, on push to `main` and on demand (every
  branch, tag and pull request head). Rules in `.gitleaks.toml` (same as the root repo), reviewed findings pinned
  in `.gitleaksignore`; a PR that changes either file, either secret scan workflow, `.github/CODEOWNERS` or any
  `.gitattributes`, or adds an inline `gitleaks:allow`, needs the label `secret-scan-baseline-reviewed` after the last push (any later push, reopen or label change
  removes it). Merge commits (remerge diff), renames and type changes are scanned too, every file as text. A red
  result means the value is public: rotate it, even if it was force-pushed away. False positives: prefer a
  narrow allowlist in `.gitleaks.toml`; a commit fingerprint does not survive the squash merge (header of
  `.gitleaksignore`).
  The PR check runs on `pull_request_target`: GitHub always runs the base branch's copy of the workflow and the PR
  head is only read as data, so a PR cannot change the check that judges it. It also scans a squash preview of the PR
  (what the squash merge writes), and it fails when another workflow has a job named `gitleaks`. `.gitleaks.toml`
  carries a copy of the gitleaks v8.30.1 default rules without gitleaks' global path allowlist, so text files such as
  `.svg`, `go.sum` or `node_modules/` are scanned; a text file under a binary-format name (`.png`, `.pdf`, ...) needs
  the label.
- `.github/workflows/deploy-dev.yml`: on push to `main` (backend changes) — OIDC role from the `dev`
  environment variable `AWS_ROLE_ARN`, ECR push with the git-SHA tag, new task definition revision,
  update service, wait stable, verify image, smoke `/booking/health` + `/booking/health/ready`.
  Skipped until the variable is set.
- **Frontend deploys:** CI-created via `.github/workflows/vercel-deploy.yml` (only when `frontend/**`
  changes: PR → preview, `main` → production); the Vercel Git integration is off via
  `frontend/vercel.json` (`git.deploymentEnabled: false`). Needs the repo secret `VERCEL_TOKEN`.
