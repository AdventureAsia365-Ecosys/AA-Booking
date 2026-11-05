# AA-1037 agency invoices read on booking.b2b.v1 (S270, lane aa1037-b2b-agency-invoices-read)

Source: root ADR 0019 d17 and the S265 re-scope ("the data for the Bookings with AA page": booking status AND the
settlement invoices). Before this lane `booking.b2b.v1` had no agency-facing invoice read (the agency only saw its
credit status in `GET /terms`). Contract minor **1.5** (1.4 was AA-1030 G3, Booking #192 / root #564). No migration.

## Decisions

- **Three routes, scope `read`**: `GET /invoices` (page, no lines), `GET /invoices/{number}` (detail) and the optional
  `GET /billing-status`. The billing status is kept small (the `/terms` `credit` object + `paid_through`) because
  `/terms` already answers the credit stop; `paid_through` (the software fee) was the only missing field.
- **Scope = the partner `app.b2b.access` resolved**, exactly like `/holds/{id}`: the SQL filters by that partner's
  `tenant_id` (`agency_invoice.party_tenant_id`), never by anything in the request. One CRM client serves every
  agency, so the tenant header picks the partner and the access check binds it to the token's client.
- **Drafts never cross**: list and detail pass `statuses = ('issued', 'paid', 'void')` to SQL; the `status` filter is a
  closed enum of the same three (a `draft`, `overdue` or `all` ask is 422 before any read).
- **One 404 for everything not readable**: a draft (by id), another agency's number, an unknown number and a string
  that is not `AA[ST]-YYYY-NNNNNN` all answer 404 `invoice_not_found` (a new Problem code, as the brief asked; the
  path has no pattern so a malformed number is not a different 422). A malformed number is refused without a read.
- **Allow-list in two layers**: `app.b2b.invoices` copies contract fields key by key into a new dict (the issuer
  snapshot too, `ISSUER_KEYS`, so a key added to the snapshot later stays inside Booking), and the router's
  `extra="forbid"` models are the second guard (an unexpected key would be a 500, never a leak).
- **`bill_to` left out entirely**, including the caller's own: it is the agency's billing contact (`pii:contact`,
  name, address, government id) and the B2B contract carries no personal data (root contracts CI flags
  `billing_email`). The agency's own billing details are in CIS already.
- **Payments**: approved only, `received_on` + `amount` + `currency`. A recorded payment waiting for the checker is not
  shown (the brief: approved payment dates and amounts) and neither is the waiting total.
- **Lines** carry `line_no`, `line_role`, `description`, `quantity`, `unit_price`, `amount`, `hold_id` (the source hold),
  `booking_id` (the hold's trip case id = `Booking.booking_id` of the contract) and `voided`. `plan_key`, `product`,
  `created_by` and the internal `trip_case_code` are not added (the agency knows the booking by `booking_id`).
- **`period_end` is inclusive** on the B2B answer (the last day covered), like `period_last_day` in the accountant
  export; the stored `period_end` is exclusive. `paid_through` of the view is already inclusive.
- **Money as decimal strings with 2 places** in the invoice currency (one `currency` per invoice) rather than `Money`
  objects: an invoice has a single currency, so the flat shape is lighter for the CRM; payments carry their currency.
- **Offset paging** with `total` (reuses `list_invoices`); an agency has a few invoices a month, so a cursor is not
  needed. `limit` 1-100 (default 50), `offset` 0-10,000.
- **`Cache-Control: no-store`** on the three 200 answers (financial data; same rule as the customer routes).

## Changed

- `app/agency_billing/invoices.py`: `list_invoices(statuses=...)` (opt-in; the staff list is unchanged),
  `get_for_agency(number, tenant_id)`, `AGENCY_PAYMENT_SQL`, `NUMBER_RE`, and `booking_id` added to `LINE_SQL` (the
  staff routes return dicts, so the extra key is harmless there).
- `app/b2b/invoices.py` (new), `app/b2b/errors.py` (`invoice_not_found` 404), `app/routers/b2b.py` (models + routes),
  `docs/api/openapi.json` regenerated with the pinned FastAPI 0.142.2 / Pydantic 2.13.5.
- Root repo: `docs/contracts/booking.b2b.v1.openapi.yaml` 1.5, samples (valid detail; staff-note and bill-to leaks
  refused), contract policy forbidden keys for this contract, contracts README, DM-7.1 note (no new table).

## Tradeoffs

- A new Problem code (`invoice_not_found`) instead of the existing `not_found`: the brief named it, and the CRM can
  tell "no such invoice" from a missing booking; the cost is one more enum value (additive minor).
- The detail reads lines and payments in two more queries (three per request); an agency invoice has at most 500
  lines and the page is opened by a person, so no batching.

## Should know

- **Consumer**: the AA-Partner CRM "Bookings with AA" page (booking status + AA invoices) is not built here; the CRM
  lane calls these routes server to server with the agency's tenant header and must not cache invoice answers.
- The integration test `tests/integration/test_aa1037_b2b_invoices.py` proves partner A cannot read partner B's
  invoice (and the reverse), drafts never show, the staff fields never appear, and a suspended agency reads nothing.
- Live check after deploy: the drafted scripts of the session (`bk_aa1037_flow.py`) issue and pay synthetic agency
  invoices; the B2B read needs an `aa-m2m` token of the CRM client (the AA-Partner API calls it), so the live proof is
  either through the CRM or a client-credentials token fetched by the orchestrator.
