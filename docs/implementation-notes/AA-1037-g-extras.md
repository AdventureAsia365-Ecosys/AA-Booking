# AA-1037 R4 extras G1, G2, G4, G5, G6 (S269, lane l11)

Source: root `docs/research/2026-11-05-asian-trails-b2b-benchmark/README.md`, section "Gaps worth adding to R4"; the tech
lead approved G1, G2, G4, G5 and G6 in S269 (G3, the tariff export, belongs to AA-1030). Stacked on the slice 1 branch
`feat/aa1037-agency-billing` (Booking #187; migrations bk-041 / bk-042 in #186). Migration bk-044 (expand).

## Decisions

- **G1 policy on answers, not in the hash.** `Quote` and `Hold` carry `cancellation_policy {policy_id, basis, tiers}`.
  The confirm locks the policy in force on the confirm day (AA-974), so a quote can only say "what a confirm today
  would lock" (`basis: in_force_today`); a confirmed hold says `locked`. Hashing the policy would make a policy change
  invalidate every quote, which the confirm-time lock already handles; the hash version stays `/2`.
- **G2 confirmation = JSON + printable HTML, no PDF renderer.** Booking has no document renderer (only supplier PDF
  uploads). A PDF library (WeasyPrint and its system packages, or reportlab) would grow the image and the CVE surface
  for one document; the HTML file is self-contained (inline CSS, no script, CSP `default-src 'none'`), downloaded as an
  attachment, and prints to PDF from any browser ("Save as PDF" note on the page). The brief allowed plain HTML.
- **G2 content**: no traveller names (Booking holds none for B2B) and **no prices**: the agency sells at its own price
  and may pass the document to its client. Titles are the version items written at confirm (AA-1013); supplier refs are
  the `confirmation_ref` of confirmed supplier bookings matched by `b2b_line_ref`, else by the version item position.
  Status is `confirmed` or `cancelled` (a `b2b_cancellations` row or a cancelled case). The agency looks a booking up by
  id only (its own, else 404); staff by id or case code (`b2b_desk.read`, the B2B operations permission).
- **G4 credit stop = billing status `past_due`.** The threshold is the agency's existing `suspend_after_days` (the same
  view CIS reads), not a new number: "past the agency's terms". Only new holds are refused (409 `credit_hold_overdue`,
  a wider Problem enum = contract minor); replays, confirms, extends and cancels of holds placed before still work (the
  deposit may already be with the agency's client). The check runs after the idempotent answers, before any lock.
- **G4 reminders**: steps = setting `agency_reminder_steps_days` (default `[1, 7, 14]`, read from `booking.settings`
  when present; no config row inserted, so bk-044 stays expand) + the credit-stop step at `suspend_after_days`. Only
  the highest step reached is sent (an invoice first seen 20 days overdue gets one e-mail, not three). Idempotency is a
  table row per (invoice, step) written after the send: delivery is at least once, like `bk_notify_email`.
- **G4 recipient on Dev**: outside `prod` every reminder goes to `STAFF_NOTIFY_EMAIL` (subject prefixed, the agency's
  address named in the body, `redirected` true on the row): Dev agencies are synthetic or the pilot, and a test run
  must never mail a real agency. No new environment variable.
- **G5 export**: invoices by `issued_on` (drafts excluded: no number), payments by `received_on`, every status with a
  status column; `period_last_day` (the stored `period_end` is exclusive); free-text notes and reasons left out;
  formula-looking text cells prefixed with `'`; UTF-8 BOM; at most 366 days; each export audited. Permission
  `agency_billing.read` (admin, finance).
- **G6 clauses as data with maker-checker**: `partner_terms_clause` (code, title, body, `free_cancel_days`, draft |
  approved, version). Finance or an admin edits (back to draft, version + 1); `terms_clause.approve` = admin (the
  business owner's role) approves the version it read (409 `clause_changed` otherwise), never its last editor (403 +
  CHECK). Agencies see only approved clauses (`GET /booking/b2b/v1/terms`); the staff Settings tab shows drafts with a
  "not shown to agencies" badge. The seed `price_error` row is DRAFT wording for the business owner.
- **`GET /terms`** also answers the agency's payment terms, credit status and the default policy in force, so the CRM
  can show "holds paused" and the fee schedule without a second call.

## Changed

- The brief said "billing_terms or a terms row" for G6: a separate table, because a clause has its own life (draft /
  approved / version) and text that does not belong on the per-agency money terms row; ALTERing `billing_terms` would
  also have made bk-044 a contract file.
- `cancellation.first_plan` reads `line_no` / `component_id` with `.get` (the hold answer passes quote line rows
  without every column).
- The invoice detail (`GET /booking/agency-invoices/{ref}`) also returns `reminders` (additive).

## Tradeoffs

- Credit stop on `past_due` only; a softer stop at `warn` would block agencies from the first overdue day (default
  `warn_after_days` 1). The threshold stays per agency through its terms override.
- One reminder job per day for every invoice (a failure retries the whole run; recorded steps are skipped) instead of a
  job per invoice: simpler, and the job table never holds hundreds of rows.
- The new tables keep the DELETE that the bk-014 default privileges give role `booking` (revoking it is contract);
  the code never deletes them.

## Should know

- **Merge order**: root PR (contract 1.3 + DM-7.1 rows for the two tables) -> Booking migration-only PR bk-044 (after
  bk-041 / bk-042 / bk-043 are on main; renumber to main max + 1 if the slot moved: `git grep '044_'` and `bk-044`) ->
  Infra copy (expand, auto-applied) -> this code PR (readiness bk-044), rebased on main after #186 / #187.
- bk-043 (AA-1030 partner price tiers, lane l9) is reserved in `RESERVED_ON_OTHER_BRANCHES` of
  `tests/unit/test_migration_guard.py` on both Booking branches, so the contiguity unit test is green before bk-043
  lands (the entry is harmless once it does, like the bk-016 one). The CI Migration preflight still refuses 044 while
  main max is 042 ("not main max + 1"): the bk-043 PR must merge first. If the bk-043 lane is dropped or lands after
  this one, renumber bk-044 to main max + 1 here, in the migration PR, in the readiness key and in the Infra copy.
- Dev check: SES must be configured (`SES_FROM_ADDRESS`, `STAFF_NOTIFY_EMAIL`) for reminders to send; otherwise the
  daily run is `skipped` and writes nothing.
- The HTML confirmation is served from the API host as an attachment (never rendered inline by the admin).
