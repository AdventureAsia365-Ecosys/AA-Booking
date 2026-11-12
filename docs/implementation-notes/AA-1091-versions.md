# AA-1091 — price versions and the editable pricing policy (lane l14, S278)

Design: root ADR 0005b (price versions) and `booking.b2b.v1` 1.9; advisory `docs/audits/2026-11-12-S278-pricing-advice/`
(root repo). Tech lead rule (S278): every business policy value is adjustable on a Booking staff page, never a constant
in code. This file covers slice **A** (the policy parameters); slices B–E are listed under "Not done yet".

## Decisions

- **One version per parameter, not a change set.** `booking.pricing_policy` holds one row per version of one
  parameter (`param_key`, `version_no`, typed `value` jsonb, `unit`, `effective_from`, required `reason`,
  `replaces_id`, frozen `diff`). Two parameters changed together are two versions; each is approved on its own, which
  keeps the approver group per parameter simple (pricing lead or finance).
- **Status is derived from append-only steps.** `booking.pricing_policy_events` holds `submitted` / `approved` /
  `rejected` / `withdrawn`. No status column, no UPDATE: role `booking` has SELECT + INSERT only (bk-050 REVOKEs
  UPDATE / DELETE / TRUNCATE that the schema's default privileges grant). `draft` = no step, `pending` = submitted,
  `published` = approved, split by day into `in_force` / `scheduled` / `superseded` (same ordering as the reader:
  latest `effective_from`, then latest approval, then highest version).
- **Maker-checker in the table, not only in code.** The event row copies the maker; a composite FK
  `(policy_id, maker) -> pricing_policy (id, created_by)` pins it, and a CHECK refuses `approved` / `rejected` by the
  maker and `submitted` / `withdrawn` by anyone else. A unique index allows one final step per version.
- **A draft is never edited.** Saving a new draft of the same parameter withdraws the maker's previous draft. At most
  one open (draft or pending) version per parameter (409 `open_version`); an advisory lock per key serialises writes.
- **Never backwards.** `effective_from` must be today or later when proposed and still today or later when approved
  (409 `effective_date_passed`). The diff is "value in force on the effective day -> new value".
- **Approver groups (ADR 0005b d6, Q5).** New permissions: `pricing_policy.read` (admin, sales, contracting, finance),
  `.write` (admin, contracting, finance), `.approve` (admin, contracting: the price parameters) and
  `.approve_finance` (admin, finance: FX, tax, surcharge and payment parameters). `pricing_policy.read` is in the
  preview cap.
- **No fallback values in code.** `app/pricing/policy_values.load` returns the approved values in force; a missing
  parameter raises `PolicyUnavailable`, which the B2B quote answers as 503 `unavailable` (like a missing pricing
  rule). The seed (bk-050) carries the 20 answers the tech lead approved, made by `seed:bk-050`, approved by
  `tech lead S278`, effective 2026-01-01.
- **Answers that already are staff data stay there.** Margins (`pricing_rules`), the cancellation scale
  (`cancellation_policies`), deposit terms (`billing_terms`), agency tiers and FX keep their own pages; the policy page
  links to them. bk-051 writes the S278 margins (b2b 20 / 12, fixed_departure 25 / 15, on the selling price) and the
  FIT cancellation scale (30–22 d 10 %, 21–15 d 25 %, 14–8 d 50 %, 7 d–48 h 75 %, < 48 h 100 %; 31 d+ free) as new
  approved rows dated 2026-11-12; staff change them later on their pages with a second person.
- **Two code paths read the policy today:** `app.b2b.quotes` (`valid_until` = now + `quote_validity_days`, replacing
  the 48-hour setting; a `tp:` line of `group_on_request_min_pax` travellers or more is `on_request`). The other
  parameters are stored and shown as "stored for a later step" until their slice reads them (`Param.used_by`).
- **Holds keep a line the quote answered on request.** A line quoted `on_request` (the group rule) is held
  `on_request` even when its departure has seats, so it is never auto-confirmed.

## Changed

- bk-050 is a **contract** file (a REVOKE is not expand), so the wave ships a migration-only PR (bk-050 + bk-051)
  first; the code PR raises readiness to bk-050.
- The setting `b2b_quote_valid_hours` is no longer read (bk-051 marks its description "Retired"); a CI guard fails if
  any code reads it again.
- The integration conftest undoes the bk-051 rows right after the replay applies them, so every other pricing test
  keeps its pre-S278 numbers; `test_bk051_schema.py` applies the file again and checks it.

## Tradeoffs

- Per-parameter versions vs. a "policy version" (all parameters at once): per parameter wins on approval routing and
  history readability; the cost is that two related changes (validity and festive validity) are approved one by one,
  and the cross-parameter bound (festive <= validity) is checked against the values in force at both propose and
  approve time.
- Values are jsonb with the type in code, not typed columns: one table for every type; the registry validates.
- The CI guard (`tests/unit/test_aa1091_policy_guard.py`) lists the numeric constants that exist today with a reason;
  the ones marked "known gap" are business values still in code (below), so the guard blocks new ones without
  pretending the old ones are gone.

## Should know

- Known gaps the guard lists (business values still in code or in the read-only `booking.settings`, follow-ups):
  VND line rounding (1,000), the B2C total rounding increments, the "from" price for 2 travellers, the pax bands of
  the computed price matrix, the hold length / extension / re-hold limit (settings rows with no edit page), the child
  and infant age defaults (settings rows).
- `quote_validity_festive_days`, `festive_non_refundable_days` and `gala_dinner_days` need the season calendars and
  holiday events (slice C) to be read; until then a festive quote gets the normal validity.
- bk-050 / bk-051 need bk-049 (lane l15, b2b_partners RLS) merged first: the migration guard wants contiguous numbers
  and the preflight wants main max + 1. Ship order: the l15 Booking migration PR (applies booking/049), then the
  migrations branch rebased on the new main (disjoint files, no conflict; a trial of 049 + 050 + 051 passed the
  preflight with the pinned Infra tools, the guard test, the pre-push and the whole integration suite), then the code
  PR. Never apply 050 by hand before 049 is registered (049 would become out of order).
- bk-051 review fix pass: the non-margin columns are copied from the **approved** rule in force on 2026-11-12 (the
  same pick as `repository.RULE_SQL`), never a pending staff draft or a future-dated row (that would have put an
  unapproved value in force under the tech lead's approval). Both seed inserts conflict on `id` only, so a staff row
  that already holds (channel, 2026-11-12), or a b2b default cancellation schedule on that date, fails the file and
  rolls it back instead of being skipped while bk-051 registers; a post-check also fails the file unless the two rules
  and the schedule exist with their approvals (a channel with no approved rule to copy from). If the Dev apply fails
  on such a clash, agree a new date with the tech lead and change it before the file is registered.

## Slices B and C — price list versions and season calendars (lane l17, S278; ADR 0005b delivery steps 3 and 4)

### Decisions

- **Two migrations, contract, one migration-only PR (bk-052 + bk-053)**, then the code PR raises readiness to both.
- **Stored status, derived superseded.** `price_list_versions.status` is a column (the quote, tariff and matrix read
  it), moved only along d2 by a guard trigger; `published` <-> `superseded` is recomputed from the published set on
  every publish and withdrawal (`price_versions.refresh_states`). Unlike bk-050 (events table) because a version also
  carries editable draft facts and rows, and the overlap rule reads many versions at once.
- **The effective matrix stays.** `booking.trip_plan_prices` keeps being what every reader uses (tariff, RRP,
  `v_from_prices`, readiness, workbench, workbook export); it is rebuilt from the winning versions (+ the cost sheet's
  computed rows around them) by `cost_sheet.recompute_plan`, now also called with the checker as actor on approve /
  withdraw. Each hand row carries `version_id` (the seam for step 6: a quote pins it), each row `season_code`.
- **Hand layer vs cost-sheet layer.** The overlap rule runs among price list versions only. The cost sheet's computed
  rows stay unversioned (`version_id` NULL) and fill around the hand rows exactly as before (hand wins on overlap).
  Step 5 turns them into `source = cost_sheet` versions with the d8 auto-publish rule; a version-level overlap rule
  between a daily recompute and a hand version would otherwise make every recompute override hand prices.
- **Migration of today's data.** Per plan with prices or history: every closed history set becomes a past version,
  status `withdrawn` by `migration:bk-052` at the time the next set replaced it (the old PUT replaced the whole set;
  marking them `superseded` would let the overlap rule bring their uncovered dates back to life, changing prices); the
  rows in force become the latest version, `published`, approved by `migration:bk-052` at the time of its open history
  set, source from `change_source`. Hand rows of the matrix point at it; nothing else in the matrix changes. Versions
  migrated from a cost-sheet set keep the computed rows as a snapshot (`computed` true); the matrix rebuild ignores
  them (the cost sheet owns those).
- **`PUT …/prices` is retired with 409 `price_versions`** (no breaking schema change for oasdiff; the response body
  points at the versions URL). The commercial drawer of a trip plan shows the matrix read-only and links to the
  tour's Versions tab (`/pricing/workbench?versions=<key>`).
- **Rows stay dated; the season is derived.** d1 says rows carry `season_id` instead of the free-text label. A season
  is a code inside a calendar, not a row, so the column is `season_code` (low / high / peak / festive), set on every
  matrix piece from the plan's calendar; the free-text `season` stays as a label for people. The draft editor drops
  the Peak checkbox; a row priced from a net cost takes the peak uplift of the calendar's season on its first day.
  Season-coded rows without dates (a "high season" price that follows the calendar) are a later option; the matrix
  splitting by season already gives each piece its code.
- **Peak = `peak` or `festive`.** The pricing rule has one uplift; a festive date (Tet) is at least peak. One season
  function (`seasons.Calendar.season_on`) for the cost sheet windows, the B2B quote line (service date), the tariff
  rows (each piece's first day) and the version matrix. A plan with no calendar is low season everywhere, so nothing
  prices differently until staff enter ranges (empty calendars are created by the sync, never with ranges).
- **Festive event ranges lie over fixed ranges** instead of being refused as an overlap: refusing would force staff
  to cut the high season around Tet by hand every year (copying event dates, which d4 forbids). Fixed-date season
  ranges of one calendar never overlap (409 `range_overlaps`, checked at proposal and at approval under a calendar row
  lock).
- **Maker-checker for seasons via status columns** (bk-048 style) and column grants (no trigger): ranges and events
  pending -> approved / rejected (second person) / withdrawn (maker); removal = `end_requested_*` then `ended_*` by a
  second person; a default-season change is proposed in `pending_default_season`. Creating an empty calendar is a
  direct audited write (it changes no price).
- **Stop-sale** makes an `available` `tp:` quote line `on_request`; it never changes the season or the price.
- **Festive validity** (d14) is applied now that calendars exist: a `tp:` line on a festive date makes the quote valid
  `quote_validity_festive_days` (policy). Hotel / service lines (destination calendar of the hotel) are not checked yet.
- **Default calendar of a plan**: `bk_trip_plan_sync` assigns up to 50 plans per run the calendar of the destination of
  day 1 (`catalog.v_tour_place.destination_id`, one tour per query: an `ANY()` filter is not pushed into the view's
  parse), creating the empty destination calendar when missing; plans whose day 1 names no known destination stay
  without (low season); the batch order rotates per slot so they never block the others. Fail-soft on catalog errors.
- **Workbook import = draft version per tour** (`source = import`, the file's SHA-256), never live; idempotent (rows
  equal to the hand prices in force, or the same file already the maker's open import draft, write nothing; a newer
  file withdraws the older open import draft). Rows priced from a net cost stay in the draft (amount as typed) unless
  a file row covers them, as before.
- **Policy values through the bk-050 reader**: `tariff_year_start` (default window and tariff year of a draft, the
  migration's tariff years) and `diff_highlight_pct` (diff highlight) are read by `app.catalog.price_versions`;
  `quote_validity_festive_days` by `app.b2b.quotes` (`Param.used_by` updated). The 300-character reason is a schema
  bound of ADR 0005b d1, not a policy parameter.
- **Permissions** (d6): `price_version.write` / `.approve`, `season.write` / `.approve` = admin, contracting (the
  customer-service maker and the pricing lead are both contracting today; the data CHECKs keep them two people).

### Changed

- Backend: `app/catalog/price_versions.py`, `version_matrix.py`, `app/pricing/seasons.py`, `seasons_admin.py`,
  `app/routers/price_versions.py` (new); `trip_plan_prices.py` (`replace_prices` -> `price_rows`, `version_id` /
  `season_code` in the matrix), `cost_sheet.py` (hand rows from versions, calendar cuts and peak, watermark includes
  versions and seasons, plans with published versions recomputed), `b2b/quotes.py` and `b2b/tariff.py` (season of the
  line), `price_import.py` (drafts), `trip_plan_sync.py` (default calendars), `routers/catalog.py` (PUT 409),
  `auth/staff.py`, `db.py` (readiness bk-052, bk-053), `policy_values.py` / `policy_params.py`; openapi regenerated.
- Frontend: Price setup tour tabs (Hotels & services | Versions, `VersionsDrawer.tsx`, `versions.messages.ts`), pages
  `/pricing/seasons` and `/pricing/holiday-events` (EN + VI messages), `lib/api/priceVersions.ts`,
  `ui/booking/priceVersions.ts` (+ node test), commercial drawer read-only prices, the matrix editor without Peak, nav,
  rbac, visual and layout page lists and fixtures.
- Tests: integration `test_aa1091_price_versions.py` (state machine, overlap / superseded / withdrawal restore,
  migration keeps every price, calendar no-overlap, festive from a holiday event, matrix cut by season, default
  calendar), `test_bk052_schema.py`, `test_bk053_schema.py`; unit `test_aa1091_versions_seasons.py`; existing price
  tests now publish hand prices through a version (`tests/integration/price_versions_helper.py`).

### Tradeoffs

- Matrix splitting by season multiplies rows only when a calendar has ranges; the matrix cap rose to 1000 positions.
- The draft row of a net-priced row takes the peak of its first day: a net-priced row spanning seasons is priced at
  one season. Staff split such rows (the editor makes the next season's row easily).
- The impact counts open quotes by `tp:` line service date in the window; it does not re-price them (step 6 pins
  versions and re-quotes on expiry).
- `withdraw` of a published version needs `price_version.approve` and a person other than its maker; the maker of a
  wrong published price asks a checker.

### Should know

- Ship order: Booking migration PR (bk-052 + bk-053) -> Infra copy + Migration Apply (contract) -> code PR. The code
  PR does not contain the migration files.
- Between the migration apply and the code deploy the old code can still write unversioned hand rows with `PUT
  …/prices`; the new code keeps such rows for a plan with no published version and would drop them once a version is
  published for the plan (minutes of exposure; only a handful of plans are priced today).
- The integration catalog stub (`stub_catalog.sql`) now exposes `destination_id` / `destination_name` on
  `catalog.v_tour_place` (the live CIS 256 columns), matched by place name.

## Not done yet (later lanes)

- **Step 5** diff and impact before approval of contracts, rules and FX; recompute through the auto-publish rule
  (`source = cost_sheet` versions); reject / withdraw of pending rules and FX; `supersedes_id` on contracts; contract
  seasons (`season_calendars.owner_kind = supplier_contract`) read by the cost sheet with the stay pricing flag.
- **Step 6** quotes pin `price_version_id` (the matrix row's `version_id`), re-quote shows old vs new.
- **Step 7** tariff versions, ETag / 304, next-year edition. **Step 8** versioned Excel export / import, copy to next
  year, provisional versions.
