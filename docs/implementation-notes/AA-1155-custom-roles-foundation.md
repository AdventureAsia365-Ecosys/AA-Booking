# AA-1155 — custom staff roles, Booking migration (S286, lane l4-custom-roles-foundation)

Spec: root `.kiro/specs/AA-1155-custom-staff-roles/` (task T4). Decision record: root ADR 0006a A14 (Q2 option A).
This PR is migration-only (MIG-4); the Booking code (resolver, role editor, `me.permissions` pages) follows.

## Decisions
- bk-062 (contract): `booking.staff_role` (code, label, based_on, archived, stamps) + `booking.staff_role_capability`
  (custom roles only; built-in roles stay in `app/auth/staff.py` PERMISSIONS), the key-only view
  `booking.v_staff_role_capability` (role_code, label, based_on, archived, capability) to `cis_app` and `booking`, and
  `staff_users_role_chk` widened to custom codes (the resolved role of a `booking-custom` holder).
- Code pattern `^custom_[a-z][a-z0-9_]{1,46}$`, the same as CIS 462 (`shared.staff_app_grant_role_chk` accepts it).
- The approve class is refused in the DB too (`staff_role_capability_not_approve_chk`): `*.approve`, `*.approve_<x>`,
  `supplier.activate`, `contract.publish`, `stop_sale.approve`, `payout.write`, `payment.verify`,
  `billing_config.write`, `staff.admin`. The editor's 400 (unknown or approve-class capability) stays the main gate.
- `based_on` may be any built-in role, `admin` included: it only records the clone source; the capability CHECK keeps
  approve rights out whatever the source.
- Seed `custom_product_ops_accounting` "Product, ops & accounting" (based_on contracting): the 24 capabilities of the
  S285 audit section 3.3 plus Q3 (`b2b_desk.book`, `trip_case.read/write`, `customer.read/write`).

## Changed
- Nothing beyond the issue. `supplier_bank.*` is not refused by the CHECK: Q10 may later add `supplier_bank.write`
  (entry only) to this role.

## Tradeoffs
- `pricing.write` and `stop_sale.write` still carry approval paths in today's code (pricing rules / FX approval,
  stop-sale review). The AA-1150 Booking change splits `stop_sale.approve`, `supplier.activate` and
  `contract.publish` out of the write rights; the code lane must confirm that a custom holder of `pricing.write` cannot
  approve a pricing rule (maker-checker) before the role is assigned.

## Should know
- Renumber if bk-062 is taken at merge: `git grep '062_'` and `bk-062` (this file, the migration, the integration
  test, AA-CIS-Infra `db/owners.yaml` / `db/isolation_probe.py`, root ADR 0006a A14 and the spec).
- DM-7.1 rows for the two tables are in the root PR of this lane (merged before the Infra copy).
