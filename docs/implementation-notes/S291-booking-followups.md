# S291 Booking follow-ups (AA-1177, AA-1153, AA-1149, AA-1128)

Follow-ups left by S290 (root `docs/sessions/2026-11-24-S290-accounts-closeouts-hotels-content-compliance.md`,
"Còn lại"; root `docs/audits/2026-11-24-S290-session/c43-w48/README.md` finding 6). One migration-only PR (`bk-075`,
contract class) first, then one code PR that raises readiness to `bk-075`.

Out of scope on purpose (business owner check on Thursday 26/11): supplier bank accounts, payouts and the 24 h payout
lock, approval / maker-checker rules, owner self-approval, roles and staff accounts, synthetic close-out data. None of
those files changes in this slice.

## Decisions

- **AA-1128, season calendar step: a table, not columns on `trip_plans`.** The outcome of a check (`no_tour`,
  `no_destination`, the CIS tour id, `checked_at`) lives in a new `booking.season_sync_checks` (bk-075). Columns on
  `booking.trip_plans` would fire its `set_updated_at` trigger on every check and move the plan's "last updated" for a
  background read. Cost: a DM-7.1 retention row (root PR, merged before the Infra copy).
- **What "the plan's catalog content changed" means.** A CIS lifecycle event of the plan's tour received after the
  check (`booking.inbox_events`, source `cis`, contract `catalog.tour`: retired, restored, paused, superseded; about 70
  rows, read by key in a sub-query) makes the plan due at the next run. A place linked to a destination in CIS sends
  no event and changes nothing Booking can see without reading the costly view again; the interval
  (`season_sync_recheck_days`, pricing policy, seeded 7 days, section operations, admin approver like the bk-059
  values) covers it. Rejected: adding `content_hash` to the bridge's `KEYS_SQL` (it would hash every catalog row every
  15 minutes; the hash also ignores the place rows, so it would still miss the change that matters).
- Plans never checked come first in the batch, then the due ones in the rotating order. A plan that gets a calendar
  loses its row; a cut read records nothing (the plan is tried at the next run). `deferred` (new count, outside
  `picked`) says how many plans without a calendar a run left out because their check is recent.
- **AA-1177, the mail record stays on the notification row** (`data.mail`), written after every message SES accepted
  or refused for good, plus the closing write that sets `done`. No new table: one row per contract already exists and
  only the job writes it. The feed drops the key in SQL (`n.data - 'mail'`), so no code path can leak it by accident.
- **AA-1153, which contract a draft replaces** (the diff base): every published or expired contract of the supplier
  with the latest start on or before the draft's comes back, then the one with the draft's `Reference` (spaces and
  case ignored) wins, then the one sharing the most rate items (room types, services) with the draft, then the newest
  published (`NEWEST_CONTRACT_FIRST`). A draft made by an import is reviewed against the base its import stored
  (`rate_sheet_imports.previous_contract_id`), so the live review and the import's stored diff name the same contract.
  Before, the newest published contract of that start won, so a DMC with a rooms contract and a later transfers
  contract starting the same day got every room rate listed as added.
- **AA-1153, Inventory in the diff** (`app/supply/inventory_diff.py`, pure): a term is keyed by component and dates;
  kind and numbers are compared; `narrowed` flags a change that sells less (kind free sale > allotment > on request,
  fewer units, more release days, a lower or new max pax on free sale) and counts every removed term. It is shown, not
  a business check: a new warning would need acknowledging at publish, which changes the maker-checker step this slice
  must not touch. Stop-sales are not contract terms and are not compared.
- **AA-1153, the allotment `taken` count was a real miscount.** Inside one `availability.many()` call every
  allotment answer took its units at once, also for a line another night of which was on request. Such a line is held
  on request (`HELD_ROOMS_SQL` counts only lines held available), so the next line of the same quote saw rooms as
  taken that nobody holds. `many(groups=...)` (one key per quote or hold line) keeps a line's units for the later lines
  only when the whole line was available; inside a line they count at once (two room units of one night share the
  allotment). Without `groups` (the staff check) each ask is its own group, the old behaviour.
- **AA-1149 label**: a pure helper `ui/booking/supplierBookingOrigin.ts` (unit-tested) picks "item N", "from the tour,
  day N" (a booking with `quote_part_id`), or "extra item" (only a booking added by hand).

## Changed

- Migration `bk-075` (contract): table `booking.season_sync_checks` and pricing policy `season_sync_recheck_days` v1 = 7
  (`seed:bk-075`, approved). Readiness raised to `bk-075` in the code PR.
- `app/pricing/season_sync.py`: batch query joins the checks table (still no catalog join; the CI guard test now
  allows exactly that one Booking join); `assign_default_calendars(..., recheck_days)`; new count `deferred`.
- `app/supply/contract_expiry_mail.py`: `_mail_contract` writes the record after each accepted or refused message.
- `app/notifications/service.py`: `list_feed` returns `data` without `mail`.
- `app/supply/rate_review.py`: `previous_of`, `TERMS_SQL`, `checks(..., terms, previous_id)`; `rate_sheet.import_sheet`
  passes the sheet's Inventory preview; `review` reads the draft's terms and the stored base.
- `app/supply/availability.py` (`groups`, `_Taken`), `app/b2b/quotes.py` and `app/b2b/room_hold.py` pass one group
  per line.
- Frontend: `RateReview.tsx` shows the Inventory part of the diff (warning pill when terms sell less), the Suppliers tab
  label, the policy label of the new parameter, and the Seasons page's sync panel says how many tours wait for the
  recheck (`deferred`; `SyncDeferred` split out of `SyncPanel` for the complexity ratchet).
- The local integration run of every touched module (500 tests) had 16 failures in `test_bk027_schema`,
  `test_bk045_schema`, `test_bk069_schema`; each file passes alone (the order-on-one-database issue the AA-1153 notes
  already record on origin/main).

## Tradeoffs

- A destination linked to a first-day place in CIS reaches a plan remembered as `no_destination` only after the
  recheck interval (7 days by default; an administrator can lower it). Accepted: the alternative is reading the costly
  catalog views for every waiting plan every 15 minutes, which is what this fixes.
- Moving a term's dates shows as one removed and one added term (no per-night comparison): simple and exact; the
  removed one counts as narrowed, so the warning pill still appears.
- The mail record is written once per message (a few extra single-row updates per contract) instead of once per
  contract: the price of never mailing the same person twice after a crash.

## Should know

- Ship order: Booking migration-only PR `bk-075` and the root PR (retention row) first, the Infra copy and Migration
  Apply (contract class), then the code PR (`chain: booking/075 applied before this PR`).
- Until the code PR deploys, the running code ignores the table and the key. Rolling the code back leaves the table
  unused (derived state, safe to keep).
- Integration fixture `seasons_clean` (test_aa1091_price_versions) now clears `booking.season_sync_checks` before and
  after each test: a check left by another module's catalog sync would defer the plans those tests expect assigned.
- Not done here (follow-ups): root guide `docs/guides/booking-price-setup.md` step 3 should name the Inventory changes
  in the import check; the root catalogue `docs/catalogue/surface.snapshot.json` gains
  `booking.policy.season_sync_recheck_days` on its next regeneration.
