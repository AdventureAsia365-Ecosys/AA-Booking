# AA-1091 — A10e-2 pricing pair (Dev-only E2E maker / checker for the live check with synthetic prices)

Lane l29 of S279. Root ADR 0006a A10e-2. The tech lead approved running the AA-1091 live check with SYNTHETIC prices
on Dev with E2E identities (S279, option G1 of the AA-1091 runbook "identity gate").

## Decisions

- **Maker = the CIS A10d address with the Pricing team preset.** `e2e-cis-cs@e2e-mail.lumiguides.it.com`, groups
  exactly `cis-customer_success` + `booking-contracting` (what the preset gives a customer-service member). Any other
  group set is 403 `e2e_scope`, including the A10d set alone (no Booking group: that session never writes here).
- **Checker = a new Dev-only address** `e2e-booking-pricing-checker@`, exactly `booking-contracting`
  (AA-CIS-Infra creates it with `crm_dev_identity.py e2e-booking-writers`).
- **Synthetic per plan, not per agency.** Pricing writes touch trip plans. A plan qualifies when no agency that CIS
  does not mark synthetic has a quote line (`component_id = tp:<plan id>`), a hold line, an allotment or a departure
  fee on it (on its departures). A plan no agency used qualifies (the live check picks unused tours). Same fail-closed
  shape as the A10e SQL: a missing `is_synthetic` column or a DB error answers False.
- **Versions: only the E2E maker's.** Every version route (save, submit, withdraw, approve, reject) needs the version's
  `created_by` staff row to be the maker's address AND its plan synthetic. So the maker's approve / reject can only hit
  its own version, which the service refuses (403 `maker_checker`): the live probe exercises the service rule
  instead of stopping at the guard. The checker never decides a real person's version.
- **Workbench actions:** body exactly `{"action", "tour_keys"}`, action `make_sellable` or `make_not_sellable` (no
  provisional price, no recompute), every tour key a synthetic plan (one query, distinct keys counted, so an unknown
  key also refuses). The guard reads the JSON body (like the A10e-1 departure cancel).
- **Workbook import: checked in the route.** The guard cannot parse a workbook, so `CHECKED_IN_ROUTE` lets it pass the
  guard and `post_price_import` hands `price_import.import_workbook` a `plan_guard` for an E2E session
  (`Staff.e2e_writer`, a new session flag set by `current_staff`). It runs right after the tour keys are matched,
  before the DRY RUN preview or any write: every matched tour must be synthetic, and a workbook that would create a
  departure is 403 `e2e_read_only` (departures are not a pricing step).
- **No new grant.** Contracting already holds `trip_plan.write`, `price_version.write` / `.approve`; the allow-list is
  what limits them.

## Changed

- `app/auth/e2e_staff.py`: two writers, the plan / plans / version SQL, `BODY_CHECKS`, `BODY_SYNTHETIC`,
  `CHECKED_IN_ROUTE`, `plans_synthetic`, `check_import`; `check_body` handles body checks; `check_synthetic` takes the
  body.
- `app/auth/staff.py`: reads the body when `e2e_staff.reads_body`; `Staff.e2e_writer`.
- `app/routers/catalog.py` + `app/catalog/price_import.py`: the `plan_guard` hook.
- Tests: `tests/unit/test_aa1091_e2e_pricing_pair.py`, `tests/integration/test_aa1091_e2e_pricing_pair.py`; the
  AA-1087 guard tests list the new writers and check every allowed write is checked exactly one way.

## Tradeoffs

- The runbook named the maker / checker allow-lists; the checker's version rule ("created by the E2E maker") is stricter
  than "any synthetic plan" so the checker cannot approve a real staff member's version of an unused tour.
- Prices are catalogue-wide while published (every agency tariff shows them): accepted by the runbook, which withdraws
  them at cleanup (step 10). The plan rule only keeps tours an agency really used out of reach.
- `component_id` has no index; the plan SQL scans `b2b_quote_lines` per check. Dev only, a few hundred rows.

## Should know

- Removed with the A10 family (AA-1054): drop the two writers, the SQL, `BODY_CHECKS` / `BODY_SYNTHETIC` /
  `CHECKED_IN_ROUTE`, `Staff.e2e_writer` and the `plan_guard` hook.
- No threshold, limit or cap was added (the workbench key count stays bounded by the route model).
