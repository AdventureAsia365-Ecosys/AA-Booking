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
| `POST /booking/trip-cases/{id_or_code}/versions` | New version (V1 from the TripPlanner legs, Vn copies the latest) |
| `GET`/`PATCH`/`DELETE /booking/trip-cases/{id_or_code}/versions/{n}` | Version header; edit `title`/`notes` (draft); soft-delete a draft |
| `GET`/`PUT /booking/trip-cases/{id_or_code}/versions/{n}/items` | Read / save items (upsert by `id`, removed items deleted) |
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

Integration tests need a throwaway PostgreSQL 15 database whose name ends in `_test` or `_ci`, with
a login named `aa_cis_admin` (the identity DB Migrate uses on Dev). The setup **drops** schema and
role `booking` in that database:

```bash
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

- `.github/workflows/ci.yml`: Lint (ruff), Security Audit (pip-audit + gitleaks, failing), Unit
  Tests (incl. migration guard), Integration Tests (Postgres 15, applies `migrations/`), Docker
  Build Check (image starts, Trivy report, SBOM).
- `.github/workflows/deploy-dev.yml`: on push to `main` (backend changes) — OIDC role from the `dev`
  environment variable `AWS_ROLE_ARN`, ECR push with the git-SHA tag, new task definition revision,
  update service, wait stable, verify image, smoke `/booking/health` + `/booking/health/ready`.
  Skipped until the variable is set.
