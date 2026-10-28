# AA-996 follow-up (S256): list destinations from `catalog.v_tour_place_name`

S255 (#134) bounded the trip-plans list destinations read because `catalog.v_tour_place` costs ~10 ms a tour on Dev
(380 ms for 25 tours, 994 ms for 100). The real fix belonged to CIS, the owner of the catalog views
(`AA-996-destinations-budget.md`, "Should know"). CIS migration 298 (AA-CIS-App) adds `catalog.v_tour_place_name`:
the six columns `source_system, source_id, tour_id, day_number, position, place_name` of `v_tour_place`, row for row,
without the place card, destination and route segment lookups. SELECT is granted to `booking` only. This change makes
the list read it.

## Decisions

- **One-line read change.** `DESTINATIONS_SQL` reads `FROM catalog.v_tour_place_name p`; nothing else in the
  statement changes (same columns, same `tour_id = ANY($1::uuid[])` key, same first-seen order, `[1:8]` slice). The
  migration proves the rows are identical to `v_tour_place`'s (its closing check compares both views on every live
  tour), so the list shows the same places in the same order.
- **Bounds kept.** Chunks of `DESTINATION_CHUNK = 25`, `DESTINATIONS_BUDGET_MS = 2000`, the 5-minute cache: they stay
  as the safety net. Changing them waits for the live numbers after the CIS apply
  (`AA-Ecosys/.tmp-session/S256/aa996/live_place_name_budget.py`). If a 100-tour read is well under the 1 s extra
  timeout, a later change can raise the chunk to `MAX_PAGE` (one statement per page).
- **The list must never read `v_tour_place` again.** New guard `catalog_perf.LIST_FORBIDDEN_RE`
  (`catalog.v_tour_place` not followed by `_`), checked by a unit test on the SQL of `app/catalog/trip_plans.py` and
  by the integration test on every statement a 100-row list page runs.
- **`v_tour_place_name` is parse-backed** (it reads the same day text): added to `PARSE_BACKED`, so the static scan
  and the EXPLAIN guard check that every read of it is keyed by `tour_id`.

## Changed

- `app/catalog/trip_plans.py`: `DESTINATIONS_SQL` source view; module docstring and comments.
- `tests/catalog_perf.py`: `PARSE_BACKED` += `v_tour_place_name`; `LIST_FORBIDDEN_RE`.
- `tests/integration/stub_catalog.sql`: `catalog.v_tour_place_name` (security barrier, a projection of the stub
  `v_tour_place`, so a `tour_id` filter reaches the parse through both barriers) + its grant to `booking`.
- `tests/integration/test_aa996_destinations_budget.py`: reads counted on the new view; **budget assertion**: all
  destination reads of one 100-row page <= `STUB_PAGE_BUDGET_MS` (500 ms) on the stub; no statement of the page reads
  `v_tour_place`.
- `tests/unit/test_aa996_catalog_perf_guard.py`: keyed / unkeyed cases for the new view; the list reads the names view
  only. `tests/unit/test_trip_plans.py`, `tests/unit/test_aa996_destinations_budget.py`: scripted statements and error
  messages name the new view. `tests/integration/test_bk032_trip_plan_prices_api.py`: the unreadable-extra test
  revokes the grant on the new view (revoking `v_tour_place` no longer affects the list).
- `CONTEXT.md`: the catalog read rule names the new view.

## Tradeoffs

- **Ship order dependency.** Until CIS 298 is applied on Dev, the deployed read would fail (`UndefinedTable`) and the
  list would answer with `destinations: null` and `degraded: ["destinations"]` (fail-soft, no 503). No fallback to
  `v_tour_place` was added: the migration ships first (D8), and a fallback would hide a missing grant.

## Should know

- **Ship AFTER CIS 298 is applied on Dev** (CIS migration PR → Infra copy → DB Migrate apply → this PR).
- After deploy run `live_place_name_budget.py` in the Booking container: it compares the old and new views
  (time and identical places) for 25 / 100 tours and times a cold `list_plans(limit=100)`; it prints `BUDGET_OK` when
  the new view reads 25 tours in <= 150 ms and 100 in <= 500 ms.
- Integration tests need the CI Postgres (`BOOKING_TEST_ADMIN_DSN` as the RDS-like master); they were not run
  locally in S256 (no local `booking_ci` database). Unit tests and ruff pass locally.
