# AA-996 follow-up: catalog read performance guard

Follow-up of the trip-plans list hotfix (#131). #130 read `catalog.v_tour_day` per page row through a `LATERAL` keyed
by `source_id`; on Dev each read parsed every live itinerary (~1.5 s), 25 of them ran past the pool's 5 s
`command_timeout` and the list answered 503. CI was green because the stub catalog tables were plain tables: a
`source_id` filter on them is as cheap as a `tour_id` one. This change makes that class of bug fail in CI.

## Decisions

- **The guard checks the plan, not the clock.** A CI database cannot be made slow enough to reproduce 1.5 s reads
  reliably, and timing assertions are flaky. What made #130 slow is a plan property: the filter on the parse-backed
  views (`v_tour_day`, `v_tour_place`, `_tour_day_text`) was not pushed down to the itinerary rows. The stub now has
  the same property as the live views, and the guard reads it from `EXPLAIN`.
- **Stub fidelity:** `catalog._tour_day_text` in the stub is a `security_barrier` view with
  `DISTINCT ON (tour_id, day_number)` over the stub day rows (`catalog.stub_parsed_day`) plus a fixed set of noise
  itineraries (`catalog.stub_parsed_day_noise`, tours that are not in `v_tour`). `v_tour_day` and `v_tour_place`
  are `security_barrier` views over it, as on Dev (CIS 225 / 256 / 289). Postgres pushes a `tour_id` qual (a
  DISTINCT ON key, leakproof `=`) through to the base scans; a `source_id` qual stays above the `Unique` node, so
  the base scans read every row, exactly the Dev behaviour.
- **Three layers:**
  1. Suite-wide (every integration test, so every Booking route a test exercises): an autouse fixture adds a query
     recorder to every `app.db.Database` the tests build; at teardown each new statement that reads a parse-backed
     view is `EXPLAIN`ed (no ANALYZE, nothing runs) and any scan of a stub itinerary table without a `tour_id`
     condition fails the test.
  2. Route test for the list that broke (`test_aa996_catalog_perf_guard.py`): 25 tours on the page, `EXPLAIN
     ANALYZE` of every catalog statement of the request; the itinerary rows read must stay below the noise size
     (an unpushed read reads all of it, per loop), and the number of statements must not grow with the page
     (limit 2 vs 25: no N+1).
  3. Unit (no DB): a static scan of every SQL string in `app/` for parse-backed views keyed by `source_id`, and the
     query count of `list_plans` / `list_orphaned` for a page of 1 vs 25 rows.
- **Timing budget log (runtime):** every pool connection gets a query logger (`app.db.slow_query_logger`); a
  statement at or over `SLOW_QUERY_MS` (1 s) logs `slow_query duration_ms=… budget_ms=… catalog=… error=… sql=…`
  (no arguments: they can hold personal data). A read cut by `command_timeout` logs too (`error=TimeoutError`), so
  the #130 class shows in CloudWatch the first time it happens instead of only as a 503.
- **Proof the guard fires:** `test_the_guard_fails_the_130_list_statement` feeds the exact #130 `LATERAL` statement
  (kept as a constant in the test) to the plan check and expects a violation; the unit static scan has the same
  proof. Both also pass the current hotfix statements.

## Changed

- `app/db.py`: `Database(settings, query_loggers=())`; the pool `init` adds `slow_query_logger` and the extra
  loggers to each new connection.
- `tests/integration/stub_catalog.sql`: day rows live in `catalog.stub_parsed_day`; `v_tour_day` is a view with
  INSTEAD OF INSERT / DELETE triggers so the tests keep writing day rows the same way. Tests that `TRUNCATE`d
  `catalog.v_tour_day` now truncate `catalog.stub_parsed_day` (a view cannot be truncated).
- New `tests/catalog_perf.py` (recorder, static scan, plan check), `tests/unit/test_aa996_catalog_perf_guard.py`,
  `tests/integration/test_aa996_catalog_perf_guard.py`, the autouse guard in `tests/integration/conftest.py`.

- `CONTEXT.md`: the parse-backed read rule (by `tour_id`, once per page) and the `slow_query` log line.

## Evidence (local, Postgres 16 throwaway cluster; CI runs Postgres 15, same planner rules for this)

- With the current code: unit suite 1892 passed, coverage 92.25 %; integration suite 529 passed (4 min 18 s), the
  suite-wide guard explained both parse-backed reads the app has (detail days by `tour_id`, list destinations by
  `tour_id = ANY`).
- With `app/catalog/trip_plans.py` temporarily put back to the #130 version (`git show 81fd84c^:…`): the static
  scan fails (4 findings, line 96 `v_tour_day` filtered by `source_id`), the route tests fail (unfiltered scans of
  `stub_parsed_day` and `stub_parsed_day_noise`), and every trip-plan integration test that lists or opens a plan
  fails at teardown with `catalog read parses every itinerary (AA-996 perf guard …)`, naming the #130 statement and
  the `_days` read by `source_id`. The file was restored right after.
- `test_the_guard_fails_the_130_list_statement` keeps that proof in CI: the #130 statement for 5 page rows feeds
  ≥ 5 × 800 itinerary rows into the parse; the hotfix destinations read for 25 tours feeds exactly their 75 days.

## Tradeoffs

- The plan check only looks at the parse-backed views. `catalog.v_tour` / `v_tour_photo` are read by `source_id`
  in several places and that is fine live (CIS 258 made `source_key` a stored, indexed column); checking them would
  need a faithful stub of `_live_tour` for no known risk.
- No CI timing assertion: the stub is small and CI runners vary; the runtime log covers timing.
- The unit N+1 check (page of 1 vs 25 issues the same statements) would not have caught #130 by itself: #130 was ONE
  statement with a per-row `LATERAL`. Only the plan check does; the count check guards the other way to get it wrong
  (a Python loop over the page).
- The suite-wide check dedupes by SQL text for the whole session, so a statement is explained once (cheap: a
  handful of distinct statements).

## Should know

- Live check (read only, orchestrator): `.tmp-session/S255/aa-996/live_catalog_budget.py` in the root workspace
  (not in this repo) runs the real read functions of the deployed code (trip plans list / detail / facets /
  orphaned, liveness, geo, place search, B2B products, desk titles) as role `booking` inside a READ ONLY transaction
  and prints the time of every statement; anything ≥ 1 s is `OVER_BUDGET`. This covers the catalog reads the stub
  cannot model (`v_tour`, `v_tour_retired`, `v_destination*`, `v_country` on the live views).

- Writing a new read of `v_tour_day` / `v_tour_place`: key it by `tour_id` (`= $1` or `= ANY($1::uuid[])`), get the
  `tour_id` from `catalog.v_tour` first if you only have the `source_id`. The guard names the statement and the
  unfiltered scan when it fails.
- Run the integration tests locally without docker: `initdb` a throwaway cluster on another port (the Postgres 16
  binaries in `/usr/lib/postgresql/16/bin`), `python -m tests.integration.bootstrap_roles <superuser DSN>`, then
  `BOOKING_TEST_ADMIN_DSN=… python -m pytest tests/integration`.

## Review fixes (independent review, S255)

- The static scan accepted `d.tour_id = t.tour_id` (a JOIN) and `tour_id IN (SELECT …)` as "keyed by tour_id". On
  the stub `EXPLAIN` shows both hash the whole DISTINCT ON view (unfiltered scans of `stub_parsed_day` and
  `stub_parsed_day_noise`), i.e. the #130 class. The scan now requires the `tour_id` comparison to be against a bind
  parameter (`= $n`, `= ANY($n…)`, `IN ($n, …)`), matching the rule in CONTEXT.md; the unit cases that asserted the
  join and the subquery as fine now assert them as findings.
- The plan check counted any `tour_id` text in a scan condition (e.g. `tour_id IS NOT NULL`) as keyed; it now needs
  an equality (`tour_id = …`).
