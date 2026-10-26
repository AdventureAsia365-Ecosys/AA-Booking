# Data coverage (AA-992) — Booking part: bk-025 contract views

The CIS admin page `/admin/data-coverage` (S252 design `.tmp-session/s252/datacov/design.md` §1.8, §3.2) shows Booking
counts and the trip plans whose tour left the live catalogue. Booking publishes them as contract views granted to
`cis_app`, the bk-017 pattern.

## Decisions

- **Number bk-025** (latest on main was bk-024; nothing else claimed 025).
- **Two views as designed**: `v_ops_data_totals (metric, n)` long format (a new metric is a view change, never a
  column change) and `v_ops_trip_plan_keys (source_system, source_id, sellable)`. The keys view is row-level, unlike
  the other `v_ops_*` views; it carries catalog keys only (design open question 6 / critic Q4: preferred over a
  `catalog.v_tour` grant to `bk_owner`).
- Copied bk-017 for the guard (`current_user = 'bk_owner'`, `cis_app` exists), the REVOKE of DML from `booking`, the
  `COMMENT ON VIEW` naming contract + reader and the registry row.
- Not added to `REQUIRED_SCHEMA_VERSIONS`: no Booking code reads the views; CIS degrades to `n/a` until applied.

## Changed

- `migrations/025_ops_data_coverage_views.sql` (new).
- `backend/tests/integration/test_bk025_ops_views.py` (new): contract column lists, owner/comment/registration, no
  `conf:`/`pii:`-tagged `trip_plans` column in the keys view (asserted against the live tags, not a hard-coded list),
  every metric present and counted from real rows, cis_app reads the views and gets 42501 on the tables, role
  `booking` cannot write them, guard refuses the master, re-apply is a no-op.
- `backend/tests/integration/test_bk017_ops_views.py`: the exact cis_app grant set gains the two bk-025 views.
- `CONTEXT.md`: Publishes paragraph + `bk-025` migration section.

## Tradeoffs

- `departures_open` / `contracts_published` / `rates_current` use `current_date` (DB server UTC); good enough for a
  coverage count, no time-zone parameter.
- No index: every table is small on Dev and the view is read at most once a minute.

## Should know

- Apply order: CIS rule-g allow-list PR → Infra copy (owners.yaml, isolation probe, RLS allow-list copy) → DB Migrate
  `check` → `apply` (booking). CIS shows the Booking rows as `n/a: contract_view_missing` until then.
- Local run: throwaway PG 16 cluster, `bootstrap_roles` then `pytest tests/integration` → 515 passed (incl.
  `test_migration_guard.py`).
