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

## Not done yet (this lane)

- **B** price list versions for hand prices (draft -> pending -> published, diff + impact, Excel import as draft).
- **C** season calendars per destination and per contract, holiday events per year, peak flag from the calendar
  (removing the `peak` label match), one season logic for B2B quotes and the RRP, the festive validity.
- **D** quotes pinning `price_version_id` / `tariff_version_id`, re-quote showing old vs new.
- **E** versioned agency tariff (current + next year, ETag / 304) and the AA-Partner reader of the new fields.
