# AA-1028 R1 slice 2 (AA-Booking) — bk_tenant_pull: booking.b2b_partners as the projection of every CIS tenant

Lane l8-r1-slice2 of S266. Root ADR 0019 d1 / d9. Two PRs: the migration-only PR (bk-037, MIG-4) and the code PR.

## Decisions

- **bk-037 adds `booking.b2b_partners.projected_at`** (nullable): the job cannot mark its rows through `created_by` /
  `updated_by` (both reference `booking.staff_users`). NULL = a row staff registered that the pull has not projected.
- Contract class (ALTER of an existing table): migration-only PR first, then the code PR raises readiness to bk-037.

## Should know

- Apply on Dev: the bot copy to AA-CIS-Infra `db/migrations/booking/`, then DB Migrate apply of
  `booking/037_b2b_partner_projection.sql` (contract auto-apply is off until 10/11) while every job table is idle.

## Code PR: bk_tenant_pull

### Decisions

- **The contract's view part, not HTTP.** Booking reads CIS through `catalog.*` contract views already (role
  `booking` has USAGE on `catalog`, root owners.yaml consumer_grants "catalog.* (schema + views)"), so the new CIS view
  `catalog.v_tenant_account_v1` (CIS 357) serves the account facts: no Cognito client, scope, secret or Infra PR.
- **Full reconcile each run** (tens of tenants, one transaction) instead of a cursor feed: idempotent, no cursor table.
- **Status = in service AND `trade.book`.** ADR 0019 d9 gives every tenant a row (`m2m_client_id` = the AA-Partner
  client); design §3.5 says `trade.book` on upserts the partner, and suspended means "no new hold". Both hold: the row
  exists for every tenant, `active` only when the tenant may book.
- **AA itself (kind `internal`) gets no row**; demo tenants (E2E) get one like an agency.
- **A tenant gone from CIS is suspended, never deleted** (no DELETE grant; quotes and holds reference the partner),
  but only for a row the pull projected before (`projected_at` set).
- **Adoption guard (fix pass 2, re-review finding 1).** A staff-registered row (`projected_at` NULL) whose tenant is
  not in `catalog.v_tenant_account_v1` is left untouched: no status change, no `projected_at` stamp, no audit row; it
  is counted `awaiting_cis` (log `bk_tenant_pull_awaiting_cis` lists up to 20 tenant ids). Without it the first run
  would have suspended and stamped rows that never had a CIS tenant (the CRM-seeded E2E agencies such as 'Lữ hành
  Sen Vàng' bab16039-a064-517f-a99f-172b729ad3c4, southern-cross, harbour-lights, and the AA-972 synthetic partner
  00000000-0000-4000-8000-00000000c972), locking staff out with 409 `projected_from_cis` and breaking every B2B call
  for them (403 `unknown_partner`). Same rule as the CRM adoption guard. Once CIS holds the tenant the next run adopts
  the row; only from then on does its tenant leaving CIS suspend it.
- **Projected fields read-only for staff on a projected row** (409 `projected_from_cis`); commercial terms stay staff's;
  a staff-registered row stays editable until the pull adopts it (only rows of tenants CIS holds are adopted).
- **created_by / updated_by left alone** (they reference `booking.staff_users`); the audit row names the job.

### Should know

- Order: bk-037 applied (DB Migrate) and CIS 357 applied -> this PR (readiness requires bk-037). Before CIS 357 the
  hook queues nothing and a run answers `skipped`.
- First run on Dev: one row per CIS tenant (count = CIS tenants minus the internal one). An existing staff row is
  adopted only when CIS holds its tenant (its `m2m_client_id` becomes the CRM client, its name the CIS name). Staff rows
  without a CIS tenant (the CRM-seeded agencies Sen Vàng / southern-cross / harbour-lights where they have a partner
  row, the AA-972 synthetic partner c972) stay exactly as they are, active and staff-editable: the run result shows
  them as `awaiting_cis`. Live check after deploy: the run result's `awaiting_cis` >= 1, the c972 row still `active`
  with `projected_at` NULL, and a B2B call for Sen Vàng still passes.
