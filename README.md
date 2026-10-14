# AA-Booking

Booking back office of the Adventure Asia ecosystem (AAA): customer identity (OTP), trip cases from
TripPlanner inquiries, proposals, suppliers, invoices and payments. Runs on the shared acc2
infrastructure (ECS on `aa-cis-dev-cluster`, schema `booking` on the shared RDS).

**Status:** bootstrap health slice (AA-678). Start with [CONTEXT.md](CONTEXT.md); the design is
root ADR 0004 (`AA-Ecosys/docs/adr/0004-aa-booking-stack-and-bootstrap.md`), implemented as described
in [docs/adr/0001-implements-root-0004.md](docs/adr/0001-implements-root-0004.md).

## Endpoints

| Path | Purpose |
|---|---|
| `GET /booking/health` | Liveness, no DB. ALB health check. `{status, service, git_sha}` |
| `GET /booking/health/ready` | DB reachable + required `bk-NNN` registered. 200 or 503. `{status, db, schema_version}` |

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
