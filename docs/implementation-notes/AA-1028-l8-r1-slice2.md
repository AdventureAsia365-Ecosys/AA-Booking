# AA-1028 R1 slice 2 (AA-Booking) — bk_tenant_pull: booking.b2b_partners as the projection of every CIS tenant

Lane l8-r1-slice2 of S266. Root ADR 0019 d1 / d9. Two PRs: the migration-only PR (bk-037, MIG-4) and the code PR.

## Decisions

- **bk-037 adds `booking.b2b_partners.projected_at`** (nullable): the job cannot mark its rows through `created_by` /
  `updated_by` (both reference `booking.staff_users`). NULL = a row staff registered that the pull has not projected.
- Contract class (ALTER of an existing table): migration-only PR first, then the code PR raises readiness to bk-037.

## Should know

- Apply on Dev: the bot copy to AA-CIS-Infra `db/migrations/booking/`, then DB Migrate apply of
  `booking/037_b2b_partner_projection.sql` (contract auto-apply is off until 10/11) while every job table is idle.
