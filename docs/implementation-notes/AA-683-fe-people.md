# AA-683 lane fe-people: customers and staff pages (Wave 1)

Scope: `/customers`, `/customers/[id]`, `/staff`, `/staff/[cisUserId]` on top of the scaffold branch
`feat/aa-683-staff-admin-frontend`. Sources: UI spec S236 §3, §4.12, §4.13, §5, §6; module spec
`02-users.md`; Figma `USER MANAGEMENT-1`, `USER  DETAILS`, `ADMINS`, `USER DETAILS`, `BLOCK ADMIN`;
backend routers `customers.py` and `staff.py` on `feat/aa-681-staff-auth-trip-case-versions` (bk-005).

## Decisions

- Customer tabs (All / Leads / Active / Past clients / Blocked) map to the API `status` filter computed in
  `booking.v_customer_summary`; tab counts are `limit=1` queries under the same search/filters (only `total` read).
- Customers are addressed in URLs by `CUS-` code when present, else UUID (the API accepts both).
- "+ New lead" and "Edit profile" share one drawer (`customers/_parts/CustomerFormDrawer.tsx`); the edit sends
  only changed fields. E-mail is locked for verified customers (API `email_locked`, Q14).
- Block / Unblock (PATCH `is_blocked`) and Archive (DELETE, soft) are admin-only (`customer.admin`) and live in
  a row "..." menu (`ui/booking/RowMenu.tsx`, additive) and the detail header. Archive refusal
  `customer_has_cases` tells the user to block instead. No hard delete (Q23).
- Merge review: detail page shows the candidate customer (link, or plain text if archived). Merge and
  "Mark reviewed" are wave 2; the panel says so instead of showing a dead button.
- Staff: Figma "Block / Suspend admin" becomes Activate / Deactivate (PATCH `active`); "Change role" is a drawer
  with a description per role. No invite / reset password: accounts live in CIS (ADR 0004 O1), so the page links
  to CIS admin. Self-deactivate / self-demote is disabled in the UI (API `cannot_demote_self`).
- Staff detail tabs: Overview (Booking-owned vs CIS-owned facts, CIS block marked with a lock), Assigned cases
  (`open_assignments`), Activity log (`GET /booking/audit-logs?actor=`, admin only, cursor "Load more").

## Changed

- `lib/api/client.ts` + `types.ts`: added `customers.create/update/archive` and their body/answer types (additive).
- Shared detail building blocks in `app/(staff)/_parts/` (`DetailHeader`, `FormField`), additive.

## Tradeoffs

- `/staff` filters client-side: `GET /booking/staff` returns every profile (one per CIS user who signed in),
  small enough not to need server paging.
- Customer CSV exports the current page only (server pagination); a full export needs a backend endpoint.

## Should know

- Route-level denial for roles without the page is handled by `AdminShell` + `lib/rbac.canSeePage`; the detail
  pages also render a 403 state (another staff profile for non-admins offers "Open my profile").
- "New trip case" links to `/cases/new?customer=<ref>`; the Cases lane's `/cases/new` does not read that
  parameter yet, so the customer is not pre-selected (follow-up for that lane).
- Checks: `npm run lint`, `npm run typecheck`, `npm run build` all pass. Not verified against a live backend
  in this lane.
