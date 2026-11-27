# AA-838 (S291 lane i-followups) — stable savepoint names on the Booking pools

The weekly C4.3 `pg_stat_statements` review (W48) found AA-Partner churning the 10,000 tracked statements with one
new `SAVEPOINT` / `RELEASE SAVEPOINT` text per nested transaction (asyncpg names savepoints from a process-wide
counter); AA-Partner #389 fixed it with a connection class that names savepoints by depth. This note covers the same
check for AA-Booking.

## Decisions

- **Booking does nest transactions**: about 60 functions open `conn.transaction()` on a connection their caller
  passes in, most of them on purpose as a savepoint inside the caller's transaction (fail-soft catalog reads in
  `app/catalog/*`, `app/b2b/*`, `app/supply/content.py`, the custom role read `app/auth/custom_roles.isolated`, the
  lifecycle moves `app/ops/lifecycle.py`, the season sync cut reads, the tenant erase steps). Each of them sent a new
  savepoint text, so the fix applies.
- **Port, not a shared package**: `app/pg_connection.py` is AA-Partner's `app/pg_connection.py` with prefix `bk_sp_`
  (the repos share no Python package; the class is 40 lines). Depth names, not one fixed name, keep the
  `ROLLBACK TO` semantics (see the module text).
- **Pools changed**: `app.db.Database.pool` (the API and the `booking.job` worker in the same process) and the Dev
  E2E fixtures pool (`app/e2e_fixtures.py`). No auth, bank, payout or approval code is edited: only the pool's
  connection class changes, every caller keeps its own `conn.transaction()`.

## Changed

- `app/pg_connection.py` (new), `app/db.py` and `app/e2e_fixtures.py` pass `connection_class=StableSavepointConnection`.

## Tradeoffs

- Relies on two asyncpg internals (`Connection._get_unique_id("savepoint")`, `Transaction._id`), pinned by
  `requirements.txt` (`asyncpg==0.31.0`); the unit test fails if an upgrade changes the statement texts.
- The one-off CLI scripts that open a single `asyncpg.connect` (`app/b2b/tenant_erase.py`, `app/b2b/legacy_cases.py`,
  `app/inquiry/leg_keys.py` main functions) are left as they are: a manual run adds at most a few texts once.

## Should know

- Tests: `tests/unit/test_s291_pg_connection.py` (statement texts, rollback depth, both pools) and
  `tests/integration/test_s291_pg_connection_pg.py` (rollback semantics on PostgreSQL through `app.db.Database`).
- Live check after deploy: `pg_stat_statements` rows for role `booking` whose query starts with `SAVEPOINT` /
  `RELEASE` / `ROLLBACK TO` stop growing; new ones read `bk_sp_<n>`.
