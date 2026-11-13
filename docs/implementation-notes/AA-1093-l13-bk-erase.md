# AA-1093 lane L3 (S279 l13-bk-erase): the Booking step of the tenant offboarding erasure

Root plan: `AA-Ecosys/docs/audits/2026-11-13-S279-session/aa1093-plan.md` Part 2, lane L3. Tech lead decisions (S279):
D3 (Booking anonymises, keeps money rows and the invoice bill-to under the accounting exception) and D8 (the saga
refuses the pilot tenant; on Dev, apply only for synthetic tenants).

Two PRs (MIG-4): the migration-only PR `bk-057` (`migrations/057_tenant_erasure_log.sql` + its schema test), then
this code PR (`app/b2b/tenant_erase.py`, the pull skip, readiness bk-057).

## Decisions

- **Key bk-057** (main max + 1; bk-056 is the S279 thresholds file). The B2C table drop takes bk-058.
- **Contract, not expand.** The lane brief said expand, but the file cannot be: the expand lint of AA-CIS-Infra refuses a
  grant to another owner's role (`cis_app`, the report view reader) and any trigger / grant on an existing table; the
  erasure also needs new triggers and column grants on `audit_logs` and `trip_case_status_history`. Preflight with the
  pinned Infra tools says `contract`. On Dev it is applied by Infra Migration Apply once every job table is idle.
- **Append-only tables keep their rule.** `audit_logs` (bk-001) and `trip_case_status_history` (bk-003) are append-only
  for role `booking`. The redaction gets column UPDATE grants (`before`, `after`, `entity_label`; `note`) behind a
  BEFORE UPDATE guard that refuses unless the transaction set `booking.tenant_erase` and then lets only NULL or the
  marker `{"redacted":"tenant_erased"}` through (the bk-052 "setting opens a narrow path" pattern). A tampering UPDATE
  of any other column still fails on privileges (42501); `has_table_privilege(booking, audit_logs, UPDATE)` stays false
  (bk-014 test unchanged).
- **The log table has RLS** (tenant_id column: rule a of the RLS catalogue guard), the bk-049 policy pair, so the view
  owned by `bk_owner` answers every row to `cis_app` (no tenant bound).
- **What "redact notes / cancel_reason on the agency's B2B trip cases" covers.** `trip_cases` has no `cancel_reason`; a
  case's cancel reason is a status history note, and supplier bookings of the case carry `cancel_reason`, `notes` and
  `voucher_note`. All of them are redacted. `inquiry_snapshot` of a B2B case is cleared too (free text when present).
  A cancelled supplier booking keeps `cancel_reason = 'Erased'` (bk-026 CHECK).
- **Notifications** about the cases / checks / supplier bookings lose `message` and every `data` key except
  `reminder_key` (the unique reminder index would otherwise let a daily reminder fire again). Titles are generic.
- **Audit redaction scope**: entity ids of the cases, their versions, checks, supplier bookings and the partner;
  `entity_label` is cleared only on partner rows (it is the agency name; a case label is its code). `summary` stays
  (system summaries carry no names; the guard keeps it unchanged).
- **Plan hash**: SHA-256 of canonical JSON (tenant id + per step name, action, count, md5 fingerprint). The fingerprint
  uses the microsecond epoch of each row's time, never `timestamptz::text`, so a CLI session and the worker (different
  `TimeZone`) compute the same hash. Kept rows are counted but not hashed (a final invoice paid after offboarding must
  not invalidate the approval of the erasure).
- **Guards fail closed**: account view unreadable -> `account_unknown`; tenant missing -> `tenant_unknown` (Booking runs
  before CIS erases the tenant); `is_pilot` -> always refused; not `is_synthetic` -> refused unless
  `ENVIRONMENT=prod` exactly. A DRY RUN is never refused, it reports `apply_allowed` / `refused`.
- **Requests from CIS**: `catalog.v_tenant_erasure_v1` is not on CIS main yet (lane L2). Booking feature-detects the
  columns `request_id`, `tenant_id`, `phase` (`plan` | `apply` | anything else ignored) and `approved_plans` (jsonb,
  key `booking` = the approved hash). A different shape degrades to CLI only. **The CIS lane and the contract
  tenant.erasure.v1 (lane L0) must use these names**, or this reader must follow theirs.
- **Kill switch**: `TENANT_ERASE_APPLY_ENABLED` (Settings `tenant_erase_apply_enabled`, default off) gates the job apply
  only; the CLI apply needs the approved hash. Root thresholds audit section 4 item 7 keeps destructive kill switches
  in Terraform. Not set anywhere yet.
- **Thresholds rule**: no new business threshold. `MAX_ATTEMPTS` and `SLOT_SECONDS` carry `# technical-constant:`
  (job mechanics, like every kind; job-kind settings are the audit's follow-up).

## Changed

- `bk_tenant_pull` reads the applied tenants (`tenant_erase.erased_tenants`, empty before bk-057) and skips them
  (`erased_skipped`), both in the projection and in the "left CIS" suspension.
- New job kind `bk_tenant_erase` (kinds.py 300 s, worker handler, `EraseSchedule` hook); readiness raised to bk-057.

## Tradeoffs

- One transaction per tenant (Booking holds tens of rows per agency) instead of batches: simplest to make all or
  nothing and to compare the hash inside the same snapshot.
- The `tenant_erase` setting is a speed bump, not a security boundary: anyone holding role `booking` could set it. The
  guard still limits what such an update can write (NULL / marker only), so it can only erase, never forge.

## Should know

- Apply order on Dev: bk-057 (migration PR, Migration Apply) -> this PR (readiness bk-057, so Deploy Dev waits for it).
  The Infra owners registry row + isolation probe cell for the new `cis_app` grant and the CIS canonical
  `rls_catalogue_rules.py` allowlist line ship with it (separate PRs in AA-CIS-Infra and AA-CIS-App), and the DM-7.1
  retention row in the root repo before the Infra copy.
- No apply in this lane: the live step is a DRY RUN on the synthetic tenant `crm-sen-vang`; the apply belongs to the
  end-to-end offboarding drill after the tech lead approves the combined fingerprint.
- Not covered by the anonymisation (documented in CONTEXT "Tenant erasure"): version / itinerary text (locked once
  approved or sent), cancellation waive and desk override reasons (money records), operations file names.
