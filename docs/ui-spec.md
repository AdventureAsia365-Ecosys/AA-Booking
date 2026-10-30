# AA-Booking staff admin: consolidated UI spec (S236)

Status: wave 1 is built (AA-Booking PR #5, S236); `/audit-log` shipped ahead of wave 2 (S237). This file is the output
of AA-683. Decided by Nghiệp in S239 (16/10/2026, AA-683 comment): no separate review step with Ms. Thư (waves 1–2 are
built from this spec); Q1a–f resolved as "CIS kit wins"; the customer-facing spec (Q25: sign-in, My Trips, approve
proposal, pay) moves to AA-680. Author: Claude Code (S236 figma-spec lane, consolidation).

> **Repo copy (S237).** Copied from AA-Ecosys `docs/audits/2026-10-14-S236-design-review/booking-ui-spec/ui-spec.md`,
> where the nine module specs (`01-shell.md` … `09-payments.md`) and the design-system note stay. This copy is the
> one to update from now on. S237 changes: migration keys (bk-005 = wave 1 admin and bk-006 = cost lock are taken,
> so wave 2 starts at **bk-007**; the old "W2 = bk-006 / W3 = bk-007" numbering is replaced below), paths to the
> archived module specs, and the nav rule (a per-entry `shipped` flag in `frontend/ui/nav.tsx` replaced the single
> `SHIPPED_WAVE` gate). Everything else is the S236 text; "as built" notes mark where wave 1 differs.

## Sources

- **Figma**: 120 PNG exports in `docs/AAA/figma_AAA/`. The 9 module specs (now archived next to the source of this file, AA-Ecosys `docs/audits/2026-10-14-S236-design-review/booking-ui-spec/01-shell.md` … `09-payments.md`) read all of them. For this consolidation I re-read `OVERVIEW.png` and `Assigned Trips-1.png` myself, to settle the visual-language and case-detail-layout questions the specs left open. No export failed to load.
- **ADRs**: root ADR 0004 (O1 staff log in through CIS; O7 content is authored in CIS, Booking keeps commercial fields; O8 TripPlanner is the customer front door) and ADR 0005 (pricing, suppliers, the `contracting` role, maker-checker).
- **Data model**: ERD v2 `docs/AAA/erd-v2/booking.dbml` and `apps/AA-Booking/CONTEXT.md` (main = bk-001 + bk-002).
- **Branches under construction** (S236; all merged since, AA-Booking PRs #2–#5): AA-Booking `feat/aa-681-staff-auth-trip-case-versions` (bk-003) and `feat/aa-810-pricing-engine` (bk-004). CIS `cis-679` (migration 225, schema `catalog`) and `cis-jwt` (`POST /auth/booking-token`, JWKS).
- **UI kit**: CIS kit `apps/AA-CIS-App/frontend/app/_kit` (tokens.ts, primitives.tsx, DataTable.tsx, Modal.tsx, Drawer.tsx, Toast.tsx, AdminShell/Sidebar/Topbar/BottomNav/CommandPalette, adminNav.tsx, api.ts, csv.ts), `app/_brand/tokens.ts` and `app/globals.css`.

## Conventions

- **Roles**: `admin`, `sales`, `contracting`, `content_creator`. These are the values of `booking.staff_users.role` in bk-003. Figma's "Super Admin" maps to `admin`.
- **Data classes**:
  - **C** (COMMERCIAL): owned by Booking, editable.
  - **CT** (CONTENT): owned by CIS, read-only in Booking, read from `catalog.*`.
  - **ID** (IDENTITY): staff (CIS login plus `booking.staff_users`) or customer (`booking.customers`).
- **Waves**: **W1**, **W2** and **W3** are the build waves in section 8.

---

## 1. Visual language and token table

### 1.1 Approach (Nghiệp's choice: split the CIS kit into shared tokens, copied into each app's `ui/` folder)

```
AA-Booking/frontend/
  ui/
    tokens.css      ← copy of the `--aa-*` blocks of CIS app/globals.css (light, dark, data-theme="dark")
    brand.ts        ← copy of CIS app/_brand/tokens.ts (BRAND hex, fonts, BTN_RADIUS, LOGO_SRC)
    tokens.ts       ← copy of CIS _kit/tokens.ts (K = var(--aa-…), alpha(), formatDate/DateTime, RADIUS, SPACE)
    tailwind.css    ← Tailwind v4 `@theme inline` that maps the same vars to utilities (bg-card, text-ink, …)
    primitives.tsx, DataTable.tsx, Modal.tsx, Drawer.tsx, Toast.tsx, AdminShell.tsx, Sidebar.tsx,
    Topbar.tsx, BottomNav.tsx, CommandPalette.tsx, csv.ts, savedViews.ts, theme.ts, useTheme.ts,
    useMediaQuery.ts, sidebarStore.ts, useSidebar.ts       ← copied unchanged except imports
    nav.tsx         ← Booking nav model (replaces CIS adminNav.tsx)
    api.ts          ← Booking BFF fetch helpers (replaces CIS api.ts: Bearer from BFF, not x-reviewer-id)
    booking/        ← Booking-only components (section 5)
```

- **Tokens are copied byte for byte.** A header comment names the CIS commit they came from. Booking never edits a copied token value: a brand change happens in CIS first and is copied again. A CI check can diff `ui/tokens.css` against the CIS source (open question Q3).
- **Styling.** The CIS kit uses inline styles that read `K` and does not use Tailwind classes (CIS AA-605). The brief asks for Tailwind. Recommendation (Q2):
  - copied kit components stay inline-style, so they are byte-comparable with CIS;
  - Booking-only components and page layout use Tailwind utilities generated from the same CSS variables, never raw hex.
  - One source of colour (the `--aa-*` vars), with two ways of reading it.
- **Theme.** Light and dark, the same as CIS: `<html data-theme>`, restored before first paint by an inline script. The localStorage key is renamed to `bk_theme` so the two apps do not share it.

### 1.2 Token table (CIS kit ↔ Figma)

Rule: **CIS kit value wins** (ADR 0004 decision 1: copy the CIS kit and brand tokens). The Figma value is recorded so the drift is visible. The rows marked ⚠ were the differences large enough for Nghiệp to choose; they were resolved as "kit wins" (Q1, decided S239). The ⚠ markers stay only as a record of the Figma drift, not as open choices.

| Role | CIS kit token (`K.*` / CSS var) | CIS light value | Figma value (sampled) | Decision |
|---|---|---|---|---|
| Primary / brand gold | `K.accent` / `--aa-accent` | `#DB9628` | `#DB9628` | identical |
| Primary hover / text on tint | `K.accentDeep` / `--aa-accent-deep` | `#9E6E16` | ≈`#B87A1A` (not drawn) | kit |
| Gold soft (halo, unread tint) | `K.accentSoft` / `--aa-accent-soft` | `#F4E2C2` | `#F8ECD6` | kit |
| Gold tint (selected row bg) | `K.accentTint` / `--aa-accent-tint` | `#FBF3E3` | cream selected row ≈`#FBF3E3` | kit |
| Gold border (outline btn, focus) | `K.accentBorder` / `--aa-accent-border` | `#DDA64E` | gold focus ring on inputs | kit |
| Sidebar bg | `--aa-side-bg` | `#1F2933` | gradient `#18212A`→`#111820` | kit flat colour; gradient optional (Q1c) |
| Sidebar logo band | none | none | gradient `#18212A`→`#DB9628` | new Booking-local var `--bk-side-brand` = `linear-gradient(90deg, var(--aa-side-bg), var(--aa-accent))` |
| Sidebar active pill | `--aa-side-panel` | `#2A333E` | `#303941`, radius 10, gold text and icon | kit colour, gold text = `K.accent` |
| Sidebar text / label | `--aa-side-text` / `--aa-side-text2` | `#C9CFD8` / `#8A929D` | ≈`#E5E7EB` / ≈`#9CA3AF` | kit |
| Page bg | `K.bg` / `--aa-bg` | `#F8F6F2` (warm) | `#F4F6F8` (cool grey) | ⚠ kit warm (Q1a) |
| Page-header band | `K.bg` | `#F8F6F2` | `#F4F6F8` + 1px bottom line | kit `bg` + `K.line` border |
| Card / topbar / modal | `K.card` / `--aa-card` | `#FFFFFF` | `#FFFFFF` | identical |
| Border | `K.line` / `--aa-line` | `#E9E4DB` | `#E2E4E9` | kit |
| Row divider | `K.line2` / `--aa-line2` | `#F0EBE0` | `#E2E4E9` | kit |
| Table header bg | `K.bg` | `#F8F6F2` | `#F4F6F8` | kit |
| Text primary | `K.ink` / `--aa-ink` | `#1F2933` | ≈`#111827` | kit |
| Body text | `K.body` / `--aa-body` | `#33363D` | ≈`#111827` | kit |
| Text secondary | `K.muted` / `--aa-muted` | `#6B7380` | ≈`#6B7280` | kit (≈ same) |
| Text muted / placeholder | `K.muted2` / `--aa-muted2` | `#9099A6` | ≈`#9CA3AF` | kit (≈ same) |
| Danger | `K.danger` / `--aa-red` | `#C20101` | `#D9534F` / `#DB5E5A` | kit |
| Danger soft (badge bg) | `K.dangerSoft` / `--aa-red-soft` | `#FBE9E7` | `#FEE2E2` | kit |
| Success | `K.success` / `--aa-green` | `#2E7D5B` | `#1F9D55` (toggle), `#166534` (badge text) | kit |
| Success soft | `K.successSoft` | `#E4F1E9` | `#DCFCE7` | kit |
| Warning (In progress, Pending) | `K.amber` / `--aa-kit-amber` | `#B5791F` | `#A16207` on `#FEF9C3` | kit |
| Warning soft | `K.amberSoft` | `#FBEFD6` | `#FEF9C3` | kit |
| Orange ("Sent") | `--aa-amber-orange` / `--aa-amber-bg3` | `#C2410C` / `#FFEDD5` | `#EA580C` / `#FFEDD5` | add kit tone `orange` (Booking-local) reading these vars |
| Info (Approved) | `K.info` / `K.infoSoft` | `#1E40AF` / `#DBEAFE` | `#075985` / `#E0F2FE` | kit |
| Neutral pill (Draft, role chip) | `K.neutralBg` / `K.neutralFg` | `#F3F4F6` / `#4B5563` | `#F1F5F9` / `#64748B` | kit |
| Overlay scrim | kit Modal | kit | navy ≈55% | kit |
| Toast | `--aa-toast-bg` | `#1C1917` dark toast | white toast, green left bar, ✕ | ⚠ kit dark toast (Q1d) |
| Display font | `FONT_DISPLAY` | Fahkwang | none (geometric sans everywhere) | ⚠ kit: Fahkwang only for H1 and KPI numbers (Q1b) |
| UI font | `FONT_SANS` | Poppins | Inter-like grotesk | ⚠ kit Poppins (Q1b) |
| Mono (codes TC-…, INV-…) | `FONT_MONO` | JetBrains Mono | sans | kit mono for codes in tables |
| Button radius | `BTN_RADIUS` / `RADIUS.pill` | 999 (pill, uppercase primary) | 8 px, sentence case | ⚠ kit pill (Q1e) |
| Card radius | `RADIUS.lg` | 12 | ≈16 with a 6 px "double border" frame | kit 12, no double frame |
| Input radius | `RADIUS.md` | 8 | 8 | identical |
| Spacing scale | `SPACE` | 4/8/12/16/22 | 8-pt grid (8/16/24/32) | Tailwind 8-pt for page layout; kit `SPACE` inside components |
| Dates | `formatDate` / `formatDateTime` | "9 Oct 2026", "9 Oct 2026, 14:05" (en-GB, 24 h) | "Oct 12, 2024", "14:30 PM" | kit (this also fixes the Figma "13:00 PM" bug) |
| Dark mode | `[data-theme="dark"]` block | full palette | not drawn | kit |

Figma sizes are taken at the 1920 px frame and **scaled to kit density**: the kit is ≈ 0.75× Figma.

| Element | Figma | Kit equivalent |
|---|---|---|
| Body text | 17–18 | 13–14 |
| Topbar | 96 px | kit Topbar |
| Sidebar width | 346 px | kit Sidebar (collapsible) |
| Table row | ≈100 px | kit DataTable density |

The CIS admin already runs at this density on the same laptops. Q1f asks whether to keep the Figma size instead.

**Compact density (AA-996, S254)** for zoomed laptops: from 768 px to 1199 px CSS wide (a 1440 px laptop at 125–150 %
browser zoom; 150 % ≈ 960 px) the staff admin tightens itself; at 1200 px and wider nothing changes.

- Table cells use 8–9 × 10 px paddings and the page gutter is 20 px.
- The sidebar is the 68 px icon rail (labels in the tooltips; the collapse toggle is hidden there).
- In list tables, columns marked `meta.compact = "drawer"` (FX / money detail, audit facts such as Updated or
  Recorded by, secondary counts) leave the table. They open in the row's **details drawer** (a details button before
  the row menu).
- Built once, in `ui/booking/DataTable.tsx` (the `DataTable` every page imports) and `app/globals.css`, so a new list
  only marks its columns. The layout check (`frontend/e2e/layout`) holds the widest lists to "no column cut off, 20 px
  to spare" at 960 and 1100 px.

### 1.3 Status tones (Booking `StatusBadge` maps)

The CIS `StatusBadge` map is job-runner specific. Booking adds its own maps, all on the kit `Tone` set plus the Booking-local `orange`.

| Entity | Value → label → tone |
|---|---|
| Trip case (`trip_cases.status`, 13 DB values) | new_inquiry → New inquiry → accent · discovery → Discovery → neutral · building → Building → warning · proposal_sent → Proposal sent → orange · confirmed → Confirmed → info · supplier_confirming → Supplier confirming → info · invoice_sent → Invoice sent → info · payment_pending → Payment pending → warning · paid → Paid → success · pre_departure → Pre-departure → success · on_trip → On trip → purple · completed → Completed → success · cancelled → Cancelled → danger |
| Version (derived) | draft (not sent) → Draft → neutral · sent (sent, not approved) → Sent · awaiting reply → orange · approved → Approved (locked) → success · superseded (sent, a later version exists) → Superseded → neutral |
| Customer (derived) | lead (`verified=false` or only `new_inquiry` cases) → Lead → accent · active (open case) → Active → success · past (only completed) → Past client → neutral · blocked → Blocked → danger · merge review → "Needs merge review" → warning (extra chip) |
| Staff | active → Active → success · inactive (`active=false`) → Inactive → neutral |
| Record (supplier, hotel, profile) | active → Active → success · inactive → Inactive → neutral |
| Trip plan (W2) | published → Sellable → success · draft → Not for sale → neutral |
| Invoice (shipped, AA-682 S3) | draft → Draft → neutral · sent → Sent → orange · partially_paid → Partially paid → warning · paid → Paid → success · cancelled → Cancelled → danger · overdue (derived from the schedule, sent / partially paid only) → Overdue → danger. Paid / partially paid are derived from **verified** payments (bk-028), never set by hand |
| Payment (shipped, AA-682 S3) | recorded → To verify → warning · verified → Verified → success · voided → Voided → neutral (critic note 13: a payment is recorded by one person and verified by another; only verified money counts; pending/failed only once a gateway exists, Q22) |
| Schedule row (shipped, AA-682 S3) | scheduled → Scheduled → neutral · due (within `payment_due_soon_days`) → Due soon → warning · overdue → Overdue → danger · paid → Paid → success · cancelled → Cancelled → neutral |
| Audit action | CREATE → success · UPDATE → info · STATUS → warning · DELETE/BLOCK → danger. Upper-case labels, the only upper-case badges. |

---

## 2. App shell and navigation

### 2.1 Shell

- **Shell**: the kit `AdminShell` (Sidebar + Topbar + CommandPalette ⌘K + BottomNav on phones ≤ 767 px).
- **Sidebar**:
  - Booking logo band (`--bk-side-brand`) and the collapse chevron;
  - footer: staff card with name, Booking role and logout.
- **Topbar**:
  - left: breadcrumb (`pageTitle()` from `ui/nav.tsx`);
  - right: a pill cluster with the bell (unread dot) → notification popover, a dashed divider, the role label and the avatar → menu (Settings, Theme, Open CIS admin ↗, Log out).
- **Page frame**: kit `PageHeader` with title, description and actions. On detail pages the header carries a back arrow, the entity code (mono), a status badge and the action buttons.
- **Phone**: BottomNav shows Dashboard, Cases, Customers and Notifications, plus "More" (the full nav in a drawer).

### 2.2 Navigation (ordered; regrouped from Figma per shell C-1/C-10)

Figma groups (OVERVIEW / CONTENT & INVENTORY / TRIP OPERATIONS & SALES) put Trip Cases under content and Settings under sales. The Booking order is below. Items are shown only to roles that may open the page (same pattern as CIS `canSeePage`), and items of a later wave stay hidden until that wave ships.

| # | Group | Label | Icon (lucide) | Route | Roles | Wave |
|---|---|---|---|---|---|---|
| 1 | Overview | Dashboard | `LayoutGrid` | `/` | all | W1 |
| 2 | Sales | Trip Cases | `Briefcase` | `/cases` | admin, sales, contracting | W1 |
| 3 | Sales | Proposals | `Map` | `/proposals` | admin, sales, contracting | W1 |
| 4 | Sales | Customers | `Users` | `/customers` | admin, sales | W1 |
| 5 | Sales | Pricing calculator | `Calculator` | `/pricing/calculator` | admin, sales, contracting | W1 |
| 6 | Operations | Payments & Invoices | `CreditCard` | `/billing` | admin, sales (contracting: Supplier bills tab only, AA-682 S4) | shipped (AA-682 S3, S4) |
| 7 | Operations | Trip Checks | `SquareCheck` | `/trip-checks` | admin, sales, contracting | shipped (AA-682 S2) |
| 8 | Supply | Suppliers | `Store` | `/suppliers` | admin, contracting (sales read) | W2 |
| 9 | Supply | Hotels | `Building` | `/hotels` | admin, contracting (sales read) | W2 |
| 10 | Supply | Services | `Activity` | `/services` | admin, contracting (sales read) | W2 |
| 11 | Supply | Pricing rules & FX | `Percent` | `/pricing/rules` | admin, contracting | W2 |
| 12 | Catalog (from CIS) | Trip Plans | `CalendarDays` | `/catalog/trip-plans` | all | W2 |
| 13 | Catalog (from CIS) | Destinations | `MapPin` | `/catalog/destinations` | all | W2 |
| 14 | Catalog (from CIS) | Countries | `Globe` | `/catalog/countries` | all | W2 |
| 15 | Admin | Staff | `Shield` | `/staff` | admin | W1 |
| 16 | Admin | Audit Log | `ClipboardList` | `/audit-log` | admin | W2 |
| — | footer / avatar menu | Settings | `Settings` | `/settings` | all | W1 |
| — | topbar bell | Notifications | `Bell` | `/notifications` | all | W1 |

Renames compared with Figma:

| Figma | Booking | Why |
|---|---|---|
| "Itinerary Builder" | "Proposals" | it lists trip case versions |
| "Admins" | "Staff" | the page manages all Booking staff, not only admins |
| "End-users" | "Customers" | plain name for the customer directory |
| "Activities" | "Services" | see §7 |
| "Destination" | "Destinations" | plural, like the other list pages |

`content_creator` sees only the Dashboard, the Catalog group, Notifications and Settings. Its catalog view is read-only, and it edits content in CIS.

---

## 3. Route map

App Router under `frontend/app/(staff)/…`, plus `app/login` and the BFF under `app/api/…`. "Figma" names the PNGs each route covers (module spec in brackets). Roles: **A**=admin, **S**=sales, **C**=contracting, **CC**=content_creator; "r" = read-only.

| Route | Screen | Figma (module) | Roles | Wave |
|---|---|---|---|---|
| `/login` | Staff sign-in (CIS credentials, §4.1) | `Group 1410084962.png` background only (01) | public | W1 |
| `/` | Dashboard: KPIs + recent cases + my queue | OVERVIEW, OVERVIEW-1 (01) | A S C CC (widgets by role) | W1 |
| `/notifications` | All notifications | NOTIFICATION, SETTINGS-1 (01) | all | W1 |
| `/settings` | Profile (read-only, from CIS), theme, notification preferences | SETTINGS, SETTINGS-1 (as preferences), EDIT PROFILE (part) (01) | all | W1 |
| `/settings?tab=security` | Sign-in & security: sign-in method (company e-mail code), session facts (`GET /api/auth/session`: signed in, session end), last seen, "Manage staff & invites in CIS ↗" | SETTINGS-2, RESET PASSWORD (replaced) (01) | all | shipped (AA-996) |
| `/cases` | Trip case list (+ saved filter "Inquiries" = `status=new_inquiry`) | TRIP CASES MANAGEMENT, TRIP PLAN MANAGEMENT (03, 05) | A S C(r) | W1 |
| `/cases/new` | Create trip case (manual; optional "Start from a trip plan", AA-996) | ADD NEW TRIP 8 (03) | A S | W1 |
| `/cases/[code]` | Case workspace, tab Overview (+ Edit details drawer) | Assigned Trips-1 (layout), TRIP CASES DETAILS, USER DETAILS-3/-4/-5 (inquiry panel), ADD NEW ACTIVITY-2 (= "Edit Trip Case" form → Edit details drawer) (03, 02, 07) | A S C(r) | W1 |
| `/cases/[code]?tab=proposals` | Versions list | TRIP CASES DETAILS-1, Assigned Trips-1 (03) | A S C(r) | W1 |
| `/cases/[code]?tab=suppliers` | Supplier bookings of the case (AA-682 S1, bk-026): Generate from the approved version, Request / Confirm (ref + voucher, B2C only) / Declined / Cancel / Change; computed supply badge "Confirmed & Booked"; B2B on-request lines answered here (one action with the desk). Below: **Cost vs billed** panel (AA-682 S4, bk-030): expected net per booking next to the supplier bill lines, variance once billed and per currency (`bill.read` + `pricing.read`); links to the case's bills (`?tab=supplier-bills&case=`) and Enter bill (`bill.write`) | — (not drawn) | A S C | shipped (AA-682 S1, S4) |
| `/cases/[code]?tab=billing` | Invoices & payments of the case (AA-682 S3, bk-028): the live invoice (money, lines, schedule, payments, actions) or why the trip cannot be invoiced yet + **Create invoice** stepper; cancelled invoices below; header money badge | TRIP CASES DETAILS-2 (03) | A S | shipped (AA-682 S3) |
| `/cases/[code]?tab=checks` | Trip checks of the case (AA-682 S2, bk-027): derived checks (every supplier booking confirmed; payment verified once invoices exist, S3), the three standard checks seeded on confirm (re-check bookings, emergency contact (B2C only), insurance) + custom checks; check drawer (status, due, owner, note, emergency contact) | View Details-3 (03) | A S C | shipped (AA-682 S2) |
| `/cases/[code]?tab=activity` | Status history + audit of the case | not drawn | A S C(r) | W1 |
| `/cases/[code]/proposals/[n]` | Version builder | ITINERARY BUILDER-3, ITINERARY BUILDER-4 (04) | A S (C: cost lines) | W1 |
| `/cases/[code]/proposals/[n]/preview` | Customer-style preview + send | ITINERARY PREVIEW, ITINERARY BUILDER-2 (send modal) (04) | A S C(r) | W1 |
| `/proposals` | Cross-case versions list | ITINERARY BUILDER, ITINERARY BUILDER-1, DELETE ITINERARY BUILDER (04) | A S C(r) | W1 |
| `/customers` | Customer directory (tabs All / Leads / Active / Past) | USER MANAGEMENT, -1…-4, DELETE USER (02) | A S | W1 |
| `/customers/[id]` | Customer detail (tabs Overview / Trip cases / Inquiries) | USER  DETAILS, -1, -2 (02) | A S | W1 |
| `/pricing/calculator` | Stand-alone quote preview | not drawn (ADR 0005) | A S C | W1 |
| `/staff` | Staff list (Booking profiles of CIS users); "Manage staff & invites in CIS ↗" + per-row sign-in / invite link to CIS (AA-996) | ADMINS, USER, BLOCK ADMIN, RESET PASSWORD (bg) (02, 01) | A | W1 |
| `/staff/[cisUserId]` | Staff detail (tabs Overview / Assigned cases / Activity) | USER DETAILS, Activity Log, Assigned Trips, EDIT PROFILE (02, 01, 03) | A (self: read) | W1 |
| `/audit-log` | Audit log | AUDIT LOGS (01) | A | W2 (shipped early, S237) |
| `/suppliers` | Supplier list | SUPPLIER MANAGEMENT, -1, -2, DELETE USER-1 (08, 02) | A C, S(r) | W2 |
| `/suppliers/new`, `/suppliers/[id]/edit` | Supplier form | ADD SUPPLIER, EDIT SUPPLIER (08) | A C | W2 |
| `/suppliers/[id]` | Supplier detail (tabs Profile / Contracts / Rates / Bookings / Bills) | SUPPLIER  DETAILS (08); tabs not drawn | A C, S(r) | W2 (Bookings: AA-682 S1, read-only list linking to the case; Bills: AA-682 S4, this supplier's bills + Enter bill + the bill drawer, `?bill=BIL-…`) |
| `/suppliers/rate-sheets/[id]` | Rate-sheet upload review + publish (maker-checker) | not drawn (ADR 0005 §7–8) | A C | W2 |
| `/hotels` | Hotel grid (default) / table (`?view=table`) | HOTEL MANAGEMENT, COUNTRY MANAGAMENT-2/-3, DELETE HOTEL (07, 06) | A C, S(r) | W2 |
| `/hotels?view=[id]` / `?edit=[id]` / `?new=1` | Hotel drawer (view / edit / add) | EDIT HOTEL-1, EDIT HOTEL, ADD NEW HOTEL, HOTEL DETAILS (07) | A C, S(r) | W2 |
| `/services`, `/services/[id]` | Commercial services (activity, transfer, guide, entrance, meal) over CIS places | ACTIVITY MANAGEMENT, -1, -2, ACTIVITY  DETAILS, -1, -2, ADD NEW ACTIVITY, -1, EDIT ACTIVITY, DELETE ACTIVITY, UNPUBLISH ACTIVITY (07) | A C, S(r) | W2 |
| `/pricing/rules` | Pricing rules + FX rates (append-only, maker-checker) | not drawn (ADR 0005) | A C | W2 |
| `/catalog/trip-plans` | Sellable trip plans over `catalog.v_tour` | TRIP TEMPLATES, -1, TRIP PLAN TEMPLATES, -1, TEMPLATE MANAGEMENT, -1, UNPUBLISH TEMPLATE (05) | all (A C edit commercial) | W2 |
| `/catalog/trip-plans/[sourceId]` | Trip plan detail (CIS content + commercial panel) + "Use for trip case" modal | TRIP TEMPLATE DETAILS, -1 (modal), -2 (05) | all | W2 |
| `/catalog/trip-plans/[sourceId]/commercial` | Commercial edit (sellable, supplier, price matrix, trip style) | CREATE NEW TEMPLATE, -1, -2, EDIT TEMPLATE, -1, -2 (only the commercial parts) (05) | A C | W2 |
| `/catalog/destinations` (`?place=<id>` drawer, `&edit=1`) | Destinations: every CIS place (`catalog.v_destination`, server paging and filters) + Booking status / reason / notes (bk-029 `destination_profiles`); drawer view + edit; "Add destination" picks an existing CIS place | DESTINATION MANAGEMENT, -1, DESTINATION DETAILS, ADD/EDIT DESTINATION (CIS fields read-only), DELETE DESTINATION (→ deactivate) (06) | all (A edit status) | shipped (AA-996) |
| `/catalog/countries` (`?edit=<iso>` drawer), `/catalog/countries/[iso]` | Countries derived from CIS places and tours (card grid) + Booking status / area / notes (bk-029 `country_profiles`); country page with Overview / Destinations / Tours | COUNTRY MANAGAMENT, -1, COUNTRY DETAILS, -1, ADD/EDIT COUNTRY (CIS fields read-only), DELETE / UNPUBLISHED COUNTRY (→ deactivate) (06) | all (A edit status) | shipped (AA-996) |
| `/billing` | Invoices / Payments tabs + 4 KPIs (left to pay, overdue, received this month = verified net of refunds, waiting for verification) | PAYMENT & INVOICE, -1, -2, -3, PAYMENT MANAGEMENt, DELETE (09, 02) | A S | shipped (AA-682 S3) |
| `/billing?tab=supplier-bills` (`&bill=BIL-…` drawer) | Supplier bills (AA-682 S4, bk-030): 4 KPIs (to approve, to pay, overdue, paid out this month), list with status / overdue / search filters, **Enter bill** form (supplier, optional trip case, the supplier's bill number, amount, dates, lines = confirmed bookings of that supplier, document), bill drawer (lines with expected net, document, payouts, Approve by another person, Record payout, Void payout, Cancel with a reason; never "Delete") | not drawn (Figma 09 has no supplier-bill frame) | A C (enter, approve), S(r); payouts A | shipped (AA-682 S4) |
| `/billing/invoices/[number]` | Invoice detail: send / edit / delete a draft, record payment, verify, void, amend, remind, cancel | View Details, -1, -2 (03), CANCEL INVOICE (09) | A S | shipped (AA-682 S3) |
| `/billing/payments/[ref]` | Payment detail: evidence files, edit reference / note before verify, verify (admin, never the recorder), void | PAYMENT DETAILS, EDIT PAYMENT (09) | A S (sales record, edit and void unverified payments; only an admin verifies or voids a verified one, critic note 13) | shipped (AA-682 S3) |
| `/trip-checks` | Trip checks across the open sold cases (AA-682 S2): filters status / due / owner / check / search, soonest due first, check drawer; the latest daily lifecycle runs under the table | TRIP CHECKS, View Details-3 (03) | A S C | shipped (AA-682 S2) |

Modals and drawers that are not routes. Every confirm of a soft delete uses the Figma dialog layout (round icon, title,
message with the record name in bold, Cancel + action; `ui/booking/ConfirmDialog.tsx`, AA-996). The content forms
(trip plan commercial drawer, service form, country and destination drawers) show the CIS-owned fields read-only in a
"From CIS" block (`app/(staff)/_parts/CisFields.tsx`) with "Edit in CIS ↗" for admin and content creators (AA-996).

| Modal / drawer | Figma | Where it opens | Wave |
|---|---|---|---|
| Notification popover | OVERVIEW-1 | topbar bell | W1 |
| Convert inquiry | USER  DETAILS-6 | case workspace | W1 |
| Decline (cancel) inquiry | USER  DETAILS-7 | case workspace | W1 |
| Assign sales | not drawn | case workspace | W1 |
| Change status | not drawn | case workspace | W1 |
| Send proposal | ITINERARY BUILDER-2 | builder, preview | W1 |
| Delete draft version | DELETE ITINERARY BUILDER | proposals list, builder | W1 |
| Staff role / activation | EDIT PROFILE, BLOCK ADMIN | staff list, staff detail | W1 |
| Create invoice (4-step: approved version → adjustments → schedule → review; also edits a draft and amends a sent invoice) | CREATE INVOICE | case Billing tab | shipped (AA-682 S3) |
| Record payment (receipt or refund, evidence upload, one Idempotency-Key per opening) | RECORD PAYMENT | invoice | shipped (AA-682 S3) |
| Send reminder (one customer My Trips row a day; no e-mail) | SEND REMAINDER (no screen drawn) | invoice | shipped (AA-682 S3) |
| Cancel invoice (reason; only with nothing paid) | CANCEL INVOICE | invoice | shipped (AA-682 S3) |
| Verify payment (a second admin) | none drawn | invoice, payment | shipped (AA-682 S3) |
| Edit case details (drawer) | ADD NEW ACTIVITY-2 ("Edit Trip Case"; customer locked) | case workspace | W1 |
| Archive customer | DELETE USER (as soft delete) | customers list / detail | W1 |
| Use trip plan for a trip case | TRIP TEMPLATE DETAILS-1 | trip plan detail | W2 |
| Make not sellable / Make sellable (soft: `sellable=false` through PUT …/commercial) | UNPUBLISH TEMPLATE, DELETE TRIP | trip plans row menu / detail | shipped (AA-996) |
| Deactivate supplier | DELETE USER-1 | suppliers list / detail | W2 |
| Deactivate / Activate country (reason optional) | DELETE COUNTRY, UNPUBLISHED COUNTRY | countries grid, country page | shipped (AA-996) |
| Deactivate / Activate destination (reason optional) | DELETE DESTINATION | destinations row menu, destination drawer | shipped (AA-996) |
| Country drawer (add / edit Booking state; CIS fields read-only) | ADD COUNTRY, EDIT COUNTRY | countries, country page | shipped (AA-996) |
| Destination drawer (view; edit Booking state with CIS fields read-only) | DESTINATION DETAILS, ADD DESTINATION, EDIT DESTINATION | destinations, country page | shipped (AA-996) |
| Deactivate hotel / service | DELETE HOTEL, EDIT HOTEL (Delete button), DELETE ACTIVITY, UNPUBLISH ACTIVITY, ACTIVITY DETAILS-1/-2 | hotels, services | W2 |
| Approve pricing rule / FX row (second person) | not drawn (Q19) | pricing rules | W2 |
| Trip check details (drawer) | View Details-3 | trip checks, case Checks tab | W3 |
| Edit payment / Void payment | EDIT PAYMENT, DELETE (as void) | payment detail, invoice | shipped (AA-682 S3) |

**Figma coverage (AA-996, S254).** Every Figma export is covered: [`docs/figma-coverage.md`](figma-coverage.md) maps
each of the 120 PNGs to its route or dialog (built, new, replaced-by or duplicate). The screens that used to be "not
built" now have a working Booking equivalent:

- SETTINGS-2 (change password), RESET PASSWORD and the Invite part of USER.png → **Sign-in & security** tab of
  `/settings` (sign-in method, session facts from `GET /api/auth/session`, "Manage staff & invites in CIS ↗") and the
  CIS links on `/staff`. Booking still holds no password (O1).
- The content editors of ADD / EDIT COUNTRY, ADD / EDIT DESTINATION, ADD NEW / EDIT ACTIVITY and CREATE NEW / EDIT
  TEMPLATE → the same Figma forms with the CIS fields read-only ("From CIS", "Edit in CIS ↗") next to the editable
  Booking fields (O7).
- Hard-delete dialogs → soft equivalents in the Figma dialog layout (`ConfirmDialog`): deactivate, archive, cancel,
  void, delete draft, make not sellable.
- DELETE SUPPLIER.png and SEND REMAINDER.png are misnamed exports (Delete User on customers; the Cancel invoice frame).

---

## 4. Per-route spec (condensed)

Rules that apply to every list page. They replace the Figma drift (counts that do not add up, checkboxes with no bulk action, "flows" in the pager).

- **Table**: kit `DataTable` with server pagination (`serverPagination`), page sizes 25 / 50 / 100, and server sort where the API has it.
- **Loading / empty / error**: kit `TableSkeleton` while loading, `EmptyState` with a context message and a Reset button when nothing matches, `ErrorState` + Retry on error.
- **Filters**: a `FilterBar` above the table (search + selects + "Reset filters" shown only when a filter is not at its default). Filter state lives in the URL query.
- **Row selection** is enabled only where a bulk action exists. In W1 that is CSV export of the selection.
- **Writes**: kit `useToast` success/error toasts, with text from the entity ("Trip case TC-000123 updated"). Every write is audited server-side.
- **Codes** (TC-…, INV-…) use the mono font. **Dates** use `formatDate` / `formatDateTime`.
- **Currency** is always shown with its ISO code, never a bare `$`.

### 4.1 `/login` (W1)

The O1 flow: CIS login, then a Booking token.

1. The form takes the CIS username and password and posts them to the BFF `POST /api/auth/login`.
2. The BFF calls CIS `POST /auth/admin-login`, which returns the CIS session token.
3. The BFF then calls CIS `POST /auth/booking-token` (Bearer = CIS token), which returns a 15-minute RS256 JWT (`aud=booking`).
4. Both tokens are stored in httpOnly, Secure, SameSite=Lax cookies on the Booking origin. The BFF refreshes the Booking token from the CIS token when it has less than 2 minutes left.
5. After login the app calls `GET /booking/staff/me`. If the profile is inactive (`active=false`), it shows a "Your Booking access is pending activation by an admin" screen.

Background image: `Group 1410084962.png`. Error copy never says which of the two fields was wrong. Alternatives are in Q4.

### 4.2 `/` Dashboard (W1)

- **KPI tiles** (kit stat tiles, 4-up, 2-up on phone):

  | Tile | Source | Wave |
  |---|---|---|
  | Open trip cases | count of non-terminal statuses, with the month-over-month delta | W1 |
  | New inquiries | `new_inquiry`, red hint "needs attention" when > 0 | W1 |
  | My cases | for sales: open assignments to `me` | W1 |
  | Proposals awaiting reply | versions sent and not approved | W1 |
  | Outstanding balance | — | W3 |
  | Active suppliers | — | W2 |

- **"Recent trip cases" table**: code, customer (name, email, lead chip), trip (first-leg tour title + "+N legs", or "Custom"), status, sales, updated. "View all cases" → `/cases`.
- **By role**:
  - contracting: "Rate sheets awaiting publish" (W2) instead of New inquiries;
  - content_creator: catalog counts and a link to the CIS admin.

### 4.3 `/notifications` + bell popover (W1)

- **Popover**: the latest 5, "Mark all as read", "View all".
- **Page**:
  - search;
  - filters: date range, read/unread, type;
  - list items: icon by type, title, message with entity codes as links, timestamp;
  - one action button derived from type (e.g. "Open case").
- **Types** (fixed vocabulary, shell C-8): `inquiry.new`, `trip_case.assigned`, `trip_case.status_changed`, `proposal.approved` (W1); `rate_sheet.awaiting_publish` (W2); `payment.recorded`, `invoice.overdue` (W3); `job.failed` (admin).

### 4.4 `/settings` (W1)

- **Profile card**: name, email and CIS role, all read-only ("Managed in CIS ↗"), and the Booking role (read-only).
- **Appearance**: theme (light / dark / system).
- **Notification preferences**: per-type toggles. The Figma "Notification" tab becomes this (shell C-9).
- No Security tab and no photo upload (O1).

### 4.5 `/cases` Trip case list (W1)

- **Header**:
  - title "Trip cases";
  - actions: Export CSV (secondary), "+ New trip case" (primary; A and S only).
- **Saved-filter tabs**: All · Inquiries (`new_inquiry`) · Mine (`assignee=me`) · Unassigned (`assignee=none`) · Active (pre-sale) · Closed (`completed`, `cancelled`).
- **FilterBar**:
  - search (code, customer name or email);
  - status (multi);
  - sales rep (staff picker, "Unassigned");
  - source (TripPlanner / Manual);
  - created date range.
- **Columns**:

  | Column | Content |
  |---|---|
  | Code | mono |
  | Customer | avatar initials, name, email; chips "Lead" / "Merge review" |
  | Trip | first-leg CIS tour title + "+N legs", or "Custom" |
  | Dates | start–end, "N days" |
  | Pax | |
  | Status | badge |
  | Sales | assignee name, or "—" |
  | Updated | relative time |

  The row click goes to the case. There is no row checkbox until bulk actions exist.
- **Figma replaced**: the 6-value status vocabulary becomes the 13 DB values (03 C1); "Linked trip template" becomes "Trip" (templates are W2).

### 4.6 `/cases/new` Create trip case (W1)

Header: Cancel, then the primary "Create trip case". Sections:

1. **Customer**: a combobox that searches customers by name, email or phone, plus "+ New lead" inline (name; email or phone required; consent checkbox; created unverified, `source=manual`).
2. **Trip**: start date (optional), pax (≥ 1; adults / children split, Q13), currency (USD default; USD/EUR/GBP/AUD/SGD), trip style (select, Q16), countries (multi, ISO; W2 if `trip_case_countries` is not migrated). "Start from trip plan" is hidden until W2.
3. **Assignment and notes**: sales rep (staff picker; default me if I am sales), internal notes (textarea).

Validation: customer required; pax 1–999; currency ISO-3. Success → `/cases/[code]` with a toast.

### 4.7 `/cases/[code]` Case workspace (W1)

The layout is `Assigned Trips-1.png` (03 decision 11), extended with tabs.

- **Header band**:
  - back arrow, case title (first-leg tour title or "Custom trip"), code (mono), dates, pax, status badge;
  - actions: "Change status ▾" (the menu lists `allowed_transitions` only; `cancelled` asks for a reason), "Assign", "Edit details".
- **Status stepper**: the 12 non-cancelled DB values. They group visually into Inquiry (new_inquiry, discovery), Proposal (building, proposal_sent), Booking (confirmed … paid) and Travel (pre_departure, on_trip, completed). It collapses to the group on narrow widths. `cancelled` shows as a red banner with the reason, never as a step.
- **Tabs**: Overview · Proposals (n) · Suppliers (booked / to book, AA-682 S1) · Billing (AA-682 S3) · Checks (AA-682 S2) · Activity. The header carries the supply badge and the money badge (Invoice draft / To pay n / Overdue n / Paid in full; billing.read only).
- **Supply badge** (AA-682 D1): next to the status, "Suppliers n/m" (warning) while bookings wait, "Confirmed & Booked"
  (success) once every supplier confirmed; nothing when the trip has nothing to book. Never a status: the 13 values stay.
  Post-sale statuses are never offered in "Change status" (API 409 `status_system_managed`); staff only cancel.
- **Overview, main column**:
  - **Inquiry panel** (cases from TripPlanner): source chip "TripPlanner", received at, and the `inquiry_snapshot` rendered read-only: route days and legs, plus message / budget / travel window when the contract carries them (v1.2, Q12).
  - **Primary actions on a `new_inquiry` case**:
    - "Convert to discovery": a modal with sales rep, optional notes, an optional "Start from a trip plan" picker (sellable plans, `version.write`; AA-996: adds a draft version from the plan's CIS days through `POST /booking/catalog/trip-plans/{source_id}/trip-cases` with `{trip_case}`), and status → `discovery`;
    - "Decline": a modal with a reason select (Budget too low / Not our destination / Duplicate / No response / Other) and notes, then status → `cancelled` with the reason;
    - "Reopen" when allowed.
  - **Trip legs**: an ordered list, each with the CIS tour title (from `catalog.v_tour` by `source_tour_id`), days from–to, and "View in catalog". All CIS content here is read-only, tagged "From CIS".
- **Overview, side column**:
  - **People**: the customer (name, email, phone, verified / lead chip, merge-review warning with a link) and the assigned sales (name, role);
  - **Trip details**: destinations (derived from the legs, CIS), budget (snapshot, if any), currency;
  - **Internal notes** (editable inline, A and S).
- **Proposals tab**: a list of versions, each row showing V{n}, notes / title, derived badge, total + currency, sent / approved dates and "Last edited by". Actions:
  - "Open builder" (draft);
  - "Preview";
  - "Duplicate as new version";
  - "Delete" (unsent draft only, confirm).

  Header button: "+ New version" (V1 is pre-filled from the legs; Vn copies the latest).
- **Activity tab**: status history (from → to, by, note, time) merged with the case's audit rows.
- **Edit details** (drawer): pax, start date, currency, notes. These are the only editable fields in bk-003 (`EDITABLE_FIELDS`). Trip style and countries come later (Q13).

### 4.8 `/cases/[code]/proposals/[n]` Version builder (W1)

The Figma builder (day cards with Morning / Afternoon / Evening slots) does not match the **bk-003 item model**. A bk-003 item is a free-text row: `title`, `day_from`, `day_to`, an optional `source_tour_id` and `notes`, ordered by position, up to 200 per version. W1 builds the bk-003 model and keeps the Figma visual language:

- **Header**:
  - back arrow, "Proposal V{n} · TC-… · customer";
  - derived badge;
  - Save (PUT items), Preview, "Send to customer" (primary).
  - An approved version shows everything read-only with an "Approved, locked" banner.
- **Day timeline (main column)**:
  - a vertical list of `ItineraryDayCard`s, one per day in 1…max(day_to), dated from `trip_cases.start_date` when it is set;
  - each item renders as a card spanning its day range: drag handle, title, "Days 3–5", a "From CIS tour" chip with the tour title when `source_tour_id` is set, and notes;
  - per-item actions: edit (`FormDrawer`: title, day from/to, source tour picker over `catalog.v_tour`, notes) and delete;
  - "+ Add item" and "+ Add from CIS tour". The second inserts the tour's days as items, offset to a chosen start day, with titles from `catalog.v_tour_day`.
  - Reorder with drag and keyboard (up / down buttons for accessibility).
- **CIS day text**: when an item has `source_tour_id`, the card can expand to show `v_tour_day.title` and `body` for the covered days, read-only (O7). The staff note is commercial correspondence (04 C1).
- **Price panel (side column)**: see 4.10. It lists cost lines per item (net amount, supplier currency, supplier note), the channel, selling currency and peak toggle, and calls `quote-preview` live (debounced 500 ms). It shows subtotal, total (rounded to 5), effective margin and floor. An override total below the floor shows a red "Below floor (x%): reason required" message.
- **Unsaved-changes guard** on leaving the page.

W2/W3 extensions, once the bk ERD typed items land (04 C8):

| Field | Source |
|---|---|
| period | morning / afternoon / evening |
| hotel | `hotels` |
| service | `services` |
| destination | `catalog.v_destination` |
| times and transfer | structured fields |

The card then gains the Figma period slots. The data contract is in Q9.

### 4.9 `/cases/[code]/proposals/[n]/preview` (W1)

- **Main column**: a customer-style rendering. For each day: "Day N" plus the date, the CIS day title and body (read-only), the hero photo from `catalog.v_tour_photo`, the item titles and the staff notes.
- **Side panels**:
  - **Overview**: case, customer, dates, destinations, status, sales;
  - **Price**: total, currency, valid until;
  - **Included / Not included**: from `catalog.v_tour.inclusions/exclusions` of the legs (CT), plus per-version commercial lines (W2, Q10);
  - **Map**: the places of `catalog.v_tour_place` (lat/lng); W1 may show a static list instead.
- **Actions**: Edit (→ builder), Download PDF (W2), "Send to customer".
- **Send modal** (ITINERARY BUILDER-2, with the primary button labelled **Send**, not "Create Invoice"):
  - To: customer email, read-only (Q14);
  - subject, message (prefilled with the staff signature);
  - "Valid until" (default +14 days, 7–30);
  - a read-only summary of total, currency and the FX date that will be locked.
  - Effect: the version is marked sent, price and FX are locked, the case moves to `proposal_sent`, and a customer notification row is written. Email is out of scope today, so the copy says "Marked as sent; the customer will see it in My Trips" and never claims an email (02 #11).

### 4.10 `/pricing/calculator` (W1)

A stand-alone version of the builder's price panel, with no case. It holds:

- net cost lines (amount + currency);
- the channel (B2C tailor-made / fixed departure / B2B);
- the selling currency;
- a peak toggle;
- an optional override.

The result shows:

- the rule in force (target and floor margin, peak uplift, card fee, valid from);
- the FX rates used (rate per USD, buffer, as of, source);
- each line's net USD and sell amount;
- subtotal, rounding increment, total, effective margin and the below-floor flag.

Errors map from the API: 422 "no FX rate for VND" → an inline message and a link to `/pricing/rules` (W2; admin and contracting only); 503 → "Pricing data unavailable".

### 4.11 `/proposals` Cross-case versions (W1)

- **Search**: code, customer, version title.
- **Filters**: badge (Draft / Sent / Approved / Superseded), sales, updated range.
- **Columns**: proposal (title + code · customer), destinations (from legs), dates and days, version V{n}, badge, total, last edited (time · by).
  *As built (S239, AA-683 width fix)*: version V{n} leads the proposal title, and dates · days sit under the first leg in
  the Trip cell, so the table fits 1440 px without sideways scroll; title and leg are one clamped line each with the full
  text in the tooltip.
- **Row actions**: Open builder, Preview, Duplicate, Send, Delete (draft only).
- **"+ New proposal"**: opens a case picker, then routes to the case's builder (04 C4).

### 4.12 `/customers` and `/customers/[id]` (W1)

- **List**:
  - tabs All · Leads · Active · Past clients;
  - search (name, email, phone);
  - filters: status, merge review, source (`customers.source`: `tripplanner_lead` / `manual` / `otp`); no country filter (no `customers.country_code` column; dropped in the critic pass);
  - columns: customer (initials, name, email), phone, location (`address`), status (derived) + chips (Blocked, Merge review), trip cases (count), last activity;
  - "+ New lead" (drawer).
- **Detail**:
  - header: name, status, code (Q15), added on, source;
  - actions: Edit (name, phone, address; email only if unverified, Q14), "New trip case", Block / Unblock (A);
  - tabs:
    - Overview: contact, consent (with date), merge-review panel (candidate customer with a link; "Mark reviewed" is W2), recent activity;
    - Trip cases: the case table filtered to this customer, with the Figma empty state "No trip cases yet" + "Create first trip case";
    - Inquiries: cases that came from TripPlanner, with the snapshot summary.
- **No hard delete** (02 #7): archive = soft delete, allowed only with no cases past `new_inquiry`; otherwise Block.

### 4.13 `/staff` and `/staff/[cisUserId]` (W1)

- **List** (A only):
  - KPI tiles: total, active, pending activation (`active=false`);
  - search, filters for role and active;
  - columns: name, email (from the CIS JWT claims cached on the profile), Booking role (chip), CIS role (muted), status, last seen.
  - "Add staff" is not in Booking. A banner says: "Staff accounts are created in CIS admin ↗. They appear here after their first sign-in to Booking."
- **Row and detail actions**: Change role (select admin / sales / contracting / content_creator); Activate / Deactivate, with a confirm and an optional reason (audited). Deactivate replaces Figma's "Block / Suspend". There is no reset password (O1).
- **Detail**:
  - header: name, role, email, since, last seen;
  - tabs: Overview (role, CIS role, active, created / updated), Assigned cases (open assignments → the case table), Activity (audit rows by actor).

---

## 5. Shared components

Kit components are copied unchanged. **Booking** components are new, in `ui/booking/`, and use Tailwind utilities on the same vars.

| Component | From | Used by | Notes |
|---|---|---|---|
| `AdminShell`, `Sidebar`, `Topbar`, `BottomNav`, `CommandPalette` | kit | all | nav model from `ui/nav.tsx`; Topbar gains the bell cluster |
| `PageHeader` | kit | all | supports back + code + badge + actions (detail band) |
| `Button` (primary / secondary / danger / ghost; sm / md / lg) | kit | all | pill radius unless Q1e says otherwise |
| `Badge`, `StatusBadge` | kit + Booking maps | all lists | Booking adds `orange` tone and the per-entity maps of §1.3 |
| `DataTable` | kit + Booking wrapper | every list | server pagination / sort; CSV export; saved views per user (`cis_user_id`); compact density 768–1199 px (§1.2): `meta.compact = "drawer"` columns move to a row details drawer |
| `FilterBar` | Booking | lists | search + selects + date-range + "Reset filters"; state in the URL; composes into `DataTable.toolbarExtra` |
| `SavedFilterTabs` | Booking (kit `Tabs`) | cases, customers, billing | tabs with counts that set filter presets |
| `Tabs` | kit | detail pages | `?tab=` in the URL |
| `Modal`, `ConfirmModal` → `ConfirmDialog` | kit | destructive and confirm actions | `ConfirmDialog` adds an optional required reason field and danger styling; the copy names the entity and says what happens (soft delete / cancel, never "permanently") |
| `Drawer` → `FormDrawer` | kit | edits (case details, item, hotel, staff role) | `FormDrawer` = Drawer + form, footer (Cancel / Save), dirty guard, inline field errors from 422 bodies |
| `Toast` / `useToast` | kit | all writes | |
| `EmptyState`, `ErrorState`, `TableSkeleton`, `Spinner`, `LoadingScreen` | kit | all | |
| `KpiTile` | Booking | dashboard, staff, billing | label, value, hint line with a tone, optional delta |
| `StatusStepper` | Booking | case workspace | 12 statuses grouped into 4 phases; current / done / future; collapses on narrow widths; cancelled banner |
| `EntityHeader` | Booking | detail pages | avatar or thumbnail, title, code (mono), badges, meta row with icons |
| `PersonCell` / `PersonCard` | Booking | lists, People card | initials avatar, name, sub-line, chips |
| `CustomerCombobox` (+ inline new lead) | Booking | create case, invoice (W3) | debounced search on `/booking/customers` |
| `StaffPicker` | Booking | assign, filters | needs the staff directory endpoint (§6) |
| `CatalogTourPicker` | Booking | builder, legs | search `catalog.v_tour` (title, sku, country), shows duration and cover |
| `ItineraryDayCard` | Booking | builder, preview | day header (N, date), items, collapse; preview mode renders the CIS day text + photo |
| `VersionItemCard` | Booking | builder | title, day range, source chip, notes, drag handle, edit / delete; read-only when locked |
| `PricePanel` | Booking | builder, calculator | cost lines editor + quote-preview result + floor warning |
| `MoneyText` | Booking | everywhere money shows | amount + ISO code, locale-formatted; never a bare `$` |
| `FromCisTag` / `ManagedInCisLink` | Booking | all CIS content | "From CIS" chip; "Edit in CIS ↗" deep link (A and CC only) |
| `NotificationPopover`, `NotificationItem` | Booking | topbar, `/notifications` | |
| `AuditTimeline` | Booking | case Activity tab, staff Activity tab, audit log | action badge, actor, summary, before / after diff toggle |
| `CsvExportButton` | kit `csv.ts` | lists | exports the current filter result (server-side for > 1 page, W2) |
| `Stepper` (wizard) | Booking | rate-sheet upload (W2), invoice create (W3) | numbered steps, back / next |
| `FileDropzone` | Booking | rate sheets (W2), payment evidence (W3) | presigned S3 upload to `booking.media_files` |
| `PhotoGallery` (read-only) | Booking | catalog pages, preview | CIS photo paths; lightbox |

---

## 6. API contract per route

All Booking calls go browser → Next.js BFF `/api/booking/*` → `https://<booking-api>/booking/*` with `Authorization: Bearer <booking JWT>`. The browser never holds a token. Base URL: the `aa-booking-dev` execute-api until `api-booking.lumiguides.it.com` has its ACM certificate (O2).

### 6.1 Endpoints that exist today or on the open branches

| Where | Method + path | Request | Response | Used by |
|---|---|---|---|---|
| main (live) | `GET /booking/health` | — | `{status, service, git_sha}` | BFF health widget |
| main (live) | `GET /booking/health/ready` | — | `{status, db, schema_version}` | — |
| CIS `cis-jwt` branch | `POST /auth/admin-login` (live) | `{username, password}` | CIS session token | `/login` BFF |
| CIS `cis-jwt` branch | `POST /auth/booking-token` | Bearer CIS token or `cis_admin_token` cookie | 15-minute RS256 JWT, `aud=booking`, `cis_role` claim | BFF |
| CIS `cis-jwt` branch | `GET /auth/.well-known/jwks.json` | — | JWKS | Booking API only |
| bk-681 | `GET /booking/staff/me` | — | `{cis_user_id, email, name, role, active, cis_role, …, permissions[]}` | shell, role gating |
| bk-681 | `GET /booking/staff?active=` | (admin) | `{items:[staff]}` | `/staff` |
| bk-681 | `PATCH /booking/staff/{cis_user_id}` | `{active?, role?}` (admin) | staff | `/staff` |
| bk-681 | `GET /booking/trip-cases?status=&assignee=me\|none\|<id>&limit≤100&offset` | — | `{items:[case + customer_* + assignees[] + leg_count + latest_version], total, limit, offset}` | `/cases`, dashboard |
| bk-681 | `GET /booking/trip-cases/{id\|code}` | — | case + `inquiry_snapshot`, `legs[]`, `versions[]` (number, notes, total_price, currency, sent / approved, item_count), `status_history[]`, `assignments[]`, `allowed_transitions[]` | case workspace |
| bk-681 | `PATCH /booking/trip-cases/{ref}` | `{pax?, start_date?, currency?, notes?}` | case + `changed[]` | Edit details, notes |
| bk-681 | `POST /booking/trip-cases/{ref}/status` | `{to_status, reason?}` (reason required for `cancelled`) | case | status menu, convert, decline |
| bk-681 | `POST /booking/trip-cases/{ref}/assign` | `{cis_user_id \| null, role='sales'\|contracting\|ops\|guide}` | assignment | Assign |
| bk-681 | `POST /booking/trip-cases/{ref}/versions` | `{notes?}` | 201 version (V1 from legs; Vn copies latest) | + New version, Duplicate |
| bk-681 | `GET /booking/trip-cases/{ref}/versions/{n}/items` | — | `{items:[{position, title, day_from, day_to, source_tour_id, notes}]}` | builder, preview |
| bk-681 | `PUT /booking/trip-cases/{ref}/versions/{n}/items` | `{items:[…]}` ≤ 200, full replace; 409 when approved | items | builder Save |
| bk-810 | `POST /booking/pricing/quote-preview` | `{channel, currency, peak, items:[{net_amount, net_currency}], override_total?}` | `{rule, fx_rates[], lines[], net_total_usd, subtotal, rounding_increment, total, override_total, final_total, effective_margin, floor_margin, below_floor, priced_on, …}` | price panel, calculator |

Permissions in bk-681 (code matrix): `trip_case.read` = A S C; `trip_case.write` / `assign` / `version.write` = A S; `staff.admin` = A. The bk-810 pricing roles are A S C.

**Integration defects found while mapping**. They block the UI, so they are backend work for W1:

- **D1**: bk-810 `auth/staff.py` has a separate `StaffPrincipal` / `require_staff` stub that always returns 401. bk-681 has the real `Staff` / `require(...)`. On merge, `quote-preview` must be rewired to `require("pricing.preview")`, a new permission for A S C. Otherwise the endpoint is unreachable. Both branches also edit `auth/staff.py`, `main.py` and `db.py`, so a merge conflict is certain.
- **D2**: bk-681 `PUT …/items` deletes and re-inserts every item. bk-004 `version_item_costs.version_item_id` is `ON DELETE CASCADE`, so **every Save wipes the cost lines**. Fix: items carry a stable `id` and PUT upserts by id (delete only the missing ones), or costs move into the PUT body (§6.2).
- **D3**: bk-003 transitions only cover pre-sale edges. `proposal_sent → confirmed` (customer approved) and `cancelled → new_inquiry` (Reopen, 02 #8) are missing, so Approve and Reopen cannot work.
- **D4**: `staff_users.name` / `email` are filled from JWT claims. `GET /booking/staff` is admin-only, so sales cannot render a `StaffPicker`. Needs a directory endpoint.

### 6.2 New endpoints: W1

| Method + path | Request | Response | For | Backend notes |
|---|---|---|---|---|
| `GET /booking/trip-cases` (extend) | `status` accepts a comma list (saved tabs Active / Closed); add `q` (code / customer), `source`, `created_from/to`, `sort` (updated_at\|created_at\|start_date, ±) | add `trip_title` (first-leg CIS title), `leg_titles[]` | case list, dashboard | join `catalog.v_tour` on `tour_id` (Q11) |
| `POST /booking/trip-cases` | `{customer_id \| new_customer:{name, email?, phone?, contact_consent}, start_date?, pax?, currency?, trip_style?, notes?, assignee?}` | 201 case | `/cases/new` | `source_system='manual'`; code from the sequence; lead rules as intake |
| `GET /booking/trip-cases/summary` | `?assignee=me` | `{open, new_inquiry, mine, proposals_awaiting, by_status:{…}, mom_delta}` | dashboard KPIs | |
| `GET /booking/trip-cases/{ref}/activity` | `limit, before` | `{items:[{kind:'status'\|'audit', at, actor{id,name}, action, summary, before?, after?}]}` | Activity tab | `audit_logs` where entity = case or its versions + `status_history` |
| `GET /booking/trip-cases/{ref}/versions/{n}` | — | `{version_number, title, notes, total_price, currency, fx_locked:{rate,as_of}?, valid_until, sent_at, approved_at, badge, created_by, updated_by, updated_at}` | builder header | add `title`, `updated_by`, `valid_until`, `fx_rate_locked`, `fx_as_of`, `channel` (bk-005) |
| `PATCH /booking/trip-cases/{ref}/versions/{n}` | `{title?, notes?}` | version | builder | 409 if approved or sent |
| `DELETE /booking/trip-cases/{ref}/versions/{n}` | — | 204 | Delete draft | only unsent, unapproved; audited; a soft delete is acceptable |
| `PUT /booking/trip-cases/{ref}/versions/{n}/items` (change) | items gain `id?` (uuid) and `costs:[{id?, net_amount, net_currency, supplier_note?}]` | items with ids + costs | builder Save | fixes D2; costs editable by A S C (Q8) |
| `POST /booking/trip-cases/{ref}/versions/{n}/price` | `{channel, currency, peak, override_total?, override_reason?}` | the quote-preview body + persisted `total_price` | builder "Apply price" | same engine; reason required when `below_floor`; audited |
| `POST /booking/trip-cases/{ref}/versions/{n}/send` | `{to, subject, message, valid_until}` | version (sent) + case status | Send modal | locks price, currency and FX on the version; case → `proposal_sent` (transition rule); customer `notifications` row; message stored (new `outbound_messages` or audit `after`) |
| `POST /booking/trip-cases/{ref}/versions/{n}/approve` | `{note?}` (staff records the customer's approval) | version (approved) + case | Proposals tab | needs edge `proposal_sent → confirmed` (D3); the approval later comes from the customer app (O8) |
| `GET /booking/customers` | `q, status(lead\|active\|past\|blocked), merge_review, source, limit, offset, sort` | `{items:[{id, name, email, phone, address, verified, is_blocked, needs_merge_review, source, case_count, last_activity_at, status}], total}` | `/customers` | status derived in a view `booking.v_customer_summary` |
| `GET /booking/customers/{id}` | — | customer + consent + merge candidate (id, name) + `cases[]` + `inquiries[]` + `recent_activity[]` | `/customers/[id]` | |
| `POST /booking/customers` | `{name, email?, phone?, address?, contact_consent}` | 201 customer | New lead | one of email / phone required; unverified |
| `PATCH /booking/customers/{id}` | `{name?, phone?, address?, email? (unverified only), is_blocked?}` | customer | Edit, Block | blocking is A only |
| `GET /booking/versions?q=&badge=draft\|sent\|approved\|superseded&assignee=&updated_from=&updated_to=&limit&offset&sort` | — | `{items:[{trip_case_code, customer{id,name}, version_number, title, badge, total_price, currency, leg_titles[], start_date, end_date, updated_at, updated_by{id,name}}], total}` | `/proposals` (critic pass: was missing) | badge derived from `is_sent_to_customer`, `is_approved`, later version exists |
| `DELETE /booking/customers/{id}` | — | 204 | Archive (soft delete) | sets `deleted_at`; 409 when a case is past `new_inquiry`; A only; audited (critic pass) |
| `GET /booking/staff/directory` | `role?` | `{items:[{cis_user_id, name, role}]}` active only | `StaffPicker` | any active staff (D4) |
| `GET /booking/staff/{cis_user_id}` | — | staff + `open_assignments[]` + counts | staff detail | admin, or self |
| `GET /booking/audit-logs?actor=&entity_type=&entity_id=&from=&to=&limit&before` | — | `{items:[{id, at, actor{type,id,name}, action, entity_type, entity_id, entity_label, summary, before, after}]}` | staff Activity tab (W1); `/audit-log` (W2) | add `entity_label`, `summary` at write time; indexes |
| `GET /booking/notifications?unread=&type=&limit&before` | — | `{items:[{id, type, title, message, entity_type, entity_id, created_at, read}], unread_count}` | bell, `/notifications` | bk-002 rows have `recipient_id NULL` (staff queue); bk-002 already has `type`, `entity_type`, `entity_id`, `data`; only `notification_reads(notification_id, cis_user_id)` is new (`read_at` is per-row, not per-staff) |
| `POST /booking/notifications/read` | `{ids[] \| all:true}` | `{unread_count}` | Mark read | |
| `GET/PUT /booking/staff/me/preferences` | `{notify:{type: bool}}` | prefs | `/settings` | small jsonb on `staff_users` |
| `GET /booking/catalog/tours?q=&country=&limit&offset` | — | `{items:[{source_id, tour_id, sku, title, country, duration_days, cover_photo_path, photo_count}]}` | `CatalogTourPicker`, leg titles | reads `catalog.v_tour` (grant to `booking` is in CIS migration 225) |
| `GET /booking/catalog/tours/{source_id\|tour_id}` | `?days=1&photos=1` | v_tour row + `days[]` (v_tour_day) + `places[]` (v_tour_place) + `photos[]` | builder expand, preview | photo URLs built from the CIS public API base |

### 6.3 New endpoints: W2 (supply, catalog, admin)

| Method + path | Request → response (sketch) | For |
|---|---|---|
| `GET/POST /booking/suppliers`, `GET/PATCH /booking/suppliers/{id}`, `POST …/{id}/deactivate\|activate`, `DELETE …/{id}` (soft; 409 when bookings, bills or active contracts exist) | `{company_name, categories[], website, internal_notes, contact_name, contact_title, contact_email, contact_phone, address, region, postal_code, destination_id, country_code, status}` | `/suppliers*` |
| `GET/POST /booking/suppliers/{id}/contracts`, `PATCH /booking/contracts/{id}` | `{valid_from, valid_to, currency, payment_terms, cancellation_policy, status(draft\|published\|expired)}` | Contracts tab |
| `POST /booking/rate-sheets` (multipart xlsx) → `{id, status:'validating'}`; `GET /booking/rate-sheets/{id}` → `{rows_ok, errors:[{sheet,row,field,message}], preview[]}`; `POST /booking/rate-sheets/{id}/publish` (403 if `uploaded_by == me`, maker-checker) | ADR 0005 §7–8 | rate-sheet review |
| `GET /booking/suppliers/{id}/rates?service_type=&valid_on=` | `{items:[{service_type, variant, season\|date range, unit, net, currency, supplements, child_policy, valid_from, valid_to}]}` | Rates tab, hotel / service Rates |
| `GET/POST /booking/hotels`, `GET/PATCH/DELETE /booking/hotels/{id}` | `{name, destination_id, supplier_id, address, hotel_style, links[{label,url}], internal_notes?, status}`; list rows also carry `photo_count` and `cover_photo_url` (first active supplier photo, presigned 15 min; AA-994 S6d) for the grid | `/hotels` (no customer-facing `description` or photos until Q17 is decided: O7; supplier photos are internal, bk-023) |
| `GET/POST /booking/services`, `GET/PATCH/DELETE /booking/services/{id}` | `{service_type, title(internal), destination_id, supplier_id?, max_group_size?, internal_notes, status, source:{system, id}?}` | `/services` |
| `GET /booking/pricing/rules`, `POST /booking/pricing/rules` (append-only new row), `GET/POST /booking/pricing/fx-rates` | rows of bk-004; publish requires a second person (Q19) | `/pricing/rules` |
| `GET /booking/catalog/trip-plans?q=&sellable=&country=&style=` | `catalog.v_tour` LEFT JOIN `booking.trip_plans` → `{source_id, title, sku, country, duration_days, cover, sellable, trip_style, from_price{amount,currency}?}` | `/catalog/trip-plans` |
| `PUT /booking/catalog/trip-plans/{source_id}/commercial` | `{sellable, supplier_id?, trip_style?, commercial_notes?}` → upserts `trip_plans` (`source_system='cis'`) | commercial edit |
| `GET/PUT /booking/catalog/trip-plans/{source_id}/prices` | season × pax band × currency × validity matrix (`trip_plan_prices`) | price matrix |
| As built (AA-996, bk-029): `GET /booking/catalog/geo/destinations?q=&country=&status=&sort=&limit=&offset=` (+ `summary`), `PATCH /booking/catalog/geo/destinations/{destination_id}` `{status?, status_reason?, internal_notes?}` (`catalog.admin`) | `catalog.v_destination` + `destination_profiles` | `/catalog/destinations` |
| As built (AA-996, bk-029): `GET /booking/catalog/geo/countries?q=&status=&area=` (+ `areas`, `unmapped`), `PATCH /booking/catalog/geo/countries/{iso}` `{status?, status_reason?, area?, internal_notes?}` (`catalog.admin`; `area` is free text, no `area_id`) | countries from `v_destination` / `v_tour` mapped through `booking.countries` + `country_profiles` + counts | `/catalog/countries` |
| `GET /booking/rate-sheets?status=validating\|ready\|published\|rejected&uploaded_by=` → `{items:[{id, supplier{id,name}, file_name, status, rows_ok, error_count, uploaded_by, uploaded_at}]}` | list for the contracting dashboard tile and the supplier Rates tab (critic pass) | dashboard, `/suppliers/[id]` |
| `POST /booking/pricing/rules/{id}/approve`, `POST /booking/pricing/fx-rates/{id}/approve` (403 if `created_by == me`) | maker-checker for rules and FX (Q19); a row is in force only when approved (critic pass) | `/pricing/rules` |
| `GET /booking/catalog/trip-plans/{source_id}` → v_tour summary + `trip_plans` commercial row + `prices[]` | detail page commercial panel (critic pass) | `/catalog/trip-plans/[sourceId]` |
| `POST /booking/catalog/trip-plans/{source_id}/trip-cases` `{customer_id \| new_customer, start_date, pax_adults, pax_children, notes?}` → 201 case (V1 from the plan's CIS days) | "Use for trip case" modal, TRIP TEMPLATE DETAILS-1 (critic pass) | trip plan detail |
| As built (AA-996): `GET /booking/catalog/geo/countries/{iso}` (+ `destination_items`, `tour_items`), `GET /booking/catalog/geo/destinations/{destination_id}` (+ `photos` from `v_destination_photo` when served, active suppliers / hotels / services at the place) | detail = row + profile + destinations / tours / photos (critic pass) | country detail page, destination drawer |
| `GET /booking/audit-logs` (+ export `?format=csv`) | as W1, all actors | `/audit-log` |
| `POST /booking/trip-cases/{ref}/versions/{n}/pdf` → `{media_id, url}` | rendered proposal PDF | Download PDF |
| `GET /booking/v_from_prices` (internal) / view `booking.v_from_prices` | contract `price.from.v1` for TripPlanner and CIS | not UI; listed for completeness |

### 6.4 New endpoints: W3 (money, operations)

> **Shipped shape (AA-682 S3, bk-028)** supersedes the W3 rows below where they differ: no tax lines (proforma,
> design D7); `received_on` (a day) instead of `received_at`; payments `recorded → verified | voided` with
> maker-checker; `Idempotency-Key` on create invoice / record payment; `POST …/amend` (append lines to a sent invoice);
> `GET /booking/trip-cases/{ref}/billing` for the case tab; KPIs per currency + USD at each version's locked FX.
> The OpenAPI document (`docs/api/openapi.json`) is the contract.

| Method + path | Request → response (sketch) | For |
|---|---|---|
| `GET /booking/invoices?status=&overdue=&case=&q=&from=&to=`, `GET /booking/invoices/{number}` | invoice + items + tax lines + schedule + payments + balance; list rows carry the staff `actions` too (row menu, AA-994 S6d) | `/billing`, invoice detail, case Billing tab |
| `POST /booking/trip-cases/{ref}/invoices` | `{lines_from_approved_version:true, adjustments:[{description, quantity, unit_price, reason}], issued_at, schedule:[{kind, amount\|pct, due_date}], notes, send_now}`; 409 without an approved version | Create Invoice |
| `POST /booking/invoices/{number}/send`, `/cancel {reason}`, `/remind {message?}` | status changes + notifications | row actions |
| `GET /booking/payments?…`, `GET /booking/payments/{ref}` | payment + invoice + schedule item + evidence | payments tab, detail |
| `POST /booking/invoices/{number}/payments` | `{payment_schedule_id?, amount, currency(=invoice), payment_method, received_at, reference, payment_description, evidence_media_id?}` (ERD column names; `reference` is new in the wave 3 migration) | Record Payment |
| `PATCH /booking/payments/{ref}` (note, reference only) / `POST …/void {reason}` | no hard delete (09 C2) | Edit / Void |
| `POST /booking/media/upload-url` → `{media_id, put_url}` | evidence, vouchers, bills | `FileDropzone` |
| `GET /booking/billing/summary` | revenue YTD (USD equivalent), outstanding, overdue, invoices sent this month | KPIs |
| `GET /booking/trip-checks?status=&due_before=&assignee=&code=&case=&q=`, `GET/POST /booking/trip-cases/{ref}/checks`, `PATCH /booking/trip-checks/{id}`, `GET /booking/case-lifecycle/runs` (AA-682 S2, shipped) | rows of `booking.trip_case_checks` (code, title, status open / done / not_applicable, due, assignee, note, emergency contact B2C only); three standard checks seeded by a DB trigger on confirm; derived checks from `v_trip_check_board` | `/trip-checks`, Checks tab |
| supplier bookings / bills: `GET/POST /booking/trip-cases/{ref}/supplier-bookings`, `GET /booking/supplier-bookings?supplier_id=` (supplier Bookings tab, critic pass), `PATCH /booking/supplier-bookings/{id}`, `GET/POST /booking/supplier-bills?supplier_id=` | ERD v2 tables | supplier Bookings / Bills tabs, case ops |
| supplier bills (AA-682 S4, bk-030, shipped): `GET/POST /booking/supplier-bills` (`Idempotency-Key` on POST), `GET /booking/supplier-bills/summary`, `GET /booking/supplier-bills/billable?supplier_id=&case=`, `GET/PATCH /booking/supplier-bills/{id}`, `POST …/approve` (another person than the creator and the last editor of the amounts / lines, `facts_edited_by`: 403 `maker_checker`), `POST …/cancel {reason}`, `POST …/payouts` (`Idempotency-Key`; admin, never the bill's creator nor its `facts_edited_by`), `POST /booking/supplier-payouts/{id}/void {reason}`, `GET /booking/trip-cases/{ref}/supplier-costs` | bill + lines + payouts + files; `approve_blockers`, `actions`; paid derived from payouts by the DB | `/billing?tab=supplier-bills`, supplier Bills tab, bill drawer, case cost vs billed |
| post-sale transitions | migration rows for confirmed → … → completed | status menu |

---

## 7. Content ownership (ADR 0004 O7, ADR 0002)

Rule: **if a customer reads it as catalog content (tour, day, place, photo, country, destination, activity or hotel description), CIS writes it.** Case-specific correspondence that only one customer reads (proposal item labels, staff notes on a version, the send message, invoice lines, reminders) is COMMERCIAL and written in Booking; it must never copy or rewrite CIS catalog text (critic pass: the earlier one-line rule contradicted §4.8–4.9).

- Booking renders CIS content **read-only** from `catalog.*`, which CIS migration 225 (branch `cis-679`) grants to role `booking`. Every such block carries a "From CIS" tag; admin and content_creator also get an "Edit in CIS ↗" link.
- Booking writes only commercial fields.

Catalog views available, keyed by `(source_system, source_id)`, where `source_id` is stable across re-uploads and `tour_id` is the current CIS version:

| View | Columns used by Booking |
|---|---|
| `catalog.v_tour` | title, subtitle, summary, description, highlights, country, duration_days, activities, inclusions, exclusions, best_time_to_go, group_size, sku, cover_photo_path, photo_count, itinerary_text, content_hash, updated_at |
| `catalog.v_tour_day` | day_number, title, body, places[] |
| `catalog.v_tour_place` | day_number, position, place_name, place_display_name, locality, region, country, destination_id, lat, lng, activity_types[], atom_count |
| `catalog.v_tour_photo` | photo_id, path, place_label, destination_id, position (1 = cover) |
| `catalog.v_destination` | destination_id, name, country (**text name, not ISO**), lat, lng, cover_photo_path, cover_image_url, photo_count |
| `catalog.v_destination_photo` | destination_id, photo_id, path, position |

| Figma screen(s) | Becomes | Read-only from CIS | Booking-owned (editable) |
|---|---|---|---|
| Trip Plan Templates: list, detail, create, edit (05) | **Catalog → Trip Plans**: a list over `v_tour`; "Create Trip Plan" becomes "Make sellable" on an existing CIS tour | title, subtitle, summary / description, highlights, inclusions / exclusions (**`v_tour` has them, so 05 C3 resolves to CIS**), duration, days (`v_tour_day`), places, photos, country | `trip_plans`: sellable flag (`is_published`), supplier, trip style (Q16), commercial notes; `trip_plan_prices` matrix (season × pax band × currency × validity, ADR 0005 §3); from-price (derived). Dropped from the Figma form: start/end dates, lat/lng, photo upload, Status dropdown, "Send to customer", age-category price rows |
| Countries: list, details, add, edit, delete, unpublish (06) | **Catalog → Countries**: derived from distinct `v_destination.country` / `v_tour.country` | name, destinations, photos (via destinations), tour counts | `country_profiles`: status (sellable), area. **Country description, FAQ and festivals have no CIS source in 225** (Q18). Until CIS adds one they are not shown, and Booking does not author them |
| Destinations: list, details, add, edit, delete (06) | **Catalog → Destinations**: a table over `v_destination`; "Add" becomes "attach CIS place"; "Delete" becomes "detach / deactivate" | name, country, lat, lng, photos, cover | `destination_profiles`: status, internal notes. Dropped: name / coords / address inputs, uploads. A missing place becomes a "Request in CIS" link |
| Activities: list, detail, add, edit, unpublish, delete (07) | **Supply → Services**: a commercial service (activity, transfer, guide, entrance, meal) at a CIS place | place (`v_destination`), activity types seen in tours (`v_tour_place.activity_types`); customer-facing activity text comes from the tour day text | `services`: type, internal title, supplier, max group size, internal notes, status; rates via contracts. **There is no CIS activity entity in 225** (Q17). Booking never writes customer-facing activity copy |
| Hotels (07) | **Supply → Hotels** (Booking-owned: CIS has no accommodation model) | the place (`destination_id` → `v_destination`) | name, supplier, address, links, style, status, rates (contracts). Customer-facing description and photos: Q17 (O7 exception vs CIS model vs none in v1) |
| Itinerary Builder / Preview (04) | **Proposals** (trip case versions, all COMMERCIAL) | day title, body and photo for items with `source_tour_id` (`v_tour_day`, `v_tour_photo`); places / map (`v_tour_place`) | item title (proposal label), day range, staff notes (case correspondence, length-capped), costs, price, send message; per-version included / excluded lines (W2, Q10) |
| Trip case Overview "Trip Information" (03) | read-only block in the case workspace | legs → tour title, duration, destinations, country | case dates, pax, currency, notes, assignees, status. Dropped: City, Latitude, Longitude, template "Description" editing |
| Suppliers (08) | Supply → Suppliers (all COMMERCIAL) | city via `destination_id` | everything else. "+ Add Activity" becomes "+ Add contract / rates" |
| Users, staff, customers, payments (02, 09) | no content | — | identity and money only |

**Identity caveat (Q11).** `trip_case_legs.source_tour_id` stores the CIS **`tour_id`**, which TripPlanner sends. `tour_id` changes when a tour is re-uploaded, whereas catalog identity is `source_id`. Joining on `tour_id` loses the title after a re-upload. Fix options:

- (a) a Booking migration adds `source_id` to legs and items, back-filled from `v_tour` (planned for bk-005, not built there: Q11 is still open);
- (b) the API resolves through a CIS mapping view of all historical `tour_id → source_id`.

---

## 8. Build waves

"FE" is the Next.js work in `AA-Booking/frontend` (new). "BE" is the AA-Booking backend. "CIS", "Infra" and "TP" are work other repos must ship first.

### Wave 1: shell + auth + trip cases + proposals / versions + pricing preview + customers + staff

| Item | Detail |
|---|---|
| FE | Scaffold Next.js 16 / React 19 / TS / Tailwind v4 / react-query / react-table; `ui/` kit copy + `tokens.css` + Tailwind theme; BFF (`/api/auth/*`, `/api/booking/[...path]` proxy, cookie refresh); `/login`, shell + nav + role gating from `/staff/me`; Dashboard; Notifications (popover + page); Settings; Cases list / new / workspace (Overview, Proposals, Activity); builder + preview + send; Proposals list; Pricing calculator; Customers list / detail; Staff list / detail |
| BE (in order) | 1. Merge bk-681 (bk-003) and bk-810 (bk-004); resolve the `auth/staff.py` conflict; rewire quote-preview (D1). 2. bk-005: transition edges `proposal_sent→confirmed`, `cancelled→new_inquiry` (D3); version `title`, `updated_by`, `valid_until`, `channel`, FX-lock columns; stable item ids / cost upsert (D2); `notification_reads` (notifications already have `type` / `entity_type` / `entity_id` in bk-002); staff preferences jsonb; `audit_logs.entity_label` / `summary` + indexes; `v_customer_summary`; customers `code` (Q15); legs / items `source_id` (Q11). 3. Endpoints of §6.2. 4. Every write audited (already the pattern in bk-003) |
| CIS | Merge `cis-jwt` (booking-token + JWKS) and `cis-679` (migration 225 + grants); set the Booking JWT signing secret; CORS / cookie not needed (server-side BFF) |
| Infra | Vercel project `aa-booking-staff` (preview per PR); Booking API env `CIS_JWKS_URL`, audience; optionally the `api-booking` ACM certificate + DNS (O2) |
| TP | none required; contract `inquiry.submitted` v1.2 (budget, message, pax split, travel window) is optional and makes the Inquiry panel richer (Q12) |
| Done | `next build` + lint in CI (Frontend Build check, non-required per ADR 0004 dec. 3); UI smoke on a Vercel preview (AA-732 pattern): login, case list, open case, create V2, save items with costs, price, send, approve; desktop / phone, light / dark; Dev rollout + live verify per aa-ship |

As built (S237 note): the Vercel project is `aa-booking-admin` (root `frontend/`), not `aa-booking-staff`; bk-005
shipped without the legs / items `source_id` (Q11 is still open); the dashboard tiles follow Nghiệp's S236 role rule
(New inquiries, In progress, Proposals sent, then Awaiting activation for admins or My open cases), and the
"open cases + month-over-month delta" figure of §4.2 sits on the Pipeline by phase card; the price summary shows the
card fee as its own row (it is built into the price, ADR 0005).

### Wave 2: suppliers + hotels / services commercial + catalog / templates + pricing admin + audit

| Item | Detail |
|---|---|
| FE | Suppliers (list, form, detail tabs Profile / Contracts / Rates); rate-sheet upload `Stepper` + review + publish (maker-checker); Hotels (grid + drawer); Services; Pricing rules & FX; Catalog Trip Plans (list, detail, commercial edit, price matrix); Destinations; Countries; Audit log page; builder typed items (hotel / service / place per day, period slots); proposal PDF; per-version inclusions |
| BE | bk-007 onward, one key per slice in delivery order: bk-007 = pricing rule / FX approvals (AA-810, a separate append-only `booking.policy_approvals` table, because bk-004 revokes UPDATE on the policy tables); bk-008 = suppliers + supplier contracts (wave 2a, AA-822); bk-009 = commercial-only `trip_plans` (AA-679, S237 decision D4); bk-010 = hotels + services (wave 2b, AA-823); later keys for the rest, numbered when built (keys as planned in S237; the delivery order decides). Content: `suppliers` extensions (categories, website, notes, contact title, region, postal code, destination_id, country_code), `supplier_contracts`, `supplier_rates` (per service type), `rate_sheets` + `rate_sheet_rows` (validation, `uploaded_by`, `published_by`, check uploader ≠ publisher), `hotels` (+ address, code), `services` (replaces ERD `activities`), `trip_plans` / `trip_plan_prices` keyed on CIS `source_id` (+ `commercial_notes`; ERD content columns `title`, `slug`, `summary`, `what_it_has`, `what_it_does_not_have` and tables `trip_plan_itinerary`, `trip_plan_destinations` are NOT created: O7), `country_profiles` / `destination_profiles` slimmed to commercial columns (drop ERD `name`, `subtitle`, `description`, `cover_media_id`, `faqs`, `festivals`, `weather`; add `destination_profiles.internal_notes`; O7), pricing rule / FX approvals (Q19; `policy_approvals` in bk-007, not `approved_by` / `approved_at` columns), `entity_media` limited to hotel internal photos only if Q17 allows, `booking.v_from_prices`; version items typed (`item_type`, `hotel_id`, `service_id`, `destination_id`, `activity_period`, `start_time`, `nights`, `room_type`); xlsx parser job (`bk_rate_sheet_validate`); PDF render job; endpoints §6.3 |
| CIS | (if Q17 / Q18 choose CIS) a country content model + `catalog.v_country`, and an activity / accommodation content model; a `tour_id → source_id` history view (Q11-b); later the `catalog.tour.*` outbox (price-impact notifications) |
| Done | maker-checker proven (the same user cannot publish); from-price view read by TripPlanner (contract `price.from.v1`) |

### Wave 3: payments / invoices + trip checks + supplier operations

| Item | Detail |
|---|---|
| FE | Billing (Invoices / Payments tabs, KPIs), invoice detail, Create Invoice (from approved version + schedule `Stepper`), Record Payment (+ evidence upload), Send reminder, Cancel / Void dialogs; case Billing tab; Trip checks list + drawer + case Checks tab; supplier Bookings / Bills tabs |
| BE | the next keys after wave 2 (numbered when built): `invoices` (+ `due_date`, notes / terms, tax lines), `invoice_items`, `payment_schedule`, `payments` (+ `reference`, `external_ref`, status received / voided), `tax_rules` (rates pending the accountant, ADR 0005 §10), `trip_case_tasks` (+ task template; replaces the ERD `trip_case_checks` boolean row, which cannot hold the drawn per-task due date / assignee), `supplier_bookings`, `supplier_bills`, post-sale transition edges; media presign + S3 bucket (Infra); overdue job `bk_invoice_overdue`; email sending (decision pending; until then notifications only); endpoints §6.4 |
| Infra | S3 bucket (SSE-S3, task role only) for evidence / vouchers; SES (if email is approved) |
| Gate | Tax rules confirmed with the accountant before invoices show tax; payment gateway is a separate decision (09 C11): W3 is manual recording only |

---

## 9. Open questions (for Nghiệp; recommendation first)

**Visual / technical**

- **Q1. Visual drift: Figma vs the CIS kit.** **Decided S239 (Nghiệp, AA-683): CIS kit wins on a–f.** Recommendation was: kit everywhere (one brand across CIS and Booking, ADR 0004 dec. 1).
  - a. Page background warm `#F8F6F2` (kit) or cool `#F4F6F8` (Figma).
  - b. Font Poppins + Fahkwang (kit) or Inter (Figma).
  - c. Sidebar flat (kit) or gradient (Figma).
  - d. Toast dark (kit) or white with a green bar (Figma).
  - e. Buttons as pills, uppercase primary (kit), or radius 8, sentence case (Figma).
  - f. Kit density or Figma size.
- **Q2. Tailwind and the inline-style kit.** Recommendation: kit components stay inline-style (byte-copy); Booking components use Tailwind mapped to the same vars. Alternative: rewrite the copied kit in Tailwind (more work, then drifts from CIS).
- **Q3. Token drift guard.** Should CI diff `ui/tokens.css` against CIS `globals.css`, or wait for the shared package (AA-655)? Recommendation: a cheap diff check now.

**Auth and roles**

- **Q4. Login flow (O1).** Recommendation: Booking `/login` + server-side BFF calling CIS `admin-login` → `booking-token`. Alternatives:
  - (b) a redirect to the CIS login with a return URL, sharing a cookie on `.lumiguides.it.com` (needs both apps on sub-domains of one parent; Vercel default domains differ);
  - (c) staff must already be logged into the CIS admin in the same browser (does not work across domains).
- **Q5. Role matrix.**
  - Does `contracting` read trip cases (bk-003 says yes)?
  - Does `content_creator` get anything beyond read-only catalog?
  - Is a separate `finance` or `ops` role needed for payments in W3?

  Recommendation: keep the 4 roles in W1; decide finance before W3.
- **Q6. Staff activation.** CIS non-admins are provisioned inactive. Should an admin get a notification "N staff awaiting activation"? Recommendation: yes (dashboard tile + notification).

**Trip cases and proposals**

- **Q7. Status vocabulary.** Show all 13 DB values with the 4-phase stepper (recommended), or collapse them to Figma's 6–8 display labels?
- **Q8. Who enters per-proposal net costs?** ADR 0005 says sales never edit **rate sheets**, while bk-004 `version_item_costs.entered_by` allows quote-specific costs. Recommendation: A S C may enter costs on a draft version until W2 rates exist; after W2, costs default from rates and a sales override needs a reason.
- **Q9. Builder model.** W1 ships bk-003 free-text items with day ranges (recommended, already built). Alternative: wait for typed day / period slots (Figma) in bk-005 before shipping any builder.
- **Q10. Inclusions / exclusions.** Tours read them from CIS `v_tour`. Should proposals also have their own commercial included / not-included lines (W2)? Recommendation: yes, seeded from the tours, locked when sent.
- **Q11. Tour identity in legs / items.** Store CIS `source_id` (recommended, bk-005 back-fill), or keep `tour_id` and resolve history through a CIS view?
- **Q12. Inquiry fields.** Should TripPlanner add budget, message, travel window and the adults / children split (contract `inquiry.submitted` v1.2)? Recommendation: yes; it is a TP and contract change, and the Inquiry panel shows them when present.
- **Q13. Case fields.** Add `title`, `pax_adults` / `pax_children`, `trip_style` and countries to the editable set (bk-005)? Recommendation: `pax_adults` / `pax_children` and countries yes; title derived (not stored).
- **Q14. Send-to address and customer email edits.** Recommendation: the default is the customer's email and is read-only; email is editable only while the customer is unverified.
- **Q15. Human codes.** Recommendation: keep `TC-000123` (live format) and add `CUS-000123` for customers. Drop the Figma `EMP-` / `CTR-` / `ACT-` / `TP-YYYY-NNN` formats. Show trip plans by CIS `sku`.

**Catalog and supply**

- **Q16. Trip style list.** Use the CIS taxonomy if CIS has one (content classification belongs to CIS), or a Booking lookup table?
- **Q17. Activities and hotels content.** CIS 225 has no activity or accommodation entity. Recommendation: Services and Hotels are Booking commercial records with internal text only; customer-facing hotel description and photos are none in v1, then a CIS accommodation model. The alternative is an O7 exception in an ADR amendment.
- **Q18. Country content** (description, FAQ, festivals). Not in CIS 225. Recommendation: CIS adds `catalog.v_country` when the content exists; Booking shows status only until then. Also: the ISO mapping of `shared.destinations.country` (text) is needed for country joins.
- **Q19. Maker-checker scope.** Does it apply to rate sheets only, or also to pricing rules and FX rows? Recommendation: all three (they move every price).
- **Q20. Supplier category and verification.** Recommendation: one or more of hotel / transport / guide / entrance / activity / meal; drop Figma's "awaiting document check" unless Ms. Thư confirms a verification step.

**Money (W3)**

- **Q21. Email.** CONTEXT.md says email is out of scope. Approve SES (or another provider) for proposal send, reminders and invoices in W2/W3? Until then copy must never claim an email was sent.
- **Q22. Payments.** Manual recording only in W3 (recommended), or start a gateway decision (Stripe / PayPal; the customer app per O8 pays online)?
- **Q23. Delete semantics.** No hard delete anywhere; invoices cancel with a reason; payments void with a reason; drafts soft-delete (recommended, ADR 0005 audit).
- **Q24. Invoice lines.** Invoice lines derive from the approved version (locked), with free lines only as audited adjustments (recommended), or keep Figma's free-form invoice editor?

**Scope**

- **Q25. TripPlanner customer UI.** The relayed request notes that TripPlanner also has no built UI. This spec covers the **Booking staff admin** only. The customer-facing Booking surface (sign-in, My Trips, approve proposal, pay; O8) and the TripPlanner web UI need their own spec. Recommendation: a separate spec next, reusing the same `ui/` token copy. **Moved to AA-680 (S239):** the customer-facing spec is written there; this file stays the staff admin spec.
- **Q26. Figma copy defects.** "Requires attentions", "Save Changs", "Finanlized", "ITINERARY BUIDER", "Suppplier", "flows" in pagers, "14:30 PM", wrong sidebar highlights, copy-pasted user menus on other entities, Kathmandu coordinates on Bali, mismatched counts. They are fixed in the build and not reproduced. Should they also go back to the designer? Recommendation: a short list to the designer, no blocking.

---

## 10. Critic pass (S236, completeness review)

Checked: all 120 PNG names in `docs/AAA/figma_AAA/` against §3, the 9 module specs, ERD v2 `booking.dbml`, the real migrations (bk-001, bk-002 on main; bk-003 on `bk-681`; bk-004 on `bk-810`) and ADR 0004 O7.

**Figma coverage.** Every PNG is named somewhere in §3. Two were mapped to the wrong screen:

- `ADD NEW ACTIVITY-2.png` is the "Edit Trip Case #TC-…" form (customer locked, template, agent, notes), not the builder. It moved to `/cases/[code]` (Edit details drawer).
- `TRIP TEMPLATE DETAILS-1.png` is the "Use for trip case" modal on a trip plan (W2), not `/cases/new`. It moved to `/catalog/trip-plans/[sourceId]` and got its own endpoint.

Ten confirm dialogs and drawers that are drawn (or implied) were added to the modal table: edit case details, archive customer, use trip plan, make not sellable, deactivate supplier, deactivate hotel or service, approve rule or FX, trip check drawer, and edit or void payment.

**Routes without an API contract (now added).**

| Route / UI | Added |
|---|---|
| `/proposals` (cross-case list) | `GET /booking/versions` (W1) |
| Customer archive (§4.12) | `DELETE /booking/customers/{id}`, a soft delete (W1) |
| `/cases` saved tabs Active / Closed | `status` accepts a comma list |
| Contracting dashboard tile "Rate sheets awaiting publish" | `GET /booking/rate-sheets?status=` (W2) |
| `/pricing/rules` maker-checker | `POST …/pricing/rules/{id}/approve`, `POST …/fx-rates/{id}/approve` (W2) |
| `/catalog/trip-plans/[sourceId]` commercial panel | `GET /booking/catalog/trip-plans/{source_id}` (W2) |
| Use for trip case modal | `POST /booking/catalog/trip-plans/{source_id}/trip-cases` (W2) |
| `/catalog/countries/[iso]`, destination drawer | `GET …/countries/{iso}`, `GET …/destinations/{id}` (W2) |
| Supplier Bookings / Bills tabs | `?supplier_id=` on supplier bookings and bills (W3) |

Still open (no change; they belong to open questions): the trip-style select in `/cases/new` has no source (Q16), and the countries multi-select needs `trip_case_countries` (Q13).

**Fields mapped to columns that do not exist.**

| Spec field | Finding | Change |
|---|---|---|
| Notifications "needs `type`, `entity_type`, `entity_id`" | Wrong: bk-002 already has them (plus `data`). Only per-staff read state is missing. | §6.2 and §8 W1 now ask only for `notification_reads` |
| Customer filter "country (`country_code`)" | No such column in the ERD or in bk-001/002 | Filter dropped. Source filter values corrected to `customers.source` (`manual` / `otp` / `tripplanner_lead`) |
| Record payment `schedule_id`, `method`, `note` | ERD names are `payment_schedule_id`, `payment_method`, `payment_description` | Renamed; `reference` marked new in the wave 3 migration |
| Create invoice `issue_date`, adjustment `qty` | ERD names are `invoices.issued_at`, `invoice_items.quantity` | Renamed |
| Hotel `description` | ERD column exists, but it is customer-facing content (O7, Q17) | Removed from the W2 contract; replaced by `internal_notes` |
| Destination profile `notes` | ERD has only `description` (content) | The wave 2 migration adds `internal_notes`, drops `description` |
| Trip plan `commercial_notes` | Not in the ERD and not in the wave 2 list | Added to the wave 2 migrations |
| Pricing rules / FX "publish requires a second person" | bk-004 has no approval columns | bk-007 adds the append-only `policy_approvals` table (S237; bk-004 revokes UPDATE, so no columns) |
| Trip check tasks (title, due, assignee) | ERD `trip_case_checks` is one boolean row per case | The wave 3 migration says `trip_case_tasks` replaces it |

The ERD itself is stale compared with bk-002 and bk-003. These columns exist in the database but not in `booking.dbml`, and the spec is right to use them:

- `customers.verified`, `source`, `needs_merge_review`, `merge_candidate_id`, `contact_consent`, `contact_consent_at`;
- `trip_cases.inquiry_snapshot`, `notes`;
- `staff_users` keyed by `cis_user_id` (TEXT), with `active`, `cis_role`, `last_seen_at` and role `contracting`;
- `trip_case_versions.sent_at`;
- the free-text `trip_case_version_itinerary` (`position`, `title`, `day_from`, `day_to`, `source_tour_id`, `notes`);
- `notifications.entity_type`, `entity_id`, `data`, with `recipient_id` nullable;
- `audit_logs.actor_ref`;
- `trip_case_legs.source_tour_id` is TEXT, not uuid.

Recommendation: refresh `booking.dbml` after bk-003 and bk-004 merge.

**O7 violations removed.**

- §7 rule. "If a customer reads it, CIS writes it" contradicted the proposal builder, where sales write item titles and notes the customer reads. It is now scoped to catalog content, and case correspondence is explicitly commercial and never copies CIS text.
- Hotel `description` was taken out of the W2 API (see the table above).
- The wave 2 migrations now say the ERD content columns and tables are not created:
  - `trip_plans`: `title`, `slug`, `summary`, `what_it_has`, `what_it_does_not_have`;
  - the tables `trip_plan_itinerary` and `trip_plan_destinations`;
  - `country_profiles`: `name`, `subtitle`, `description`, `cover_media_id`, `faqs`, `festivals`, `weather`;
  - `destination_profiles.description`;
  - `entity_media` galleries, except hotel photos if Q17 allows them.
- Left as an open question, not removed: Q10, per-version included / not-included lines. It stays commercial only if the lines are case-specific and seeded from CIS, never re-authored.
