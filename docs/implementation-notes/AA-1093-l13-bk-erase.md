# AA-1093 lane L3 (S279 l13-bk-erase): the Booking step of the tenant offboarding erasure

Root plan: `AA-Ecosys/docs/audits/2026-11-13-S279-session/aa1093-plan.md` Part 2, lane L3. Tech lead decisions (S279):
D3 (Booking anonymises, keeps money rows and the invoice bill-to under the accounting exception) and D8 (the saga
refuses the pilot tenant; on Dev, apply only for synthetic tenants). Contract: root
`docs/contracts/tenant.erasure.v1.openapi.yaml` (merged on root master before this lane's fix pass).

Two PRs (MIG-4): the migration-only PR `bk-057` (`migrations/057_tenant_erasure_log.sql` + its schema test), then
this code PR (`app/b2b/tenant_erase.py`, the pull skip, readiness bk-057).

## Decisions

- **Key bk-057** (main max + 1; bk-056 is the S279 thresholds file). The B2C table drop takes bk-058.
- **Contract, not expand.** The expand lint of AA-CIS-Infra refuses a grant to another owner's role (`cis_app`, the
  report view reader) and any trigger / grant on an existing table; the erasure also needs new triggers, a replaced
  guard and column grants. Preflight with the pinned Infra tools says `contract`. On Dev it is applied by Infra
  Migration Apply once every job table is idle.
- **The contract shape wins** (fix pass). The log is append-only, one row per report, with exactly the contract's
  `ErasureReportRow` fields (report_id, request_id, tenant_id, app, round, mode dry_run|apply|verify, status
  ok|blocked|refused|failed, error_code, plan_hash, tables, blockers, census, job_id, reported_at) plus `source`
  (cis | cli), which the view does not expose. The request reader reads `ErasureRequestRow` (CIS 410): `WHERE app =
  'booking'`, the newest `updated_at` per request, acts on `phase` dry_run / apply / verify while `step_status` is
  `active` and `not_before` has passed, takes `approved_plan_hash` and `approval_expires_at`. The CIS saga (lane L14)
  reads `to_jsonb` of the view and already uses the contract statuses.
- **Plan hash per contract**: table digest = md5 hex of the sorted `'<id>|<updated_at UTC ISO 8601 or empty>'` lines
  joined by `\n` (sorted with `COLLATE "C"`; `to_char(ts AT TIME ZONE 'UTC', ...)` so no session time zone enters);
  plan hash = sha256 hex of `'tenant.erasure.v1|<tenant>|<round>'` and the sorted table lines, joined by `\n` (the
  contract does not name the separator; `\n` is stated in the module docstring and CONTEXT). Keep entries are in the
  hash with an empty digest (the contract hashes every table entry): a new invoice between DRY RUN and apply starts a
  new round, which is the safe direction.
- **Opening of the guards** (review finding: the GUC alone was an opt-in flag). `booking.tenant_erase` must name the
  report id of an ok DRY RUN in the append-only log, no tenant may be bound, and each guard checks the row belongs to
  that tenant's footprint (`booking.tenant_erase_scope`, one SQL function the app's UPDATEs also use, so app and guard
  cannot disagree). SECURITY DEFINER was the alternative and is not allowed in schema booking (bk-014 post-check).
- **Free text covered** (review findings). The audit `summary` (a status change writes `"<from> -> <to>: <reason>"`,
  a custom check its title) is redacted to `[redacted]`; `entity_label` is cleared on every audited row of the
  footprint (it is `pii:free_text`). Notification titles are redacted too (they name the agency or a check, e.g. the
  desk below-floor notice). Custom check titles, invoice `staff_note` / `void_reason`, payment `note` /
  `reject_reason` and cancellation `waive_reason` (bk-047 already allows `[redacted]` and booking holds the column
  grant) are redacted. Forced non-null text uses `[redacted]`, as the DM-7.1 rows and the DSAR runbook do (was
  `Erased`).
- **Payment guard**: replaced verbatim from bk-042 with one branch first in the UPDATE path; the invoice guard needed
  no change (bk-042 leaves `staff_note` / `void_reason` editable on an issued invoice).
- **Manual, not in the apply**: `b2b_desk_bookings.override_reason` and `departure_fees.note`. DM-7.1 says the app
  role has no grant on them and a reviewed bk_owner run-script redacts them; kept as is, listed in CONTEXT and in
  `CENSUS_KEPT` with that reason.
- **Census (verify)**: per table the tenant rows that still hold a value in a cleared `pii:` column; the
  pii-tagged columns are read from the catalog at run time, and a column in neither `CENSUS` nor `CENSUS_KEPT` fails
  the verification (`census_incomplete`), so a new column is never silently skipped. An integration test asserts the
  same on the migrated schema.
- **Guards fail closed**: from the request flags (CIS copied them at T0, so they survive CIS erasing the tenant); the
  account view is a second opinion only (pilot there = refused; missing there = not a refusal, CIS may have erased).
  CLI: account view only, an unknown tenant counts as the pilot except for a DRY RUN.
- **Kill switch**: `TENANT_ERASE_APPLY_ENABLED` (Settings `tenant_erase_apply_enabled`, default off) gates the job
  apply; when off the apply job reports `refused` / `apply_disabled` (a contract code) instead of staying silent. Root
  thresholds audit section 4 item 7 keeps destructive kill switches in Terraform. Not set anywhere yet.
- **Thresholds rule**: no new business threshold. `MAX_ATTEMPTS` and `SLOT_SECONDS` carry `# technical-constant:`.
  The approval validity and the exit windows are CIS settings; Booking computes no window.

## Changed

- Log: upsert per (request, mode) -> append-only per report; statuses planned/stale/applied -> ok/blocked/refused/failed
  with error codes; `counts`/`detail`/`finished_at` -> `tables`/`blockers`/`census`/`error_code`/`reported_at`.
- Request columns `phase plan|apply` + `approved_plans` -> the contract's `ErasureRequestRow`; new mode `verify`.
- Job payload `{request_id, tenant_id, round, mode}`; the job re-reads the request and writes no report when the round
  or phase moved (`superseded`). Every other outcome is a report (refusals and failures included).
- `EraseSchedule()` no longer takes the kill switch (the job reports `apply_disabled`).
- CLI: `--mode dry_run|apply|verify`, `--round`; `--request-id` removed (the CLI is its own request).
- `bk_tenant_pull` skips tenants with an apply report of status ok (unchanged API `erased_tenants`).

## Tradeoffs

- One transaction per tenant (tens to hundreds of rows per agency) instead of batches: simplest all-or-nothing and the
  hash is compared in the same snapshot. The audit guard runs the scope function per row (inlinable SQL, indexed
  lookups); fine at agency scale, revisit if an agency ever has tens of thousands of audit rows.
- The opening still trusts the log: a session holding role `booking` can append an ok DRY RUN row for a tenant and
  then redact that tenant's rows. It cannot remove that row (append-only), cannot reach another tenant or any B2C row,
  and can only write NULL or the marker, so the residual risk is "erase one tenant, with evidence", not tampering.
- Full redaction of audit `before` / `after` on the money entities (invoices, payments) too: the money rows themselves
  keep every fact; the audit keeps actor, action, entity and time.

## Should know

- Apply order on Dev: bk-057 (migration PR, Migration Apply) -> this PR (readiness bk-057, so Deploy Dev waits for it).
  The Infra owners registry row + isolation probe cell for the `cis_app` grant and the CIS canonical
  `rls_catalogue_rules.py` allowlist line ship with it (separate PRs in AA-CIS-Infra and AA-CIS-App). The DM-7.1
  retention row for `booking.tenant_erasure_log` is already on root master (contract PR); this lane's root PR only
  updates the DSAR runbook's Booking row.
- The CIS saga's `services/tenant_erasure/reports.py` docstring (lane L14) still names the old column list; its
  reader uses `to_jsonb` and `normalise_report` reads the contract keys, so it reads this view as is.
- No apply in this lane: the live step is a DRY RUN on the synthetic tenant; the apply belongs to the end-to-end
  offboarding drill after the tech lead approves the combined fingerprint.
- The AA-CIS-Infra registry change (owners.yaml row + isolation probe cell) edits the same `CONSUMER_GRANT_KEYS`
  assertion as the TripPlanner erasure lane's Infra PR (tp-030 grant): the two ship as one Infra PR. The resolved key
  tuple is `(..., "bk-042", "bk-057", "tp-015", "tp-020", "tp-022", "tp-030")`. The AA-CIS-App canonical
  `rls_catalogue_rules.py` line merges before the Infra copy (the Infra copy check compares with CIS main).
- Integration tests pick a queued job by its key (request id + mode), never by `created_at` (the SQL order
  stability lint refuses a time-only ORDER BY in a DB test).
- Not covered by the anonymisation: version / itinerary text (locked once approved or sent), operations file names.
