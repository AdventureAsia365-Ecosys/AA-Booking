# AA-996 fix slice 4 — case workspace and proposals

Linear AA-996, Figma visual audit S254 (`AA-Ecosys/docs/audits/2026-10-28-S254-booking-figma-audit/`, slice 4: items
6, 7, 8 (second half), 11, 12, 15 (second half) and the Convert / Decline dialog footers). Session S254, agent Claude
Code (lane, orchestrated). No migration.

## Decisions

- **Quote-preview 403 was the audit session, not the role matrix.** `POST /booking/pricing/quote-preview` needs
  `pricing.read` (`app/routers/pricing.py` `CanPrice`), which is admin, sales and contracting (`app/auth/staff.py`):
  sales may quote, as `docs/ui-spec.md` §4.8 / Q8 asks. The audit ran as the E2E staff user, whose session refuses
  every non-GET request with 403 `e2e_read_only` before any permission check (AA-910); Preview-host sessions do the
  same with `preview_read_only` (AA-911). So nothing is granted; the matrix is pinned by a new unit test, and the
  price panel now explains a read-only session ("This session is read-only: live quotes are a POST…") in a neutral
  info box instead of the red "No access to pricing" (which blamed the role).
- **Approving an empty version is refused in the API, not only hidden in the UI.** `versions.approve_version` counts
  the version's items and answers 422 `version_empty` (the code Send and Price already use). Send already refused an
  empty version, so the Dev row (TC-000022 V1: approved, 0 items, 2,400 USD) can only come from an out-of-band write;
  the check makes the rule hold for every path. The builder also disables "Record approval" on an empty version.
- **Countries from the legs is a separate field (`leg_countries`), not a rewrite of `countries`.** `countries` is what
  staff set (Edit details drawer, `trip_case_countries`); filling it with derived values would make the drawer save
  them as if typed. `get_case` adds `leg_countries`: each CIS-keyed leg's tour country (`catalog.v_tour.country` →
  `v_country.iso2`, the join geo.py already uses), then the countries of the inquiry's route-day places
  (`itinerary.days[].destination_id` → `v_destination.country_iso2`), each once, legs first. One query, in a savepoint,
  fail soft on the catalog errors `destinations.py` lists (no country is better than a failed case page). The page
  shows stored countries when set, else the leg countries marked "from the trip legs".
- **Trip-check tabs count on the server.** `GET /trip-checks` gains `counts` (open, overdue, done, not applicable,
  all) under the same owner / check / case / search filters, one extra aggregate query per page load. Overdue uses the
  board's definition (`open` and `due_date < current_date`, as `v_trip_check_board.checks_overdue`).
- **Per-trip progress is computed in the list query, not by joining `v_trip_check_board`.** The view joins the supply
  state view for every case; the list needs only three counts per row, so three correlated counts on
  `trip_case_checks` (same definitions as the board) plus the case's current sales owner (`staff_assignments`, open
  sales row) for the drawer context. A page is at most 100 rows.
- **Overview order:** tour alerts, then the trip facts card (main column, two columns, Edit pencil → the existing Edit
  details drawer), then the TripPlanner inquiry panel, then the legs (one dashed line when the case has none). People
  and notes stay in the rail. "Adults / children" reads `pax_children ?? 0`. "Approved total" links to the approved
  version. Travel length = `duration_days` (legs) else both ends of the dates counted (`inclusiveDays`).
- **Inquiry preferences are read tolerantly.** The stored snapshot carries no message / budget / travel window today
  (contract v1.4 has `customer` + `itinerary` only); ui-spec Q12 says the panel shows them once the contract does. The
  readers (`ui/booking/caseFacts.ts` `inquiryPrefs`) look for the likely keys at the top level and under
  `preferences` / `trip` / `itinerary` / `customer`, so a v1.x addition shows without a frontend change; when none is
  present the block is not drawn.
- **Proposals tab actions mirror the /proposals row menu** (same icons, same disabled rules). Send stays disabled
  with the reason in the tooltip for an approved / sent / unpriced / empty draft; Discard is the existing DELETE of a
  draft (Booking has no hard delete of sent or approved versions, so Figma's Delete = discard a draft).
- **Trip-check tabs drive the existing URL keys.** Overdue = `status` open + `due=overdue` (the filter that already
  existed), so the FilterBar selects stay in sync and keep working; leaving Overdue clears only that due value.
  `SavedTabs` gained optional `active` / `onSelect` props for a tab that sets two keys (backward compatible).
- **Convert / Decline dialogs:** the stateful form now renders the Modal itself (mounted only while open) so its
  buttons can go into the Modal `footer` prop.

## Changed

- Backend: `app/trip_cases/versions.py` (approve refuses an empty version), `app/trip_cases/service.py`
  (`leg_countries`), `app/ops/trip_checks.py` (`counts`, per-row `checks` / `sales_name`), router docstring.
- Frontend: case page header + Overview, `ProposalsTab.tsx` (actions), builder price panel (locked summary, no live
  quote when locked, approval guard), preview (approved date, leg countries, Open builder on locked versions),
  `PricePanel.tsx` (read-only 403 explained), `StatusActions.tsx` (footers), `/trip-checks` (tabs, progress),
  `CheckDrawer.tsx` (trip context), `SavedTabs.tsx` (optional props), `ui/booking/caseFacts.ts` (+ node tests).
- Not done here (other slices or follow-ups): the trip-checks table width at 1440 (slice 1, PR #126), CSV on the
  Proposals tab / trip checks (slice 1), a trip-plan select in the Convert dialog (slice 2), the customer's past-trips
  count in the People card (no API field; not added).

## Tradeoffs

- Counting with `current_date` (DB session time zone) for the Overdue tab matches the board view and reminders; the
  browser's "overdue" filter uses the viewer's local date. Around midnight the tab count and the filtered list can
  differ by the checks due that day. Accepted (same as the existing badge).
- A locked version no longer shows the live quote breakdown (margin, rule, FX used). The saved total, channel and FX
  lock are what was sent; re-pricing a locked version would show a different number than the customer got.
- The one `next build` ran before the last change (the trip facts card moved above the inquiry panel; ordering only):
  that change is covered by `tsc` and `eslint`, not by a second build / browser run.

## Should know

- No migration. `leg_countries` and the trip-check `counts` / `checks` / `sales_name` are additive response fields;
  the frontend treats them as optional (an older API shows the page as before).
- The layout-mock staff fixture (`e2e/layout/staff-me.json`) gains `ops.read` and `bill.read`, byte-identical to PR
  #126, which also adds the fixtures the dashboard / billing pages then call (supplier-bookings summary, supplier
  bills). Run the shared `layout.spec.ts` after #126 is on main; `case-workspace.spec.ts` stands alone
  (`case-fixtures.ts` first, then the shared fixtures).
- `ui/booking/caseFacts.test.mjs` runs with `node --test`; `npm run test:unit` (added by PR #126) picks it up.
- The Dev data behind the audit (TC-000022 V1 approved with 0 items) stays as it is; new approvals of empty versions
  are refused.
