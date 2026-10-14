# AA-Booking — agent instructions

- Read `CONTEXT.md` first, then root ADR 0004 (`AA-Ecosys/docs/adr/0004-aa-booking-stack-and-bootstrap.md`).
  Code that disagrees with CONTEXT.md is a question to raise, not to ignore.
- Commits: `feat|fix|chore|refactor|test|docs: <english>`, body `Refs AA-xxx`. Never `[AA-xx]` in the
  subject, never `Fixes`/`Closes` (Linear auto-closes on those).
- Agents never merge, push to `main`, create repos, apply Terraform, run DB Migrate or write secrets.
  A human merges after the 5 required checks are green.
- Migrations: `migrations/NNN_*.sql` (key `bk-NNN`); the migration guard must pass; never put a
  password, LOGIN or a grant outside schema `booking` in a migration. Applied only through
  AA-CIS-Infra DB Migrate.
- DB access: asyncpg, plain SQL, `ssl="require"`. Do not guess table/column names — read the
  migration files or `AA-Ecosys/docs/architecture/db-schema-reference.md`.
- Underscore environment variable names are set by Nghiep in the GitHub/AWS UI; list them, do not
  set them.
- Everything in Git is English. Keep per-issue decisions in `docs/implementation-notes/<ISSUE>.md`
  while working.
