# AA-1073 rest — departure board, ops sheet, departure fees, price adjustments and early booking

Lane l35 of session S277 (Claude Code). The four items of AA-1073 that the S277 per-item check found were never built
(root `docs/audits/2026-11-11-S277-booking-uat/items-evidence.md`): bk-04 departure board, bk-16 ops sheet, bk-09
departure fees, bk-17 price adjustments + early-booking rule. `AA-1073.md` covers bk-01..bk-03 (bk-046). Our own code;
nothing of the OPL-1 app is copied.

## Decisions

- **One migration, bk-048, contract** (ALTERs departures, outbox, agency_invoice_line, pricing_rules,
  b2b_quote_lines, replaces the departures_outbox function / trigger and two invoice line CHECKs, inserts a terms clause
  row, next to three new tables and one view). MIG-4: migration-only PR, then the code PR raises readiness to bk-048.
- **Board = a view** `booking.v_departure_occupancy` with the same seat rules as `departures.py` (`USAGE_SQL`,
  `ALLOTMENTS_SQL`), counted with per-departure LATERALs so a date-window filter only touches the departures of the
  window. Country and tour title come from `catalog.v_tour` in the app (a savepoint: the board answers without them
  when the catalog cannot be read). The filter options (countries, trip plans) come from every plan with a departure
  in the window, not from the filtered page (lessons: server-side filter options).
- **Price column**: the trip plan's price hint, the adjustment in force and whether a quote today still gets the
  early-booking discount. A real "from" price per departure would need the full quote engine per plan (supplier rates,
  FX, pax bands): left out on purpose; the quote is the price of record.
- **Ops sheet**: `meeting_point` (300 chars, a public place), `meeting_time` (time, local), `ops_note` (staff only,
  `pii:free_text`, never sent, its text never copied into the audit row: only "changed"). `departure_staff` holds a staff
  user XOR a supplier (bk-008 categories guide / transport); no guide name, phone or e-mail is stored (no staff PII to
  agencies, as the issue says). Unassign deletes the row; the audit log keeps who it was. Writes `ops.write`.
- **departure.updated** names the departure only (the outbox carries ids); the agency re-reads `GET /departures`, which
  gains `meeting_point` / `meeting_time` (contract 1.8). Not emitted on a cancelled departure (its own
  `departure.cancelled` says enough).
- **Departure fees**: `status` pending → approved | rejected, decided by a second person (`b2b_desk.approve`: admin,
  contracting; maker `b2b_desk.write`). Decision final (guard). Approval emits `departure.fee_charged` with
  `fee_id`, `amount`, `currency` (three new nullable outbox columns, CHECKed together; `currency` CHAR(3) so the
  bk-022 "no free text column on the outbox" test holds). Contract 1.8 `Event.fee`.
- **Fee ↔ invoice line**: the line names the fee (`agency_invoice_line.source_fee_id`, unique while not voided), like
  `source_hold_id` names a hold, instead of an `invoice_line_id` on the fee (the line key is composite and a voided
  invoice's fee must be billed again on the next draft). `bk_settlement_draft` bills every approved fee decided before
  the period end that has no live line, on the agency's draft in the fee's currency, even for an agency without a
  booking that month.
- **Agency invoice API**: a `departure_fee` line is sent as `line_role: adjustment` (description "Departure fee (...)").
  The contract 1.5 enum has no `departure_fee` and the AA-Partner CRM refuses an unknown line role
  (`app/booking/contract.py LINE_ROLES`). Once the CRM accepts the role, a later minor sends it as is.
- **Terms clause** `departure_fees` is inserted as a **draft** (`updated_by system:bk-048`): only the business owner
  can approve legal wording (`terms_clause.approve`, maker-checker), then `GET /terms` shows it.
- **Price adjustments**: `adjust_pct` is a fraction (-0.5..1, not 0) or a signed `amount` per traveller (adults +
  children; infants take no seat) in one currency, with a reason. Maker and checker both need `pricing.write` (admin,
  contracting), two different people, like pricing rules. One in force and one pending per departure; a change replaces
  the one in force on approval.
- **Engine order** (bk-17): early booking lowers the **margin** by `early_discount_pts` (margin points, like
  `peak_uplift_pts`), then the agency discount, then the departure adjustment, then the line rounding. An amount in
  another currency converts at the plain rate (no FX buffer). A price never goes below 0. Under the floor margin the
  line still sells; `price_note.below_floor = true` and a warning log ("still warning under floor").
- **Hash**: a line hashes `price_note` (adjustment + early booking only) when one applied; any other line hashes as
  before, so existing quotes keep their hash.
- **Early booking** lives on the b2b and fixed_departure rules (CHECK); the B2B engine applies the b2b rule's. The
  fixed_departure rule stores it for the day a B2C fixed-departure quote exists (no B2C quote path today, AA-1046).
- **Early booking in the admin** (review fix): the "New pricing rule" drawer has "Early booking: days before start" and
  "margin points off" for the b2b and fixed_departure channels only; both or neither are sent (values left after a
  switch to another channel are dropped); points are typed like peak uplift ("5" = 0.0500). The 422 codes
  `invalid_early_booking` (under the points input) and `early_booking_channel` (under the days input) show inline, not
  in the drawer banner. The pure rules live in `frontend/ui/booking/earlyBooking.ts` (unit test next to it). The rules
  table shows "early −5 pts / from 60 d" under the margin (no new column).

## Changed (against the issue text)

- Fee ↔ invoice: `agency_invoice_line.source_fee_id` instead of `departure_fees.invoice_line_id` (see Decisions).
- Fee states: `status` pending / approved / rejected with `decided_by` / `decided_at` rather than a separate
  `approved_by` only (a rejection needs recording too).
- Price adjustments "valid flag": the allotment pattern (approved_at + ended_at), not a boolean.
- Board permission b2b_desk.read as asked; ops writes `ops.write`; fees `b2b_desk.write` / `b2b_desk.approve`;
  adjustments `pricing.write` (maker and checker).
- The board lives at `/departures` (Operations nav) and the drawer links to the trip plan's departure and the manifest.

## Tradeoffs

- The view recomputes seats per read (no materialisation): Dev has tens of departures; a month window is at most a few
  hundred rows. If it grows, an index on `b2b_hold_lines (departure_id)` already exists; EXPLAIN with the board's
  filter shape (date range) before adding more.
- A per-departure price on the board would be exact only through the quote engine (costs, FX, pax); a hint + the
  adjustment is shown instead.
- The fee line is shown to agencies as an adjustment for now, to keep the CRM reading invoices; the event carries the
  real fee.

## Should know

- Order: bk-048 migration-only PR, applied by Infra Migration Apply (contract); the root PR (DM-7.1 rows of the three
  tables, contract 1.8) merges before the Infra copy; then this code PR (readiness bk-048).
- The code PR is built from `origin/main` (not stacked on the migration branch). Local integration tests copy
  `migrations/048_*.sql` into the worktree (not committed).
- The B2B event `type` enum grows (`departure.updated`, `departure.fee_charged`): oasdiff reports
  `response-property-enum-value-added` on `GET /booking/b2b/v1/events`, so the PR needs the `api-breaking` label with
  the client plan (the CRM records unknown types as ignored, `sync.py unknown_type`), like #219 / #224.
- Tests that re-run bk-022 / bk-041 / bk-046 / bk-047 put back older CHECKs, trigger and the outbox column grant: the
  bk-048 tests replay bk-048 in their fixtures and delete the outbox rows of the two new types afterwards.
  `test_bk020_b2b._clean` deletes the bk-048 rows before the departures.
- The rules table's Margin cell now puts child / infant on two short lines (one long line widened the nowrap column).
  Layout-mock "pricing-rules fits 1440 / 1400" already failed before this lane (table 1112 px against a 1078 px budget,
  measured with the old fixture); the new b2b fixture row (child + infant + early booking) makes it 1144 px. The
  visual checks (1440 / 390, document and frame) pass; the table scrolls in its own box. Folding a column is a
  follow-up, not part of this slice.
- The layout-mock test "the pricing rule drawer" (390 and 1400 px) covers the channel switch, the one-value error, the
  POST body and the 422 field mapping.
- Layout and visual checks cover `/departures` (calendar), `?view=table` and the drawer; signed-in smoke
  `e2e/signed-in/departure-board.spec.ts`.
- Business owner: approve the `departure_fees` clause wording (Agency billing → terms clauses) before any fee is
  charged; the fee rate per seat is entered per fee (no default rate).
