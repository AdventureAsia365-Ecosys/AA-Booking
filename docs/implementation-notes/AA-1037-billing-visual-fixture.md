# AA-1037 lane billing-visual-fixture (S270): agency billing in the visual run, and the `?tenant=` deep link

Frontend only, no migration, no API change. Closes the S269 left-list item "layout-mock fixture for the agency billing
pages" and gives CIS Tenant 360 ("Open in Booking", lane cis-tenant360-billing-trade) a filtered invoices list.

## Decisions

- **Seven visual pages, not two.** The page guard (ui/booking/visualPages.test.mjs) allows several entries per route
  (the kit gallery already has three), so `/agency-billing` is captured per tab (invoices, payments, agencies,
  settings) plus the one-agency deep link (`?tab=invoices&tenant=<fixture tenant>`, the chip), and
  `/agency-billing/invoices/[ref]` both as a software-fee invoice (AAS-2026-000001) and as a settlement invoice
  (AAT-2026-000001, VND, six lines with a voided one). The two EXCLUDED entries are gone.
- **Fixture data.** Six invented agencies with every billing status (ok / warn / past_due), both kinds and both
  currencies, drafts without a number, an overdue issued invoice, paid and void rows, payments in every state (one
  recorded by the signed-in admin, so the "Another person approves" cell shows), two reminders (sent redirected, skipped
  no_billing_email), an invented issuing entity with an invented payment line ("Example Bank", "EXAMPLE-ACCOUNT-01"),
  three tax rules, default terms plus two agency overrides, an approved and a draft terms clause (draft by another
  person, so Approve shows). Tenant ids are valid UUIDs with letters in every group (no long digit run, lesson 179).
  The fixture filters `agency-invoices` by `tenant_id` like the API, and `agency-payments` by `state`.
- **Permissions in `e2e/layout/staff-me.json`.** Added `agency_billing.read`, `agency_billing.write`,
  `agency_payment.approve`, `billing_config.write`, `terms_clause.approve` (backend `app/auth/staff.py`: admin holds all
  five). Only the agency billing pages read them (`useCan` grep), and the nav is role-based (`lib/rbac.ts`), so no other
  page's capture changes. The visual run has no pixel baselines (it checks overflow, console, requests, frame), so
  there is nothing to accept.
- **`?tenant=` parsing.** `ui/booking/agencyFilter.ts` `tenantFilter`: a UUID (trimmed, lower-cased) or null. A
  malformed value is dropped, not sent: the API types `tenant_id` as a UUID and would answer 422, which would show an
  error instead of the list (the link degrades to the unfiltered list). The value goes to `GET /agency-invoices` as
  `tenant_id` (the backend already filters `party_tenant_id`) and is part of the query key.
- **Chip.** Kit `ChipButton` (selected) inside a kit `Field as="div"` labelled "Agency", with an X icon; a click removes
  the `tenant` key (useUrlFilters also resets the page). The name comes from `GET /agency-billing/status` (every
  partner has a row; same query key as the Agencies tab, fetched only while the filter is on), else from a listed
  invoice, else "One agency": never the raw id. Accessible name "<agency>: remove the agency filter".
- **Only the Invoices tab reads `tenant`.** The CIS link opens `tab=invoices`; Payments / Agencies keep their lists
  (the scope asked for the invoices filter only).

## Changed

- Found by the new capture: at 390 px the four KPI tiles of Agency billing sat in two columns and the money values
  ("18,450.00 USD" in Poppins at the tile size) pushed the page frame to 452 px. The grid is now one column on a phone
  and two from `sm`, four from `lg` (the billing page's pattern, `grid gap-3 sm:grid-cols-2 xl:grid-cols-4`).

## Tradeoffs

- Name lookup through the status list (all agencies) rather than a new "one partner" endpoint: no API change; the list
  is one row per agency and already cached when staff visit the Agencies tab.
- A per-tab capture costs 4 extra pages x 4 shots on every full visual run (~30 s); worth it, as the tabs render
  different tables.

## Should know

- The live check 12 screenshots (`.tmp-session/S270/live/aa1037/aa1037_shots.mjs`) should run after this ships, so
  the invoices list is filtered to the invented tenant.
- Local run: `npx next build`, then `KIT_GALLERY=1 VISUAL_GALLERY=1 npx playwright test --project=visual-1440
  --project=visual-390` (LAYOUT_PORT / LAYOUT_STUB_PORT to avoid another lane's server: `reuseExistingServer` is on
  outside CI).
