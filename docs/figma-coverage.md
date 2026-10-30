# AA-Booking: Figma coverage matrix

Status: the checklist of AA-996 (S254, Nghiệp's decision in S253: Booking covers **every** Figma screen). Every PNG
exported from the Figma file (`AA-Ecosys/docs/AAA/figma_AAA/`, 120 files) has exactly one row below. The route map and
the modal table of [`ui-spec.md` §3](ui-spec.md#3-route-map) say what each route does; this file says which route or
dialog answers each Figma frame.

Statuses:

- **built**: an existing route, drawer or dialog covers the frame.
- **new (AA-996)**: built by AA-996 (the Countries and Destinations pages, the country / destination profile drawers,
  the "From CIS" blocks of the content forms, Sign-in & security, Make not sellable, the country / destination
  deactivate dialogs).
- **replaced-by**: the frame asks for something Booking does differently on purpose, and the row names the Booking
  equivalent. Two rules drive every replacement:
  - **No hard delete** (ui-spec Q23): every "Delete …" dialog becomes deactivate, archive, cancel, void, "not
    sellable" or a soft delete of a draft. All of them use the Figma dialog layout (warning icon, title, the record
    name in bold, Cancel + a danger button) through `ui/booking/ConfirmDialog.tsx`.
  - **CIS owns accounts and content** (ADR 0004 O1 / O7, ADR 0002): passwords and invites become links to CIS →
    Platform → Staff; content fields are shown read-only with a "From CIS" tag and an "Edit in CIS ↗" link, while the
    Booking-owned commercial fields stay editable in the same Figma form layout.
- **duplicate / misfiled**: the export shows another module's screen (the row names the route that covers it).

How to keep it true: a new Figma export gets a row here in the PR that builds (or replaces) it; a route that changes
updates its rows.

| Figma file | Booking route / dialog | Status | Note |
|---|---|---|---|
| `ACTIVITY  DETAILS-1.png` | Deactivate service dialog | replaced-by | Unpublish → Deactivate. |
| `ACTIVITY  DETAILS-2.png` | Deactivate service dialog | replaced-by | Delete → Deactivate (rates and history kept). |
| `ACTIVITY  DETAILS.png` | `/services/[id]` | built |  |
| `ACTIVITY MANAGEMENT-1.png` | `/services` | built | Activities → Services (spec §2.2). |
| `ACTIVITY MANAGEMENT-2.png` | `/services` | built | Activities → Services (spec §2.2). |
| `ACTIVITY MANAGEMENT.png` | `/services` | built | Activities → Services (spec §2.2). |
| `ADD COUNTRY.png` | Country profile drawer (`/catalog/countries`, `/catalog/countries/[iso]`) | new (AA-996) | CIS fields read-only ("From CIS"); Booking status, area, internal notes editable. Countries are not created in Booking. |
| `ADD DESTINATION.png` | Destination profile drawer (edit) | new (AA-996) | Name, country, coordinates, photos read-only "From CIS"; status + internal notes editable. New places are created in CIS. |
| `ADD NEW ACTIVITY-1.png` | Edit service drawer (`/services/[id]`) | new (AA-996) | Published CIS content read-only + "Edit in CIS ↗". |
| `ADD NEW ACTIVITY-2.png` | Edit case details drawer (`/cases/[code]`) | built | Misfiled: it is "Edit Trip Case". |
| `ADD NEW ACTIVITY.png` | Add service drawer (`/services`) | new (AA-996) | Commercial facts editable; customer text "From CIS" note (CIS drafts it after save). |
| `ADD NEW HOTEL.png` | `/hotels?new=1` | built |  |
| `ADD NEW TRIP 8.png` | `/cases/new` | built | AA-996 slice 2: optional "Start from a trip plan" (sellable plans) seeds V1 from the plan. |
| `ADD SUPPLIER.png` | `/suppliers/new` | built |  |
| `ADMINS.png` | `/staff` | built |  |
| `AUDIT LOGS.png` | `/audit-log` | built |  |
| `Activity Log.png` | `/staff/[cisUserId]?tab=activity` | built |  |
| `Assigned Trips-1.png` | `/cases/[code]` workspace layout | built |  |
| `Assigned Trips.png` | `/staff/[cisUserId]?tab=cases` | built |  |
| `BLOCK ADMIN.png` | Deactivate staff member dialog | replaced-by | Block → Deactivate (Booking profile only; the CIS account is untouched). |
| `CANCEL INVOICE.png` | Cancel invoice dialog | replaced-by | Cancel (reason, nothing paid) instead of delete. |
| `COUNTRY DETAILS-1.png` | `/catalog/countries/[iso]` | new (AA-996) |  |
| `COUNTRY DETAILS.png` | `/catalog/countries/[iso]` | new (AA-996) | Drawer variant folded into the detail page (spec 06 recommendation). |
| `COUNTRY MANAGAMENT-1.png` | `/catalog/countries` | new (AA-996) | Countries from CIS places + Booking status. |
| `COUNTRY MANAGAMENT-2.png` | `/hotels` (grid) | duplicate / misfiled | Misnamed: Hotel Management list. |
| `COUNTRY MANAGAMENT-3.png` | `/hotels` (grid) | duplicate / misfiled | Misnamed: Hotel Management list. |
| `COUNTRY MANAGAMENT.png` | `/catalog/countries` | new (AA-996) | Countries from CIS places + Booking status. |
| `CREATE INVOICE.png` | Create invoice stepper (case Billing tab) | built |  |
| `CREATE NEW TEMPLATE-1.png` | Commercial drawer (`/catalog/trip-plans/[sourceId]`, `…/commercial`) | new (AA-996) | Figma form layout: tour content (title, duration, country, summary, days, inclusions / exclusions) read-only "From CIS" + "Edit in CIS ↗"; commercial fields editable: Supplier (active suppliers) and the per-person price matrix (channel × season / validity × pax band × currency, bk-032) replace the Figma Supplier and Pricing cards (AA-996 slice 2). |
| `CREATE NEW TEMPLATE-2.png` | Commercial drawer (`/catalog/trip-plans/[sourceId]`, `…/commercial`) | new (AA-996) | Figma form layout: tour content (title, duration, country, summary, days, inclusions / exclusions) read-only "From CIS" + "Edit in CIS ↗"; commercial fields editable: Supplier (active suppliers) and the per-person price matrix (channel × season / validity × pax band × currency, bk-032) replace the Figma Supplier and Pricing cards (AA-996 slice 2). |
| `CREATE NEW TEMPLATE.png` | Commercial drawer (`/catalog/trip-plans/[sourceId]`, `…/commercial`) | new (AA-996) | Figma form layout: tour content (title, duration, country, summary, days, inclusions / exclusions) read-only "From CIS" + "Edit in CIS ↗"; commercial fields editable: Supplier (active suppliers) and the per-person price matrix (channel × season / validity × pax band × currency, bk-032) replace the Figma Supplier and Pricing cards (AA-996 slice 2). |
| `DELETE ACTIVITY.png` | Deactivate service dialog | replaced-by | Delete → Deactivate (rates and history kept). |
| `DELETE COUNTRY.png` | Deactivate country dialog | replaced-by (new (AA-996)) | Booking status → inactive (soft); reactivation is the same dialog. |
| `DELETE DESTINATION.png` | Deactivate destination dialog | replaced-by (new (AA-996)) | Booking status → inactive (soft). |
| `DELETE HOTEL.png` | Deactivate hotel dialog | replaced-by |  |
| `DELETE ITINERARY BUILDER.png` | Delete draft version dialog | replaced-by | Soft delete of an unsent draft (`deleted_at`); a sent version is never deleted. |
| `DELETE SUPPLIER.png` | Archive customer dialog | duplicate / misfiled | Misnamed export: it is the Delete User dialog on the customer directory (spec 08). The supplier equivalent is DELETE USER-1 → Deactivate supplier. |
| `DELETE TRIP.png` | Make not sellable dialog (`/catalog/trip-plans`, trip plan detail) | replaced-by (new (AA-996)) | Drawn over the Trip Templates list: the trip plan stays in CIS, Booking stops selling it. A trip case is cancelled (status), never deleted. |
| `DELETE USER-1.png` | Deactivate supplier dialog | replaced-by | Off-module export (Suppliers list). |
| `DELETE USER.png` | Archive customer dialog | replaced-by | Soft delete (archive); Block when the customer has cases past new_inquiry. |
| `DELETE.png` | Void payment dialog | replaced-by | Delete Payment → Void (kept, audited). |
| `DESTINATION DETAILS.png` | Destination drawer (view) | new (AA-996) |  |
| `DESTINATION MANAGEMENT-1.png` | `/catalog/destinations` | new (AA-996) |  |
| `DESTINATION MANAGEMENT.png` | `/catalog/destinations` | new (AA-996) |  |
| `EDIT ACTIVITY.png` | `/services` | built | Activities → Services (spec §2.2). |
| `EDIT COUNTRY.png` | Country profile drawer (`/catalog/countries`, `/catalog/countries/[iso]`) | new (AA-996) | CIS fields read-only ("From CIS"); Booking status, area, internal notes editable. Countries are not created in Booking. |
| `EDIT DESTINATION.png` | Destination profile drawer (edit) | new (AA-996) | Name, country, coordinates, photos read-only "From CIS"; status + internal notes editable. New places are created in CIS. |
| `EDIT HOTEL-1.png` | `/hotels?view=[id]` hotel drawer | built |  |
| `EDIT HOTEL.png` | `/hotels?edit=[id]` | built | Its Delete button → Deactivate hotel. |
| `EDIT PAYMENT.png` | Edit payment dialog | built |  |
| `EDIT PROFILE.png` | Staff role / activation dialog (`/staff`, `/staff/[cisUserId]`); profile read-only in `/settings` | built |  |
| `EDIT SUPPLIER.png` | `/suppliers/[id]/edit` | built |  |
| `EDIT TEMPLATE-1.png` | Commercial drawer (`/catalog/trip-plans/[sourceId]`, `…/commercial`) | new (AA-996) | Figma form layout: tour content (title, duration, country, summary, days, inclusions / exclusions) read-only "From CIS" + "Edit in CIS ↗"; commercial fields editable: Supplier (active suppliers) and the per-person price matrix (channel × season / validity × pax band × currency, bk-032) replace the Figma Supplier and Pricing cards (AA-996 slice 2). |
| `EDIT TEMPLATE-2.png` | Commercial drawer (`/catalog/trip-plans/[sourceId]`, `…/commercial`) | new (AA-996) | Figma form layout: tour content (title, duration, country, summary, days, inclusions / exclusions) read-only "From CIS" + "Edit in CIS ↗"; commercial fields editable: Supplier (active suppliers) and the per-person price matrix (channel × season / validity × pax band × currency, bk-032) replace the Figma Supplier and Pricing cards (AA-996 slice 2). |
| `EDIT TEMPLATE.png` | Commercial drawer (`/catalog/trip-plans/[sourceId]`, `…/commercial`) | new (AA-996) | Figma form layout: tour content (title, duration, country, summary, days, inclusions / exclusions) read-only "From CIS" + "Edit in CIS ↗"; commercial fields editable: Supplier (active suppliers) and the per-person price matrix (channel × season / validity × pax band × currency, bk-032) replace the Figma Supplier and Pricing cards (AA-996 slice 2). |
| `Group 1410084962.png` | `/login` (background) | built |  |
| `HOTEL DETAILS.png` | `/hotels?view=[id]` hotel drawer | built |  |
| `HOTEL MANAGEMENT.png` | `/hotels` | built | Photo grid by default (AA-994 S6d, S254 audit #19): first supplier photo or the neutral tile, photo count, place chip, name, code, stars, supplier, status, updated, row menu; a Grid / Table switch (`?view=table`) keeps the same filters, paging and CSV. |
| `ITINERARY BUILDER-1.png` | `/proposals` (+ row actions) | built |  |
| `ITINERARY BUILDER-2.png` | Send proposal dialog | built |  |
| `ITINERARY BUILDER-3.png` | `/cases/[code]/proposals/[n]` builder | built |  |
| `ITINERARY BUILDER-4.png` | `/cases/[code]/proposals/[n]` builder | built |  |
| `ITINERARY BUILDER.png` | `/proposals` (+ row actions) | built |  |
| `ITINERARY PREVIEW.png` | `/cases/[code]/proposals/[n]/preview` | built |  |
| `NOTIFICATION.png` | `/notifications` | built |  |
| `OVERVIEW-1.png` | Notification popover (topbar bell) | built |  |
| `OVERVIEW.png` | `/` Dashboard | built |  |
| `PAYMENT & INVOICE-1.png` | `/billing` | built | Row menu on the invoice and payment rows (S255 rescore, AA-994 S6d): invoice = View invoice, Open trip case, Cancel invoice (when allowed); payment = View payment, View invoice, Download evidence (when a file is attached), Void (when allowed). No invoice PDF exists yet, so no Download PDF item. |
| `PAYMENT & INVOICE-2.png` | `/billing` | built |  |
| `PAYMENT & INVOICE-3.png` | `/billing` | built |  |
| `PAYMENT & INVOICE.png` | `/billing` | built | Row menu: see `PAYMENT & INVOICE-1.png`. |
| `PAYMENT DETAILS.png` | `/billing/payments/[ref]` | built |  |
| `PAYMENT MANAGEMENt.png` | `/billing` | built |  |
| `RECORD PAYMENT.png` | Record payment dialog | built |  |
| `RESET PASSWORD.png` | `/settings?tab=security`, staff row "Sign-in & invites in CIS ↗" | replaced-by (new (AA-996)) | No password exists to reset: the company e-mail code is sent by Cognito; access is managed in CIS → Platform → Staff. |
| `SEND REMAINDER.png` | Send reminder dialog | duplicate / misfiled | Frame duplicates CANCEL INVOICE; the reminder dialog is built from the menu item. |
| `SETTINGS-1.png` | `/settings?tab=notifications` | built | Figma's notification list becomes per-type preferences. |
| `SETTINGS-2.png` | `/settings?tab=security` Sign-in & security | replaced-by (new (AA-996)) | Change password → sign-in facts (company e-mail code, session) + "Manage staff & invites in CIS ↗". Booking holds no password. |
| `SETTINGS.png` | `/settings?tab=general` | built | Profile is read-only (CIS owns name, e-mail). |
| `SUPPLIER  DETAILS.png` | `/suppliers/[id]` | built | Tabs not drawn; the Bills tab (AA-682 S4, bk-030) is built without a frame. No Figma export draws a supplier bill or payout: `/billing?tab=supplier-bills` follows the PAYMENT & INVOICE layout (KPI row, filters, table, drawer). |
| `SUPPLIER MANAGEMENT-1.png` | `/suppliers` | built |  |
| `SUPPLIER MANAGEMENT-2.png` | `/suppliers` | built |  |
| `SUPPLIER MANAGEMENT.png` | `/suppliers` | built |  |
| `TEMPLATE MANAGEMENT-1.png` | `/catalog/trip-plans` | built | -1 / duplicate exports included. |
| `TEMPLATE MANAGEMENT.png` | `/catalog/trip-plans` | built | -1 / duplicate exports included. |
| `TRIP CASES  DETAILS-1.png` | `/cases/[code]?tab=proposals` | built |  |
| `TRIP CASES  DETAILS-2.png` | `/cases/[code]?tab=billing` | built |  |
| `TRIP CASES  DETAILS.png` | `/cases/[code]` Overview | built |  |
| `TRIP CASES MANAGEMENT.png` | `/cases` | built | TRIP PLAN MANAGEMENT is the Trip Cases list (spec 05). |
| `TRIP CHECKS.png` | `/trip-checks` | built |  |
| `TRIP PLAN MANAGEMENT.png` | `/cases` | built | TRIP PLAN MANAGEMENT is the Trip Cases list (spec 05). |
| `TRIP PLAN TEMPLATES-1.png` | `/catalog/trip-plans` | built | -1 / duplicate exports included. |
| `TRIP PLAN TEMPLATES.png` | `/catalog/trip-plans` | built | -1 / duplicate exports included. |
| `TRIP TEMPLATE DETAILS-1.png` | Use trip plan for a trip case dialog | built |  |
| `TRIP TEMPLATE DETAILS-2.png` | `/catalog/trip-plans/[sourceId]` | built |  |
| `TRIP TEMPLATE DETAILS.png` | `/catalog/trip-plans/[sourceId]` | built |  |
| `TRIP TEMPLATES-1.png` | `/catalog/trip-plans` | built | -1 / duplicate exports included. |
| `TRIP TEMPLATES.png` | `/catalog/trip-plans` | built | -1 / duplicate exports included. |
| `UNPUBLISH ACTIVITY.png` | Deactivate service dialog | replaced-by | Unpublish → Deactivate. |
| `UNPUBLISH TEMPLATE.png` | Make not sellable dialog | replaced-by (new (AA-996)) | Unpublish → not sellable in Booking; CIS publishing is untouched. |
| `UNPUBLISHED COUNTRY.png` | Deactivate country dialog | replaced-by (new (AA-996)) | Booking status → inactive (soft); reactivation is the same dialog. |
| `USER  DETAILS-1.png` | `/customers/[id]` (Overview / Trip cases / Inquiries) | built |  |
| `USER  DETAILS-2.png` | `/customers/[id]` (Overview / Trip cases / Inquiries) | built |  |
| `USER  DETAILS-3.png` | `/cases/[code]` inquiry panel | built |  |
| `USER  DETAILS-4.png` | `/cases/[code]` inquiry panel | built |  |
| `USER  DETAILS-5.png` | `/cases/[code]` inquiry panel | built |  |
| `USER  DETAILS-6.png` | Convert inquiry dialog (case workspace) | built |  |
| `USER  DETAILS-7.png` | Decline inquiry dialog (case workspace) | built |  |
| `USER  DETAILS.png` | `/customers/[id]` (Overview / Trip cases / Inquiries) | built |  |
| `USER DETAILS.png` | `/staff/[cisUserId]` | built |  |
| `USER MANAGEMENT-1.png` | `/customers` (tabs All / Leads / Active / Past, row menu) | built |  |
| `USER MANAGEMENT-2.png` | `/customers` (tabs All / Leads / Active / Past, row menu) | built |  |
| `USER MANAGEMENT-3.png` | `/customers` (tabs All / Leads / Active / Past, row menu) | built |  |
| `USER MANAGEMENT-4.png` | `/customers` (tabs All / Leads / Active / Past, row menu) | built |  |
| `USER MANAGEMENT.png` | `/customers` (tabs All / Leads / Active / Past, row menu) | built |  |
| `USER.png` | `/staff` + "Invite staff in CIS ↗" action | replaced-by (new (AA-996)) | The Invite modal becomes a link to CIS → Platform → Staff (accounts and invites live in CIS, O1). |
| `View Details-1.png` | `/billing/invoices/[number]` | built |  |
| `View Details-2.png` | `/billing/invoices/[number]` | built |  |
| `View Details-3.png` | Trip check drawer | built |  |
| `View Details.png` | `/billing/invoices/[number]` | built |  |

Totals (120 files): 78 built before AA-996, 19 new (AA-996), 19 replaced-by (8 of them built by AA-996:
Sign-in & security for SETTINGS-2 and RESET PASSWORD, the CIS invite link for USER, Make not sellable for DELETE TRIP and
UNPUBLISH TEMPLATE, Deactivate country for DELETE COUNTRY and UNPUBLISHED COUNTRY, Deactivate destination for DELETE
DESTINATION), 4 duplicate / misfiled.
