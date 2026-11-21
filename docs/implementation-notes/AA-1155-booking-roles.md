# AA-1155 T5 / T6 / T7 — Booking custom staff roles: resolver, role editor, pages by capability (S287, lane l1)

Root spec `.kiro/specs/AA-1155-custom-staff-roles` tasks T5, T6, T7 (requirements R2.2, R2.3, R4.1-R4.5; design D2-D4;
root ADR 0006a A14 / A10g). No migration: bk-062 (tables, view, seed, `staff_users_role_chk`) is applied on Dev; CIS 462
(`shared.v_staff_booking_grant`) too.

## Decisions

- **Resolver (`app/auth/custom_roles.py`).** A WORKFORCE token with `booking-custom` and NO built-in `booking-*` group
  is a custom holder. A built-in group always wins (the view is never read), so an administrator never depends on the
  tables (design D2). Resolution: the view row by the token `sub` -> a code matching `^custom_[a-z][a-z0-9_]{1,46}$` ->
  the role row (not archived) -> its capabilities narrowed to known, non-approve ones. No row / a built-in code / an
  unknown or archived role: 403 `no_booking_role`. A database error on the view or the tables: 403
  `custom_role_unavailable` (R4.2 "refuse"), never cached. The staff row is synced to the custom code
  (`staff.role_synced` audit, summary names the CIS grant).
- **Cache: 60 s per process, a guard, not a setting.** R4.1 says "cached at most 60 seconds": it is the spec's upper
  bound on how long a revoked or archived role keeps working, so `CACHE_S` is a `technical-constant` (raising it would
  weaken revocation; lowering it only costs reads). The role editor drops the cache of the process that saved.
- **`Staff.can` for a custom holder** answers from `Staff.custom_permissions` (session field, like `e2e_grants`); the
  built-in matrix is not consulted. `/booking/staff/me` adds `role_label` (null for built-in roles).
- **Owner marker** (`is_owner`, role editor gate) is read from the same view UNCACHED on every editor request: an owner
  change in CIS applies to the next write. An unreadable view = not an owner (fail closed).
- **Role editor (`app/auth/role_editor.py`, `app/routers/staff_roles.py`).** `GET /booking/staff-roles` (staff.admin or
  owner), `POST` (clone: code, label, based_on, optional full capability list; default = the built-in role's
  capabilities minus the approve class), `PATCH /{code}` (label, full capability set, archived). Writes are owner only
  (403 `owner_only`), validated before any write: 400 `invalid_role_code`, `invalid_label`, `unknown_base_role`,
  `unknown_capability`, `approve_class_capability` (new `app.errors.BadRequest`, status 400 as the spec says). One audit
  row per change (`staff_role.created` / `staff_role.updated`, before / after = code, label, based_on, archived,
  capabilities; `entity_label` "<label> (<code>)", the code is not a UUID).
- **A10g (custom-role maker, Dev only, groups exactly `cis-custom` + `booking-custom`).** Allow-listed writes: create a
  SYNTHETIC supplier (A10e-3 body check) and deactivate its own (`record_owner` = its address, so the supply synthetic
  SQL checks its own creator). Every other write it may send is an **approve probe**: a route whose gate needs an
  approve-class capability (read from the route: `require()` now marks its dependency with `.permission`) or a
  `SERVICE_PROBES` route (supplier / hotel / service activate, contract publish, stop-sale review). A probe is answered
  403 `forbidden` from the session's capabilities right after the role is resolved, before any record, synthetic check
  or staff row is read: the same answer the route gives, and no real record is ever touched. A probe the session could
  pass (never for a custom role) or a probe route whose body approves nothing is 403 `e2e_read_only`. No route-scoped
  grant (A10g).
- **Frontend (T7).** `lib/rbac.ts` `PAGE_RULES` name capabilities (`perms`, any-of; empty = any signed-in staff);
  `canSeePage(permissions, path)`; the shell, sidebar, command palette, bottom nav and dashboard pass
  `me.permissions`. Each rule takes the read capability of the API the page calls, which reproduces the old role lists
  exactly for every built-in role (proved by `lib/rbacPermissions.test.mjs` against a frozen copy of the old table).
  New page **Admin -> Roles** (`/roles`, `staff.admin`): custom roles table (label, code, copy of, capability count,
  holders, status, edit / archive for owners), built-in roles read-only, a FormDrawer with capability checkboxes
  grouped by area; approve-class capabilities are shown disabled. Custom role names show by label on Staff, profile,
  the picker and the avatar (`roleName()`).

## Changed

- `/roles` instead of `/staff/roles`: the Staff nav entry would be active on both (prefix match).
- Staff list, profile and the staff picker carry `role_label` (read from `booking.staff_role` FAIL SOFT: the integration
  test with the table revoked caught that a plain join made the admin's `/booking/staff` fail, against R4.2).
- `require()` sets `dependency.permission` (read by the A10g probe and by a test that lists every approve-gated route).
- `frontend/lib/rbac.permissions.fixture.json` (the backend matrix per built-in role + the seed role) feeds the frontend
  tests; `backend/tests/unit/test_aa1155_frontend_fixture.py` fails when it drifts (rewrite command in the test).
- CI stub `tests/integration/stub_shared.sql` gains `shared.v_staff_booking_grant` over a stub table (CIS 462 shape).

## Tradeoffs

- Probe answered in `current_staff` vs letting the route refuse: the route gate would also say 403, but only after the
  A10e synthetic check (which has no rule for pricing / billing rows and would answer `e2e_synthetic_only`). Answering
  from the resolved capabilities proves exactly what the live check is for (the role holds no approval) and touches
  nothing. The route gates themselves are proven by the unit test that sends every approve-gated route with a custom
  holder (403 before any read) and by the integration test.
- Fail closed on a DB error is 403 (`custom_role_unavailable`), not 503: the spec says "refuse"; the detail tells the
  two cases apart in logs and on the sign-in error.
- Role editor reads for `staff.admin` too (not only owners): admins see what each role may do; only owners change.

## Should know

- Tests: unit `tests/unit/test_aa1155_custom_roles.py` (resolver, cache bound, fail closed, approve class, every
  approve-gated route 403 before any read, service-checked approvals, A10g probes / scope / env, labels fail soft),
  `tests/unit/test_aa1155_role_editor.py` (seed = code = migration, CHECK list = code list, 400s, owner only, audit
  before / after, cache drop, reads), `tests/unit/test_aa1155_frontend_fixture.py`; integration
  `tests/integration/test_aa1155_custom_roles.py` (seed role over HTTP incl. 200 on suppliers / supplier bills / agency
  invoices, inactive supplier + activate 403, approvals 403; grant revoked on the view and on the tables: holder 403,
  admin 200 on `/booking/staff`; archived / no grant; editor clone / tick / archive with audit; non-owner and
  unreadable view 403; DB CHECK); frontend `lib/rbacPermissions.test.mjs`, `ui/booking/staffRoles.test.mjs`.
- Readiness now requires bk-062 (already applied on Dev, `/booking/health/ready` showed bk-062 in S286).
- Live (after deploy): the A10g maker signs in on Dev (its CIS grant must name `custom_product_ops_accounting`): 200 on
  `/booking/suppliers`, `/booking/supplier-bills`, `/booking/agency-invoices`; 403 `forbidden` on the approve probes.
- Not here (other lanes / repos): the CIS Staff page pickers and Sync (T9), the CIS matrix page (T10). The spec's
  tasks.md lives in the root repo: the orchestrator ticks T5-T7.
