# AA-996 follow-up (S255): bounded trip-plans list destinations

The live budget check of 28/10 (role `booking` on Dev, deployed code after #131 / #132) timed every catalog read
of the trip-plans list. All statements are keyed correctly (`tour_id = ANY`), but the destinations read on
`catalog.v_tour_place` took **380 ms at limit 25** and **994 ms at limit 100**. Its `statement_timeout` is 1 s
(`EXTRA_TIMEOUT_MS`, #131), so a page of 100 was at the fail-soft edge: one slower day and the whole page loses its
destinations (`degraded: ["destinations"]`).

## Decisions

- **The cost is in the CIS view, not in the Booking SQL.** `v_tour_place` (CIS 253 / 256 / 289) is ~10 ms per tour
  even with the filter pushed down: per tour it parses the day text (`_tour_day_text`), reads the atoms and place
  cards, and per place runs three `LEFT JOIN LATERAL` lookups (place pipeline link in `geo.tour_place`, the legacy
  `shared.destinations` name match, the chosen destination) plus the `geo.tour_segment` lookup. Booking reads only
  `tour_id`, `day_number`, `position` and `place_name`, but Postgres does not drop unused `LATERAL … LIMIT 1` joins.
  Lesson S238 (TripPlanner, migration 235) measured 78 ms for 24 tours before 256 added the geo lookups.
- **Booking-side fix (this PR), three bounds:**
  1. **Chunks:** at most `DESTINATION_CHUNK = 25` tours per statement (~0.4 s on Dev today, 2.5x headroom under
     the 1 s timeout). A page of `MAX_PAGE = 100` is 4 statements, not one per row; a page of 25 or fewer stays one.
  2. **Page budget:** all chunks of one page share `DESTINATIONS_BUDGET_MS = 2000` of wall time. Each chunk runs
     under `min(1000, what is left)`; a chunk that cannot get `MIN_CHUNK_MS = 100` is not started. So the
     destinations of one request never take more than 2 s, whatever the view does.
  3. **Cache:** a per-process TTL cache (`DESTINATIONS_TTL_S = 300`, at most 4096 tours) keyed by `tour_id`.
     `tour_id` is one CIS version (a re-upload is a new key), so the cache never shows a newer version's places
     for an older one; the TTL bounds how long an atom/place edit inside the same version takes to reach the list.
     The detail page still reads its days live. A repeat of the same page reads no destinations at all.
- **Partial results instead of all-or-nothing:** a chunk that fails (timeout, grant, missing view) stops the read
  (the next chunk would fail the same way) and leaves only the unread rows `null`; rows already read keep their
  places, and `degraded` still names `destinations`. A failed read is never cached; an empty result is cached as `[]`.
- **SQL returns at most `MAX_DESTINATIONS` (8) places per tour** (`[1:8]` on the array): the list never showed more.
- **Budget 2 s, not 1.5 s:** with 4 chunks at ~0.4 s, 1.5 s would cut the last chunk of a cold 100-row page on Dev
  today. Worst case of the whole list request stays well under the pool's 5 s per-statement `command_timeout`
  (count + page + destinations <= 2 s + cover photo <= 1 s + from price <= 1 s).

## Changed

- `app/catalog/trip_plans.py`: `TtlCache`, `DESTINATIONS_CACHE`, `_destinations` (chunks + budget + cache),
  `_now` (clock seam for tests), `_extra(timeout_ms=…)`, `_degraded` (partial destinations count as degraded),
  `_list_item` (`None` per row = not read; a tour the read did not return = `[]`), `DESTINATIONS_SQL` slice.
- New log line `trip_plan_list_extra_partial extra=destinations reason=budget budget_ms=… unresolved=…` when the
  budget leaves chunks unread (the existing `trip_plan_list_extra_unavailable` still logs a failing chunk).
- Tests: `tests/unit/test_aa996_destinations_budget.py` (fake clock: chunking, the 2 s worst case, the cut chunk,
  not-started chunks, failure mid-page, cache hit / partial hit / expiry / bound), `tests/integration/
  test_aa996_destinations_budget.py` (100 tours on the stub: 4 chunks of <= 25, each under its own timeout <= 1 s,
  `EXPLAIN ANALYZE` keyed by `tour_id` and reading only the chunk's days, no statement >= 250 ms; the second read
  of the page reads no destinations). Both conftests clear the cache around every test.
- `CONTEXT.md`: the catalog read rule notes the chunk / budget / cache.
- No API shape change: `destinations` was already nullable per row; it can now be `null` on some rows only.

## Tradeoffs

- **Cache vs O7 ("content is CIS's, read at request time"):** the cache is in memory, per process, 5 minutes, keyed
  by the content version; nothing is stored in Booking's database. A tour that leaves the catalog still drops out on
  the next read (the page query is live). Accepted for a list column; not used for the detail or for proposals.
- **Chunks run one after another on the request's connection** (no parallel pool connections): simpler, and the
  page budget bounds the total. Parallel reads would cut a cold 100-row page to ~0.4 s but take 4 pool connections
  per request.
- **Lighter source rejected for now:** `catalog.v_tour_day_route` (geo `tour_day.places`) is cheap but a different
  meaning (route stops from the geo pipeline, may be stale or missing per tour) — a product decision, not a perf fix.

## Should know

- **The real fix belongs to CIS** (owner of the catalog views). Proposal, not done here (no CIS migration number
  was reserved for this lane): a names-only view, e.g. `catalog.v_tour_place_name (source_system, source_id,
  tour_id, day_number, position, place_name)`, built from the same `atom` / `spelled` / `grouped` steps as
  `v_tour_place` but without the place card, destination and segment lookups; grant SELECT to `booking` and
  `tripplanner`. Booking would then read it instead of `v_tour_place` (one-line change of `DESTINATIONS_SQL`; the
  perf guard's `PARSE_BACKED` list must include it). The read-only diagnosis
  `AA-Ecosys/.tmp-session/S255/aa-996/explain_tour_place.py` (run in the Booking container) prints the hottest
  plan nodes for 25 / 100 tours to confirm which lookups cost the most before CIS writes it.
- Re-run `live_catalog_budget.py` after deploy: at limit 100 the destinations should be 4 statements of ~0.3–0.4 s
  each (none near 1 s), and a second identical call should show none.
