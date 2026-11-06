// e2e/layout/fixtures.ts
// AA-910 — fixture answers for the layout check (`npm run test:layout`, Playwright project "layout-mock").
//
// The staff pages read the Booking API through the BFF (`/api/booking/<path>`); the spec intercepts those requests in
// the browser and answers them from `answer()` below. The data is deliberately the WORST realistic case for width:
// full pages (25 rows), long company / hotel / service / tour names, long e-mails, long contract titles, every
// optional column filled, FX rates with six decimals and a long source. Shapes are typed against lib/api, so a
// change in an API type that a fixture no longer satisfies fails `npm run typecheck`.
// S250 (after Nghiệp's live check on Dev): the rows also mix in the shapes Dev really has — staff without a CIS role or
// a name, cases without a date ("16 days" / "no date yet"), single- and multi-leg cases, two-digit pax, a customer row
// with three badges (status + Blocked + Merge review), customers without a phone, hotels without stars or a place.
//
// Nothing here is real: names, codes and e-mails are invented (example.test domains).

import type {
  AuditLogItem,
  CaseVersion,
  CursorPage,
  NotificationFeed,
  OffsetPage,
  StaffDetail,
  StaffMe,
  StaffProfile,
  TripCaseDetail,
  TripCaseListItem,
  TripCaseStatus,
  TripCaseSummary,
  VersionListItem,
} from "../../lib/api/types";
import type {
  SupplierActivity,
  SupplierContract,
  SupplierDetail,
  SupplierList,
  SupplierSummary,
} from "../../lib/api/suppliers";
import type {
  Hotel,
  HotelDetail,
  MediaList,
  Page,
  RoomType,
  Service,
  ServiceDetail,
  SupplierRate,
  SupplyContent,
  SupplyMedia,
} from "../../lib/api/supply";
import type { FxRateRow, PolicyList, PricingRuleRow } from "../../lib/api/pricingPolicy";
import type { Listed, PartnerTariff, PartnerTerms, RateCardRow, TierDiscountRow, TierList } from "../../lib/api/agencyPricing";
import type { PriceReadiness, ReadinessItem, ReadinessStatus } from "../../lib/api/priceReadiness";
import type { OrphanedPlanItem, PriceMatrix, TripPlanDetail, TripPlanFacets, TripPlanListItem } from "../../lib/api/tripPlans";
import type { DeskDepartures, DeskSummary, OnRequestLine, OnRequestPage } from "../../lib/api/b2bDesk";
import type { CountryDetail, CountryItem, CountryList, DestinationDetail, DestinationItem, DestinationPage } from "../../lib/api/geo";
import type { Page as BillingPage } from "../../lib/api/billing";
import type { LifecycleRun, TripCheck, TripCheckPage } from "../../lib/api/tripChecks";
import type { SupplierBookingSummary } from "../../lib/api/supplierBookings";
import type { SupplierBill, SupplierBillsSummary } from "../../lib/api/supplierBills";
import type {
  AgencyInvoice,
  AgencyInvoiceDetail,
  AgencyInvoiceLine,
  AgencyInvoiceStatus,
  AgencyPayment,
  AgencyPaymentStatus,
  BillingEntity,
  BillingHealth,
  BillingStatusRow,
  BillingTerms,
  DraftRun,
  InvoiceKind,
  Page as AgencyBillingPage,
  Summary as AgencyBillingSummary,
  TaxRule,
  TermsClause,
} from "../../lib/api/agencyBilling";
import staffMeJson from "./staff-me.json";

/** The browser clock during the check (layout.spec.ts freezes it with page.clock.setFixedTime): the run gives the same
 *  pixels on any day. It matches the `today` the pricing fixtures report. */
export const CLOCK = "2026-10-23T02:00:00+00:00";
// Every fixture timestamp is more than 7 days before CLOCK, so each relative "Updated" / "Last seen" / "Last activity"
// renders the absolute date (DateText relativeTime) — the widest form, and the one Dev shows for most rows. September
// is the widest en-GB short month in Poppins ("28 Sept 2026" = 78 px at 12 px; "1 Jul 2026" = 58 px), and the day has
// two digits.
const NOW = "2026-09-28T08:15:00+00:00";
const EARLIER = "2026-09-20T03:40:00+00:00";
const ROWS = 25; // the default page size of every list page
const pad = (n: number, w = 6) => String(n).padStart(w, "0");
const uuid = (prefix: string, i: number) => `${prefix}${prefix}${prefix}${prefix}-0000-4000-8000-${pad(i, 12)}`;
const range = (n: number) => Array.from({ length: n }, (_, i) => i);

const PERSON_NAMES = [
  "Nguyen Thi Thanh Huong-Phuong",
  "Tran Van Contracting-Officer",
  "Le Thi Second-Approver Manager",
  "Pham Quoc Bao-Long Sales Lead",
];
const PLACE = { id: "dest-hoi-an", name: "Hoi An Ancient Town and Riverside Quarter", country: "Vietnam" };

// ── Staff ────────────────────────────────────────────────────────────────────
/** The signed-in staff member (same file the SSR stub serves): an admin, so every page and action is shown. */
export const STAFF_ME: StaffMe = { ...staffMeJson, role: "admin" };

const ROLES = ["admin", "sales", "contracting", "content_creator"] as const;
function staffProfile(i: number): StaffProfile {
  return {
    cis_user_id: `u-${pad(i, 3)}`,
    email: `${PERSON_NAMES[i % 4].toLowerCase().replace(/[^a-z]+/g, ".")}${i}@long-company-mail-domain.example.test`,
    // A profile whose CIS account has no display name shows the e-mail as the name (Dev has such rows).
    name: i % 7 === 6 ? null : `${PERSON_NAMES[i % 4]} ${i}`,
    role: ROLES[i % 4],
    active: i % 5 !== 0,
    // Dev: seeded profiles have no CIS role ("—"); the others are admin / reviewer.
    cis_role: i % 6 === 5 ? null : i % 4 === 0 ? "admin" : "reviewer",
    created_at: EARLIER,
    last_seen_at: i % 3 ? NOW : null,
    managed_in_directory: i % 2 === 0,
  };
}

function staffDetail(id: string): StaffDetail {
  return {
    ...staffProfile(1),
    cis_user_id: id,
    updated_at: NOW,
    open_assignments: range(12).map((i) => ({
      role: i % 2 ? "sales" : "operations",
      assigned_at: EARLIER,
      trip_case_id: uuid("c", i),
      code: `TC-${pad(i + 1)}`,
      status: OPEN_STATUSES[i % OPEN_STATUSES.length],
      start_date: "2027-01-14",
      channel: i % 7 === 3 ? ("b2b" as const) : ("b2c" as const),
    })),
    counts: { open_assignments: 12, assignments: 57 },
  };
}

// ── Trip cases ───────────────────────────────────────────────────────────────
const OPEN_STATUSES: TripCaseStatus[] = [
  "new_inquiry",
  "discovery",
  "building",
  "proposal_sent",
  "confirmed",
  "supplier_confirming",
  "invoice_sent",
  "payment_pending",
  "paid",
  "pre_departure",
  "on_trip",
];

function tripCase(i: number): TripCaseListItem {
  // Dev has cases without a date (a TripPlanner inquiry gives a length, not a day) and single-leg cases next to
  // multi-leg ones. Dated rows use September dates, the widest en-GB month.
  const dated = i % 6 !== 5;
  const legs = i % 4 === 1 ? 1 : 3;
  return {
    id: uuid("c", i),
    code: `TC-${pad(i + 1)}`,
    status: OPEN_STATUSES[i % OPEN_STATUSES.length],
    channel: i % 7 === 3 ? "b2b" : "b2c",
    party_tenant_id: i % 7 === 3 ? "agency-tenant-with-a-long-identifier-0001" : null,
    trip_style: "Private family adventure with soft trekking",
    start_date: dated ? "2027-09-28" : null,
    end_date: dated ? "2027-10-17" : null,
    pax: 12,
    pax_adults: 10,
    pax_children: 2,
    currency: "USD",
    notes: "Two children under 10; one guest needs step-free access at every hotel.",
    source_system: i % 3 === 0 ? "tripplanner" : "manual",
    source_id: i % 3 === 0 ? `inq-${pad(i, 10)}` : null,
    created_at: EARLIER,
    updated_at: NOW,
    assignees: [
      { cis_user_id: "u-001", role: "sales", name: `${PERSON_NAMES[3]}`, assigned_at: EARLIER },
      { cis_user_id: "u-002", role: "operations", name: `${PERSON_NAMES[1]}`, assigned_at: EARLIER },
    ],
    countries: ["Vietnam", "Cambodia", "Laos", "Thailand"],
    latest_version: (i % 4) + 1,
    trip_title: i % 5 === 4 ? null : "Grand Indochina Discovery: Hanoi, Ha Long Bay, Hoi An, Angkor and the Mekong Delta",
    leg_titles: [
      "Hanoi Old Quarter and Ha Long Bay overnight cruise",
      "Hoi An Ancient Town, lantern workshop and My Son sanctuary",
      "Siem Reap temples of Angkor with a private archaeologist",
    ].slice(0, legs),
    leg_count: legs,
    duration_days: dated ? 20 : 16,
    trip_end_date: dated ? "2027-10-17" : null,
  };
}

const BY_STATUS = Object.fromEntries(
  [...OPEN_STATUSES, "completed", "cancelled"].map((s, i) => [s, 3 + ((i * 7) % 23)]),
) as Record<TripCaseStatus, number>;

const TRIP_CASE_SUMMARY: TripCaseSummary = {
  open: 214,
  new_inquiry: BY_STATUS.new_inquiry,
  mine: 37,
  proposals_awaiting: 18,
  by_status: BY_STATUS,
  mom_delta: { created_this_month: 61, created_last_month: 48, delta: 13 },
};

function caseVersion(n: number): CaseVersion {
  const badge = n === 1 ? "superseded" : n === 2 ? "sent" : n === 3 ? "approved" : "draft";
  return {
    id: uuid("e", n),
    version_number: n,
    title: "Grand Indochina Discovery with private guides, boutique hotels and a two-night Mekong river cruise",
    notes: "Customer asked for a slower pace in Hoi An and one free day in Siem Reap.",
    total_price: "48250.75",
    currency: "USD",
    channel: "b2c",
    valid_until: "2026-11-30",
    fx_locked: { rate: "25432.500000", as_of: NOW },
    is_sent_to_customer: badge !== "draft",
    sent_at: badge !== "draft" ? NOW : null,
    sent_by: badge !== "draft" ? "u-001" : null,
    is_approved: badge === "approved",
    approved_at: badge === "approved" ? NOW : null,
    approved_by: badge === "approved" ? "u-003" : null,
    approval_note: null,
    created_by: "u-001",
    created_at: EARLIER,
    updated_by: "u-001",
    updated_at: NOW,
    badge,
    item_count: 18 + n,
  };
}

function tripCaseDetail(ref: string): TripCaseDetail {
  const { latest_version: _latest, ...base } = tripCase(0);
  void _latest;
  return {
    ...base,
    code: ref,
    inquiry_snapshot: null,
    inbox_event_id: null,
    legs: [
      { position: 1, source_tour_id: "tour-1", day_from: 1, day_to: 6 },
      { position: 2, source_tour_id: "tour-2", day_from: 7, day_to: 13 },
      { position: 3, source_tour_id: "tour-3", day_from: 14, day_to: 20 },
    ],
    versions: [1, 2, 3, 4, 5, 6].map(caseVersion),
    status_history: [
      { from_status: null, to_status: "new_inquiry", changed_by: "u-001", note: null, changed_at: EARLIER },
      { from_status: "new_inquiry", to_status: "discovery", changed_by: "u-001", note: "Called the customer", changed_at: NOW },
    ],
    assignments: [],
    allowed_transitions: ["building", "cancelled"],
    tour_alerts: [],
  };
}

function versionListItem(i: number): VersionListItem {
  const { updated_by: _u, ...v } = caseVersion((i % 4) + 1);
  void _u;
  return {
    ...v,
    id: uuid("f", i),
    trip_case_id: uuid("c", i),
    trip_case_code: `TC-${pad(i + 1)}`,
    trip_case_status: OPEN_STATUSES[i % OPEN_STATUSES.length],
    trip_case_channel: i % 7 === 3 ? "b2b" : "b2c",
    start_date: "2027-01-14",
    end_date: "2027-02-02",
    sales_id: "u-001",
    updated_by: { id: "u-001", name: PERSON_NAMES[3] },
    leg_titles: tripCase(i).leg_titles,
    trip_title: tripCase(i).trip_title,
    duration_days: 20,
  };
}

// ── Notifications + audit ────────────────────────────────────────────────────
// AA-996 slice 5: one row per kind of target (case, invoice, payment, checks, supplier bookings, none), so the page
// shows the per-type icons, labels and "Open …" actions; long titles stay the widest case.
const NOTIFICATION_KINDS: [string, string, string, Record<string, unknown>][] = [
  ["trip_case.new_inquiry", "New inquiry from TripPlanner: Grand Indochina Discovery for a family of six", "trip_case", { code: "TC-000001" }],
  ["invoice.overdue", "TC-000022: 1,284,500,000 VND overdue since 2026-10-01 on INV-000004", "invoice", { invoice_number: "INV-000004", case_code: "TC-000022" }],
  ["payment.recorded", "PMT-000007 recorded on INV-000004: verify it", "payment", { ref: "PMT-000007", invoice_number: "INV-000004" }],
  ["trip_check.due", "TC-000010: Emergency contact of every traveller is overdue", "trip_case", { case_code: "TC-000010" }],
  ["supplier_booking.waiting", "TC-000010: Emerald Riverside Boutique Hotel and Spa has not answered SB-000003", "supplier_booking", { code: "SB-000003", case_code: "TC-000010" }],
  ["retention.held", "Retention held: booking.inbox_events", "retention", {}],
];
const NOTIFICATIONS: NotificationFeed = {
  items: NOTIFICATION_KINDS.map(([type, title, entity_type, data], i) => ({
    id: `n-${i}`,
    type,
    title,
    message: "Alexandra Catherine Montgomery-Whitfield asked for 20 days from 14 January 2027.",
    entity_type,
    entity_id: uuid("c", i),
    data,
    created_at: NOW,
    read: i > 1,
  })),
  next_before: null,
  unread_count: 2,
};

const ACTIVITY: SupplierActivity[] = [
  ["supplier_rate.created", "supplier_rate", "SUP-000001 · 2026-2027 Allotment and Static Net Rate Agreement · Emerald Riverside Boutique Hotel · Family Suite with River View", "Added rate 1,250,000.00 VND"],
  ["supplier_rate.deleted", "supplier_rate", "SUP-000001 · 2026 Allotment · Emerald · Family Suite", "Removed rate 7.00 VND"],
  ["supplier_contract.published", "supplier_contract", "2026-2027 Allotment, Static Net Rate and Long-Stay Promotion Agreement", "Published contract"],
  ["trip_case_version.superseded", "trip_case_version", "TC-000001 V1", "V1 superseded: V2 was sent"],
  ["supplier.updated", "supplier", "SUP-000001", "Changed contact e-mail"],
].map(([action, entity_type, entity_label, summary], i) => ({
  id: `a-${i}`,
  at: NOW,
  action,
  entity_type,
  entity_label,
  summary,
  actor: { id: "u-002", name: PERSON_NAMES[1] },
}));

const AUDIT_TYPES = ["supplier_rate", "supplier_contract", "trip_case_version", "fx_rate", "pricing_rule", "hotel_room_type", "customer", "staff_user"];
function auditLogs(): CursorPage<AuditLogItem> {
  return {
    items: range(50).map((i) => ({
      id: `al-${i}`,
      at: NOW,
      actor: { type: "staff", id: `u-${pad(i % 4, 3)}`, name: `${PERSON_NAMES[i % 4]}` },
      action: `${AUDIT_TYPES[i % AUDIT_TYPES.length]}.updated`,
      entity_type: AUDIT_TYPES[i % AUDIT_TYPES.length],
      entity_id: uuid("a", i),
      entity_label:
        "SUP-000001 · 2026-2027 Allotment and Static Net Rate Agreement · Emerald Riverside Boutique Hotel and Spa · Family Suite with River View",
      summary: "Changed net amount from 1,150,000.00 VND to 1,250,000.00 VND and the season label to High season (Christmas, New Year and Tet)",
      before: { net_amount: "1150000.00" },
      after: { net_amount: "1250000.00" },
    })),
    next_before: "al-49",
  };
}

const SUP_ID = uuid("1", 0);
const HOTEL_ID = uuid("2", 0);
const SVC_ID = uuid("3", 0);
const CONTRACT_ID = uuid("4", 0);

function supplier(i: number) {
  return {
    id: uuid("1", i),
    code: `SUP-${pad(i + 1)}`,
    company_name: `Vietnam Heritage Riverside Hospitality and Transport Joint Stock Company ${i}`,
    categories: ["hotel", "transport", "guide", "activity"] as SupplierList["items"][number]["categories"],
    website: "https://www.example-very-long-supplier-domain-name.test/en/partners",
    internal_notes: "Pays on 30 days net; ask for the long-stay discount table every season.",
    contact_name: "Nguyen Thi Thanh Huong-Phuong",
    contact_title: "Senior Sales and Contracting Manager",
    contact_email: "reservations.contracting.department@example-very-long-supplier-domain-name.test",
    contact_phone: "+84 235 3912 345 ext 1024",
    address: "123 Tran Hung Dao Street, Minh An Ward",
    region: "Quang Nam",
    postal_code: "560000",
    destination_id: PLACE.id,
    destination: PLACE,
    country_code: "VN",
    country_name: "Vietnam",
    status: (i % 3 ? "active" : "inactive") as "active" | "inactive",
    contracts_in_force: 3,
    created_by: "u-001",
    updated_by: "u-001",
    created_at: EARLIER,
    updated_at: NOW,
  };
}

function contract(i: number, status: SupplierContract["status"], phase: SupplierContract["phase"]): SupplierContract {
  return {
    id: i === 0 ? CONTRACT_ID : uuid("4", i),
    supplier_id: SUP_ID,
    title: `2026-2027 Allotment, Static Net Rate and Long-Stay Promotion Agreement ${i}`,
    reference: "VHR/CTR/2026/000123-AMEND-02",
    valid_from: "2026-01-01",
    valid_to: "2027-03-31",
    currency: "VND",
    payment_terms: "30 days net from the invoice date",
    cancellation_policy: "Free until 7 days before arrival",
    notes: null,
    status,
    phase,
    created_by: "u-002",
    created_by_name: PERSON_NAMES[1],
    updated_by: "u-002",
    updated_by_name: PERSON_NAMES[1],
    published_by: status === "draft" ? null : "u-003",
    published_by_name: status === "draft" ? null : PERSON_NAMES[2],
    published_at: status === "draft" ? null : NOW,
    expired_by: null,
    expired_by_name: null,
    expired_at: null,
    created_at: EARLIER,
    updated_at: NOW,
  };
}

const CONTRACTS = [
  contract(0, "published", "in_force"),
  contract(1, "draft", "draft"),
  contract(2, "published", "upcoming"),
  contract(3, "published", "ended"),
  contract(4, "expired", "expired"),
];

function rate(i: number, service: boolean): SupplierRate {
  return {
    id: uuid("5", i + (service ? 100 : 0)),
    contract_id: CONTRACT_ID,
    contract_title: "2026-2027 Allotment, Static Net Rate and Long-Stay Promotion Agreement",
    contract_status: i % 2 ? "draft" : "published",
    supplier_id: SUP_ID,
    hotel_room_type_id: service ? null : `rt-${i % 3}`,
    room_type_name: service ? null : `Family Suite with River View and Private Balcony ${i % 3}`,
    hotel_id: service ? null : HOTEL_ID,
    hotel_code: service ? null : "HTL-000001",
    hotel_name: service ? null : "Emerald Riverside Boutique Hotel and Spa Hoi An",
    service_id: service ? SVC_ID : null,
    service_code: service ? "SVC-000001" : null,
    service_title: service ? "Private Hoi An Ancient Town Walking Tour with Lantern Making Workshop" : null,
    season: "High season (Christmas, New Year and Tet holiday peak)",
    valid_from: "2026-12-20",
    valid_to: "2027-02-15",
    unit: service ? "person" : "room_night",
    meal_plan: service ? null : "BB",
    pax_min: 1,
    pax_max: 12,
    net_amount: "12500000.00",
    currency: "VND",
    notes: "Extra bed 450,000 VND per night; children under 6 stay free sharing existing bedding.",
    in_force: i % 2 === 0,
    created_by: "u-002",
    updated_by: null,
    created_at: EARLIER,
    updated_at: NOW,
  };
}

function supplyBase(i: number) {
  return {
    supplier_id: SUP_ID,
    supplier_code: "SUP-000001",
    supplier_name: "Vietnam Heritage Riverside Hospitality and Transport Joint Stock Company",
    supplier_status: "active" as const,
    destination_id: PLACE.id,
    destination: PLACE,
    country_code: "VN",
    country_name: "Vietnam",
    internal_notes: null,
    status: (i % 4 ? "active" : "inactive") as "active" | "inactive",
    rates_in_force: 12,
    created_by: "u-001",
    updated_by: "u-001",
    created_at: EARLIER,
    updated_at: NOW,
  };
}

const CIS_CONTENT: SupplyContent = {
  content_status: "published",
  content_version: 12,
  description:
    "A heritage riverside hotel a short walk from the Old Town lanes, with a saltwater pool shaded by frangipani, bicycles to borrow and a quiet garden terrace where breakfast is served until late morning.",
  highlights: ["Riverside garden terrace and saltwater pool", "Ten minutes on foot to the Japanese Covered Bridge", "Bicycles to borrow"],
  photo_urls: [],
  edit_url: "/admin/supply-content/5b0d1c7e-2f4a-4c55-9a43-7f1f0c2b9e10",
  // AA-1040: the CIS 366 mark (a staff edit of the writer's draft) — the panel shows the "AI-assisted" badge
  ai: { ai_generated: true, generator: "Adventure Asia supply content writer", model: null, generated_at: "2026-10-02T03:00:00+00:00", human_edited: true },
};

function supplyMedia(i: number, entity: "hotel" | "service"): SupplyMedia {
  return {
    id: `m-${i}`,
    entity_type: entity,
    entity_id: HOTEL_ID,
    content_type: "image/jpeg",
    bytes: 2_950_000 - i * 400_000,
    width: 4032,
    height: 3024,
    sha256: "0".repeat(64),
    usage_rights: i !== 1,
    status: "active",
    uploaded_by: { id: "u-001", name: "Nguyen Thi Bich Ngoc Contracting" },
    updated_by: "u-001",
    created_at: "2026-09-28T09:30:00+00:00",
    updated_at: "2026-09-28T09:30:00+00:00",
    url: null,
  };
}

function hotel(i: number): Hotel {
  return {
    ...supplyBase(i),
    id: i === 0 ? HOTEL_ID : uuid("2", i),
    code: `HTL-${pad(i + 1)}`,
    name: `Emerald Riverside Boutique Hotel and Spa Hoi An ${i}`,
    hotel_style: "Boutique heritage riverside resort",
    // Dev has hotels without a star rating ("—") and without a CIS place (country only).
    star_rating: i % 5 === 4 ? null : 3 + (i % 3),
    ...(i % 6 === 5 ? { destination_id: null, destination: null } : {}),
    address: "123 Tran Hung Dao Street, Minh An Ward",
    latitude: "15.8771",
    longitude: "108.3260",
    check_in_time: "14:00",
    check_out_time: "12:00",
    amenities: ["Pool", "Spa", "Airport transfer"],
    links: [],
    room_type_count: 6,
    // The photo grid (S254 Figma audit #19): most hotels have supplier photos, some none (the neutral tile).
    photo_count: i % 4 === 3 ? 0 : 1 + (i % 3) * 6,
    cover_photo_url: i % 4 === 3 ? null : `https://signed.example/supply-media/hotel/${uuid("2", i)}/cover.jpg`,
  };
}

function roomType(i: number): RoomType {
  return {
    id: `rt-${i}`,
    hotel_id: HOTEL_ID,
    name: `Family Suite with River View and Private Balcony ${i}`,
    max_occupancy: 4,
    bed_types: ["King", "Twin"],
    internal_notes: null,
    status: "active",
    rates_in_force: 3,
  };
}

function service(i: number): Service {
  return {
    ...supplyBase(i),
    id: i === 0 ? SVC_ID : uuid("3", i),
    code: `SVC-${pad(i + 1)}`,
    service_type: (["activity", "transfer", "guide", "entrance", "meal"] as const)[i % 5],
    title: `Private Hoi An Ancient Town Walking Tour with Lantern Making Workshop ${i}`,
    unit: (["person", "group", "vehicle", "day"] as const)[i % 4],
    source_system: i % 2 ? "cis" : null,
    source_id: i % 2 ? `tour-${i}` : null,
    max_group_size: 12,
    duration_minutes: 240,
    inclusions: ["Guide", "Entrance fees", "Bottled water"],
  };
}

function page<T>(items: T[], total = 137): Page<T> & OffsetPage<T> {
  return { items, total, limit: ROWS, offset: 0 };
}

// ── Pricing ──────────────────────────────────────────────────────────────────
const person = (id: string, i: number) => ({ id, name: PERSON_NAMES[i % 4] });
const FX_ROWS: [FxRateRow["status"], string, string][] = [
  ["pending", "VND", "25432.500000"],
  ["in_force", "VND", "25410.000000"],
  ["in_force", "THB", "33.875000"],
  ["in_force", "IDR", "16250.000000"],
  ["scheduled", "KHR", "4105.250000"],
  ["superseded", "LAK", "21875.125000"],
  ["superseded", "MMK", "2099.987654"],
];
const FX: PolicyList<FxRateRow> = {
  today: "2026-10-23",
  items: FX_ROWS.map(([status, currency, rate_per_usd], i) => ({
    id: `fx-${i}`,
    status,
    created_by: person("u-002", 1),
    created_at: NOW,
    approved_by: status === "pending" ? null : person("u-003", 2),
    approved_at: status === "pending" ? null : NOW,
    can_approve: status === "pending",
    currency,
    rate_per_usd,
    buffer_pct: "0.0150",
    as_of: "2026-09-28",
    source: "Vietcombank published sell rate, 08:30 ICT, checked against the State Bank of Vietnam reference rate",
  })),
};
const RULES: PolicyList<PricingRuleRow> = {
  today: "2026-10-23",
  items: (
    [
      ["pending", "b2c_tailor_made"],
      ["in_force", "b2c_tailor_made"],
      ["in_force", "b2b"],
      ["scheduled", "fixed_departure"],
      ["superseded", "b2c_tailor_made"],
    ] as const
  ).map(([status, channel], i) => ({
    id: `r-${i}`,
    status,
    created_by: person("u-002", 1),
    created_at: NOW,
    approved_by: status === "pending" ? null : person("u-003", 2),
    approved_at: status === "pending" ? null : NOW,
    can_approve: status === "pending",
    channel,
    target_margin: "0.30",
    floor_margin: "0.18",
    peak_uplift_pts: "0.05",
    card_fee_pct: "0.03",
    valid_from: "2026-10-01",
    note: "Peak season uplift agreed with sales for the Christmas, New Year and Tet period across all channels",
  })),
};

// ── Agency tiers (AA-1030) ───────────────────────────────────────────────────
const TIER_LABEL: Record<string, string> = { standard: "Standard", silver: "Silver", gold: "Gold" };
const AGENCY_TIERS: TierList = {
  today: "2026-10-23",
  b2b_rule: { target_margin: "0.1500", floor_margin: "0.1000", card_fee_pct: "0.0300", valid_from: "2026-10-14", rrp_markup_pct: "0.2500" },
  items: (
    [
      ["standard", "0.0000", "0.1500", false, 14, 0],
      ["silver", "0.0300", "0.1246", false, 6, 1],
      ["gold", "0.0600", "0.0977", true, 3, 0],
    ] as const
  ).map(([code, discount, margin, below, partners, pending], i) => ({
    code,
    label: TIER_LABEL[code],
    sort_order: (i + 1) * 10,
    partners,
    discount_in_force: discount,
    pending,
    margin_after_discount: margin,
    below_floor: below,
  })),
};
const TIER_DISCOUNTS: Listed<TierDiscountRow> = {
  today: "2026-10-23",
  items: (
    [
      ["pending", "silver", "0.0400"],
      ["in_force", "gold", "0.0600"],
      ["in_force", "silver", "0.0300"],
      ["in_force", "standard", "0.0000"],
    ] as const
  ).map(([status, tier, discount], i) => ({
    id: `td-${i}`,
    status,
    created_by: person("u-002", 1),
    created_at: NOW,
    approved_by: status === "pending" ? null : person("u-003", 2),
    approved_at: status === "pending" ? null : NOW,
    can_approve: status === "pending",
    tier_code: tier,
    tier_label: TIER_LABEL[tier],
    discount_pct: discount,
    valid_from: "2026-10-01",
    note: "Starting value from the business decision page, to review after the first three agency contracts",
  })),
};
const AGENCY_NAMES = ["Lotus Lantern Travel", "Heron Coast Journeys", "Blue Bamboo Holidays", "Northern Pass Tours"];
function partnerTerms(i: number): PartnerTerms {
  const tier = (["standard", "silver", "gold"] as const)[i % 3];
  const discount = AGENCY_TIERS.items[i % 3].discount_in_force;
  const card = i === 2 ? "0.0450" : null;
  return {
    id: `pt-${i}`,
    agency_name: `${AGENCY_NAMES[i % AGENCY_NAMES.length]} ${i + 1}`,
    status: i === 5 ? "suspended" : "active",
    billing_currency: "USD",
    projected: true,
    tier_code: tier,
    tier_label: TIER_LABEL[tier],
    tier_discount: discount,
    card_discount: card,
    plan_cards: i === 2 ? 2 : 0,
    effective_discount: card ?? discount,
  };
}
// AA-1030 G3: an agency tariff, six pax bands x two windows of four invented tours (long titles), one row unpriced.
const TARIFF_BANDS: [number, number | null][] = [[1, 1], [2, 2], [3, 4], [5, 6], [7, 10], [11, null]];
const TARIFF: PartnerTariff = {
  partner: { id: "pt-1", agency_name: "Heron Coast Journeys 2", status: "active", billing_currency: "USD", tier_code: "silver", tier_label: "Silver" },
  currency: "USD",
  priced_on: "2026-10-23",
  basis: "per_person_band_min_twin_share",
  tier_code: "silver",
  fx: { base_currency: "USD", quote_currency: "USD", rate_per_usd: "1", rate_date: "2026-10-23", source: "base" },
  items: range(4).flatMap((t) =>
    ["2027-01-01", "2027-04-01"].flatMap((from, w) =>
      TARIFF_BANDS.map(([min, max], b) => {
        const each = 420 - b * 35 + w * 20 + t * 15;
        return {
          tour: { source_system: "cis" as const, source_id: `tour-${t}` },
          title: `Invented Highlands and Hidden Lagoons Discovery Journey ${t + 1}`,
          country: "Invented Land",
          duration_days: 4 + t,
          season: w ? "Green season (shoulder months)" : "High",
          valid_from: from,
          valid_to: w ? "2027-09-30" : "2027-03-31",
          pax_min: min,
          pax_max: max,
          priced_for_pax: min,
          priced_for_date: from,
          price: { amount: `${each * min}.00`, currency: "USD" },
          price_per_person: { amount: `${each}.00`, currency: "USD" },
          agency_discount_pct: t === 2 ? "0.0800" : "0.0300",
          rate_card_id: t === 2 ? "card-1" : null,
          rrp_per_person: { amount: `${Math.ceil((each * 1.25) / 5) * 5}.00`, currency: "USD" },
        };
      }),
    ),
  ),
  unpriced: [
    {
      tour: { source_system: "cis", source_id: "tour-9" },
      title: "Invented Coastal Villages by Bicycle",
      season: null,
      valid_from: "2027-01-01",
      valid_to: "2027-06-30",
      pax_min: 1,
      pax_max: 1,
      reason: "no rate in force on 2027-01-01 for 1 per room",
    },
  ],
  truncated: false,
};
// ── Price readiness (AA-1029) ───────────────────────────────────────────────
// Every status, long titles, a plan out of the catalog, a missing FX and a long list of components without a rate.
const READINESS_CYCLE: ReadinessStatus[] = [
  "no_components",
  "missing_rates",
  "priced",
  "no_components",
  "hand_priced",
  "missing_fx",
  "no_components",
  "inactive_component",
];
function readinessItem(i: number): ReadinessItem {
  const status = READINESS_CYCLE[i % READINESS_CYCLE.length];
  const priced = status === "priced" || status === "hand_priced";
  const unrated =
    status === "missing_rates"
      ? range(5).map((c) => ({
          day_no: c + 1,
          kind: c % 2 ? ("service" as const) : ("hotel" as const),
          code: c % 2 ? `SVC-00${4200 + c}` : `HTL-00${3100 + c}`,
          room_type: c % 2 ? null : "Superior Garden View Twin with Balcony",
          supplier_code: `SUP-00${1100 + c}`,
        }))
      : [];
  return {
    tour_key: `aa000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
    title: i === 9 ? null : `Invented Highlands and Hidden Lagoons Discovery Journey ${i + 1}`,
    country: ["Invented Land", "Lake Country of the North", "Coastland"][i % 3],
    duration_days: 3 + (i % 12),
    in_catalog: i !== 9,
    sellable: i % 4 === 0,
    status,
    reason: priced ? (status === "hand_priced" ? "no_components" : null) : status,
    components: status === "no_components" ? 0 : 6 + (i % 5),
    components_without_rate: unrated,
    missing_fx: status === "missing_fx" ? ["VND", "THB"] : [],
    computed_rows: status === "priced" ? 12 : 0,
    hand_rows: status === "hand_priced" ? 2 : 0,
    hand_rows_expired: i % 7 === 0 ? 1 : 0,
    from_price: priced ? { amount: `${1235 + i * 10}.00`, currency: "USD", season: "Green season (shoulder months)" } : null,
    pending_recompute: false,
    note: null,
  };
}
const READINESS_ITEMS = range(ROWS).map(readinessItem);
const READINESS: PriceReadiness = {
  today: "2026-10-23",
  horizon_to: "2027-10-23",
  rules: ["b2b", "fixed_departure"],
  fx_currencies: ["EUR", "THB"],
  summary: {
    total: 892,
    by_status: { priced: 41, hand_priced: 12, missing_rates: 96, missing_fx: 8, no_rule: 0, no_components: 731, inactive_component: 4 },
    by_country: ["Invented Land", "Lake Country of the North", "Coastland"].map((country, i) => ({
      country,
      total: 300 - i * 4,
      priced: 14,
      hand_priced: 4,
      missing_rates: 32,
      missing_fx: 3,
      no_rule: 0,
      no_components: 245 - i * 4,
      inactive_component: 2,
    })),
  },
  items: READINESS_ITEMS,
  degraded: [],
};
const RATE_CARDS: Listed<RateCardRow> = {
  today: "2026-10-23",
  items: (
    [
      ["pending", null, "0.0500"],
      ["in_force", "e1000000-0000-4000-8000-000000000101", "0.0800"],
      ["in_force", null, "0.0450"],
      ["expired", null, "0.0400"],
      ["scheduled", "e1000000-0000-4000-8000-000000000101", null],
    ] as const
  ).map(([status, source, discount], i) => ({
    id: `rc-${i}`,
    status,
    created_by: person("u-002", 1),
    created_at: NOW,
    approved_by: status === "pending" ? null : person("u-003", 2),
    approved_at: status === "pending" ? null : NOW,
    can_approve: status === "pending",
    partner_id: "pt-2",
    agency_name: "Blue Bamboo Holidays 3",
    trip_plan_id: source ? "tp-1" : null,
    source_id: source,
    discount_pct: discount,
    ends_contract_rate: discount === null,
    valid_from: "2026-10-01",
    valid_to: status === "expired" ? "2026-09-30" : "2027-09-30",
    contract_ref: "Invented contract BB-2026-07",
    note: null,
  })),
};

// ── Catalog trip plans ───────────────────────────────────────────────────────
function tripPlan(i: number): TripPlanListItem {
  return {
    source_system: "cis",
    source_id: `tour-${pad(i + 1, 5)}`,
    tour_id: `tour-${pad(i + 1, 5)}`,
    sku: `AA-VN-${pad(i + 1, 4)}-PRIVATE`,
    title: `Grand Indochina Discovery: Hanoi, Ha Long Bay, Hoi An, Angkor and the Mekong Delta ${i}`,
    country: (["Vietnam", "Cambodia", "Laos", "Thailand"] as const)[i % 4],
    duration_days: 14 + (i % 8),
    cover_photo_path: `/media/photos/layout-${i}.jpg`,
    photo_count: 24,
    lifecycle_stage: "published",
    updated_at: NOW,
    plan_id: i % 3 ? uuid("6", i) : null,
    has_commercial_row: i % 3 !== 0,
    sellable: i % 2 === 0,
    trip_style: "Private family adventure with soft trekking",
    default_channel: i % 2 ? "b2b" : "b2c_tailor_made",
    price_hint: { amount: "4850.00", currency: "USD", basis: "per_person" },
    // AA-996 slice 2: the widest real values (a 5-digit price with season, a long supplier, eight long places).
    from_price: i % 2 === 0 ? { amount: "12485.00", currency: "USD", basis: "per_person", season: "Low season" } : null,
    supplier_id: i % 3 ? uuid("5", i) : null,
    supplier: i % 3 ? { id: uuid("5", i), code: `SUP-${pad(i + 1)}`, company_name: "Red River Destination Management Company", status: "active" } : null,
    destinations: ["Hanoi", "Ha Long Bay", "Hoi An Ancient Town", "Siem Reap", "Phnom Penh", "Can Tho", "Luang Prabang", "Vientiane"].slice(0, 2 + (i % 7)),
  };
}

// AA-996 slice 2: one trip plan with its CIS content (long text) and the price matrix (priced and hand rows).
function tripPlanDetail(sourceId: string): TripPlanDetail {
  return {
    ...tripPlan(1),
    source_id: sourceId,
    source_version: 4,
    slug: "grand-indochina-discovery",
    subtitle: "Fourteen days from the Red River Delta to the temples of Angkor",
    summary: "A long private journey through Vietnam and Cambodia with soft trekking, home stays and boat days. ".repeat(4),
    description: null,
    highlights: ["Ha Long Bay overnight cruise", "Angkor Wat at sunrise"],
    duration_raw: "15 days / 14 nights",
    inclusions: "All private transfers and the internal flights\nEnglish-speaking guides every day\nBreakfast daily, 9 lunches, 4 dinners",
    exclusions: "International flights\nVisa fees\nTravel insurance and personal expenses",
    best_time_to_go: "October to April",
    group_size: "2-12",
    itinerary_text: null,
    published_at: NOW,
    content_hash: "h",
    commercial_notes: "DMC net rates 2027, 2 pax minimum, child policy as per contract",
    days: range(15).map((d) => ({ day_number: d + 1, title: `Day ${d + 1} title`, body: "Body", places: ["Hanoi"] })),
    photos: [],
    cases_started: 3,
    commercial: { created_at: EARLIER, updated_at: NOW, created_by: { id: "u-001", name: PERSON_NAMES[1] }, updated_by: null },
  };
}

const PRICE_MATRIX: PriceMatrix = {
  source_id: "",
  // AA-1029: the from price is the suggested retail price of the cheapest b2b row for 2 (root ADR 0019 b15).
  from_price: { amount: "15610.00", currency: "USD", basis: "per_person", season: "Low season" },
  items: [
    { position: 1, channel: "b2b", season: "Low season", peak: false, valid_from: "2027-01-01", valid_to: "2027-04-30", pax_min: 2, pax_max: 2, currency: "USD", amount: "12485.00", net_amount: "8365.00", net_currency: "USD", effective_margin: "0.3000", priced_on: "2026-10-20", priced: true, overridden: false, computed: true, rrp_amount: "15610.00" },
    { position: 2, channel: "b2b", season: "Low season", peak: false, valid_from: "2027-01-01", valid_to: "2027-04-30", pax_min: 3, pax_max: 4, currency: "USD", amount: "10950.00", net_amount: null, net_currency: null, effective_margin: null, priced_on: null, priced: false, overridden: false, computed: false, rrp_amount: "13690.00" },
    { position: 3, channel: "fixed_departure", season: "Tet holiday peak", peak: true, valid_from: "2027-02-01", valid_to: "2027-02-20", pax_min: 11, pax_max: null, currency: "EUR", amount: "11250.00", net_amount: "185000000.00", net_currency: "VND", effective_margin: "0.1890", priced_on: "2026-10-20", priced: true, overridden: true, computed: true, rrp_amount: null },
  ],
  reference: { raw: "from USD 12,900 per person", amount: "12900.00", currency: "USD", reviewed_by: null, reviewed_at: null },
  changes: [
    { at: "2026-10-20T08:00:00Z", change_source: "cost_sheet", changed_by: "system:bk_price_recompute", rows: 3 },
    { at: "2026-10-18T08:00:00Z", change_source: "staff", changed_by: "u-001", rows: 2 },
  ],
};

const TRIP_PLAN_FACETS: TripPlanFacets = {
  total: 412,
  sellable: 276,
  orphaned: 9,
  countries: [
    { country: "Vietnam", tours: 188, sellable: 131 },
    { country: "Cambodia", tours: 74, sellable: 49 },
    { country: "Laos", tours: 61, sellable: 38 },
    { country: "Thailand", tours: 89, sellable: 58 },
  ],
  trip_styles: ["Private family adventure with soft trekking", "Classic culture and food", "Beach and islands"],
};

function orphanedPlan(i: number): OrphanedPlanItem {
  return {
    source_system: "cis",
    source_id: `tour-${pad(900 + i, 5)}`,
    plan_id: uuid("7", i),
    orphaned: true,
    sellable: i % 2 === 0,
    trip_style: "Private family adventure with soft trekking",
    default_channel: "b2c_tailor_made",
    price_hint: { amount: "4850.00", currency: "USD", basis: "per_person" },
    updated_at: NOW,
    catalog: {
      source_system: "cis",
      source_id: `tour-${pad(900 + i, 5)}`,
      state: i % 3 ? "retired" : "paused",
      reason: "Supplier closed the river route for the whole 2027 season after the new dam works were announced",
      changed_at: NOW,
    },
    open_cases: 3,
    future_departures: 2,
    confirmed_holds: 1,
  };
}

// ── B2B desk (AA-975) ────────────────────────────────────────────────────────
const SLAS: OnRequestLine["sla"][] = ["overdue", "due_soon", "on_time", "answered", "closed"];
function onRequestLine(i: number): OnRequestLine {
  const sla = SLAS[i % SLAS.length];
  const answered = sla === "answered" || sla === "closed";
  return {
    hold_id: uuid("8", i),
    line_ref: `L${i + 1}`,
    status: answered ? (i % 2 ? "confirmed" : "declined") : "on_request",
    sla,
    answer_due_at: NOW,
    requested_at: EARLIER,
    answered_at: answered ? NOW : null,
    answered_by: answered ? person("u-002", 1) : null,
    decline_code: answered && i % 2 === 0 ? "supplier_unavailable" : null,
    trip_case: { id: uuid("c", i), code: `TC-${pad(i + 1)}` },
    proposal_ref: `PRP-${pad(i + 1)}-V2`,
    partner: { id: uuid("9", i), agency_name: `Southeast Asia Discovery Travel and Incentives Agency Ltd ${i}`, tenant_id: "agency-tenant-with-a-long-identifier-0001" },
    component: {
      id: uuid("a", i),
      kind: i % 2 ? "hotel_room" : "service",
      label: i % 2 ? "Family Suite with River View and Private Balcony" : "Private Hoi An Ancient Town Walking Tour with Lantern Making Workshop",
      supplier: { id: SUP_ID, name: "Vietnam Heritage Riverside Hospitality and Transport Joint Stock Company" },
    },
    service_date: "2027-01-14",
    nights: i % 2 ? 3 : null,
    pax: { adult: 4, child: 2, infant: 1 },
    seats: 7,
    departure: i % 3 ? { id: uuid("b", i), start_date: "2027-01-10" } : null,
  };
}
const DESK_SUMMARY: DeskSummary = { on_request: { open: 23, overdue: 4, due_soon: 6 }, open_incidents: 2, due_soon_hours: 24 };
function deskDepartures(): DeskDepartures {
  return {
    from: "2026-10-24",
    to: "2027-04-24",
    items: range(ROWS).map((i) => ({
      id: uuid("b", i),
      trip_plan_id: uuid("6", i),
      source_id: `tour-${pad(i + 1, 5)}`,
      tour_title: `Grand Indochina Discovery: Hanoi, Ha Long Bay, Hoi An, Angkor and the Mekong Delta ${i}`,
      start_date: "2027-01-10",
      capacity: 24,
      held: 6,
      sold: 12,
      available: 6,
      status: (["scheduled", "guaranteed", "cancelled"] as const)[i % 3],
      auto_confirm: i % 2 === 0,
      partners: 5,
      open_incident:
        i % 4 === 0
          ? { id: `inc-${i}`, departure_id: uuid("b", i), incident_code: "political", flagged_at: NOW, flagged_by: person("u-002", 1), cleared_at: null, cleared_by: null }
          : null,
    })),
  };
}

// ── AA-996: Catalog → Destinations / Countries (long names, every optional field filled) ─────────────────────────
const PLACE_NAMES = ["Ha Long Bay (Lan Ha Bay and Cat Ba Archipelago)", "Luang Prabang", "Siem Reap", "Chiang Mai Old City"];
const GEO_COUNTRIES = ["Vietnam", "Laos", "Cambodia", "Thailand", "Myanmar", "Bhutan", "Nepal", "Indonesia"];
const GEO_ISO = ["VN", "LA", "KH", "TH", "MM", "BT", "NP", "ID"];

function destination(i: number): DestinationItem {
  const inactive = i % 6 === 5;
  return {
    destination_id: uuid("d", i),
    name: `${PLACE_NAMES[i % PLACE_NAMES.length]}${i > 3 ? ` ${i}` : ""}`,
    country: GEO_COUNTRIES[i % GEO_COUNTRIES.length],
    country_code: GEO_ISO[i % GEO_ISO.length],
    lat: -8.4095 + i * 1.111111,
    lng: 115.1889 - i * 0.733333,
    cover_photo_path: null,
    cover_image_url: null,
    photo_count: (i * 7) % 130,
    status: inactive ? "inactive" : "active",
    status_reason: inactive ? "No contracted guides for the 2026-27 season after the operator closed" : null,
    internal_notes: inactive ? "Check again with the DMC in March." : null,
    profile_updated_at: inactive ? NOW : null,
    profile_updated_by: inactive ? "u-001" : null,
    profile_updated_by_name: inactive ? "Alexandra Montgomery-Whitfield" : null,
    // AA-997: a few places CIS keeps off customer maps (the row's extra badge is the widest name cell)
    hidden_from_customers: i % 7 === 3 || i % 11 === 6,
    hidden_reason: i % 7 === 3 ? "location_unconfirmed" : i % 11 === 6 ? "merged" : null,
    merged_into: i % 7 !== 3 && i % 11 === 6 ? uuid("d", i + 1) : null,
  };
}

function destinationPage(): DestinationPage {
  return {
    ...page(range(ROWS).map(destination), 1872),
    summary: { total: 1872, active: 1790, inactive: 82, countries: 14, hidden: 57 },
  };
}

function destinationDetail(id: string): DestinationDetail {
  return { ...destination(5), destination_id: id, active_suppliers: 12, active_hotels: 34, active_services: 120, photos: [] };
}

function countryItem(i: number): CountryItem {
  return {
    country_code: GEO_ISO[i % GEO_ISO.length],
    name: GEO_COUNTRIES[i % GEO_COUNTRIES.length],
    destinations: 120 + i * 37,
    photos: i % 4 === 2 ? 0 : 1400 + i * 211,
    cover_photo_path: i % 4 === 2 ? null : `/content/photos/layout-country-${i}`,
    tours: 30 + i * 9,
    active_suppliers: 4 + i,
    area: i % 2 ? "Indochina and the Mekong region" : null,
    booking_reference: true,
    status: i === 3 ? "inactive" : "active",
    status_reason: i === 3 ? "Paused while the new DMC contract is signed" : null,
    internal_notes: null,
    profile_updated_at: i === 3 ? NOW : null,
    profile_updated_by: null,
    profile_updated_by_name: null,
  };
}

const COUNTRY_LIST: CountryList = {
  items: range(GEO_ISO.length).map(countryItem),
  total: GEO_ISO.length,
  areas: ["Indochina and the Mekong region"],
  unmapped: [{ name: "Tibet Autonomous Region", destinations: 3 }],
};

function countryDetail(iso: string): CountryDetail {
  return {
    ...countryItem(1),
    country_code: iso.toUpperCase(),
    destination_items: range(ROWS).map(destination),
    tour_items: range(12).map((i) => ({
      source_id: uuid("t", i),
      title: `Classic Mekong Discovery from Huay Xai to Luang Prabang by Slow Boat ${i}`,
      sku: `LA-${10 + i}D`,
      duration_days: 10 + i,
      cover_photo_path: null,
      sellable: i % 3 === 0,
    })),
    photo_items: range(24).map((i) => ({
      path: `/content/photos/layout-gallery-${i}`,
      caption: i % 5 === 4 ? null : `${PLACE_NAMES[i % PLACE_NAMES.length]} ${i}`,
      destination_id: i % 3 === 2 ? null : uuid("d", i),
    })),
  };
}

// ── Router ───────────────────────────────────────────────────────────────────
const billingPage = <T,>(items: T[], total: number): BillingPage<T> => ({ items, total, limit: ROWS, offset: 0 });

// ── Supplier bills (AA-996 review fix: the list exports CSV like every list page) ─────────────────────────────────────
// Bill 1 carries a formula-like supplier bill number: the CSV must neutralise it (csvCell.ts).
function supplierBill(i: number): SupplierBill {
  const status = (["received", "approved", "paid", "cancelled"] as const)[i % 4];
  return {
    id: uuid("9", i),
    code: `BIL-${pad(i + 1)}`,
    supplier: { id: SUP_ID, name: "Vietnam Heritage Riverside Hospitality and Transport Joint Stock Company" },
    trip_case: i % 3 === 2 ? null : { id: uuid("8", i), code: `TC-${pad(i + 1)}`, channel: "b2c" },
    bill_number: i === 0 ? "=HYPERLINK(\"http://x.test\",\"click\")" : `VHR/2026/10/${pad(i + 1, 4)}`,
    currency: i % 2 ? "USD" : "VND",
    amount: i % 2 ? "12450.00" : "1250000000",
    issue_date: "2026-09-28",
    due_date: "2026-10-09",
    status,
    notes: null,
    document_file_id: null,
    created_by: person("u-002", i),
    facts_edited_by: null,
    approved_by: status === "approved" || status === "paid" ? person("u-003", 2) : null,
    approved_at: status === "approved" || status === "paid" ? NOW : null,
    paid_at: status === "paid" ? NOW : null,
    cancelled_by: status === "cancelled" ? person("u-003", 2) : null,
    cancelled_at: status === "cancelled" ? NOW : null,
    cancel_reason: status === "cancelled" ? "Duplicate" : null,
    created_at: EARLIER,
    updated_at: NOW,
    paid_amount: status === "paid" ? (i % 2 ? "12450.00" : "1250000000") : "0",
    balance: status === "paid" ? "0" : i % 2 ? "12450.00" : "1250000000",
    live_payouts: status === "paid" ? 1 : 0,
    line_count: 2,
    lines_amount: i % 2 ? "12450.00" : "1250000000",
    overdue: status === "approved",
    approve_blockers: status === "received" ? ["maker_checker"] : [],
    actions: { edit: status === "received", attach: true, approve: false, cancel: status !== "paid" && status !== "cancelled", pay: status === "approved" },
  };
}
const billKpi = (count: number) => ({ count, by_currency: [{ currency: "VND", amount: "3750000000" }, { currency: "USD", amount: "24900.00" }] });
const SUPPLIER_BILLS_SUMMARY: SupplierBillsSummary = {
  to_approve: billKpi(7),
  to_pay: billKpi(6),
  overdue: billKpi(2),
  paid_this_month: billKpi(5),
  bills: { received: 7, approved: 6, paid: 9, cancelled: 2 },
  month_start: "2026-10-01",
  today: "2026-10-23",
};

// ── Trip checks (AA-996 audit slice 1: the list must fit 1440 px with its Status column in view) ─────────────────────
// Worst case for width: long custom check titles, long customer names, a B2B row, overdue and done checks, owners with
// long names and owners without a name.
const CHECK_CODES = ["pre_trip_recheck", "emergency_contact", "insurance", "custom"] as const;
function tripCheck(i: number): TripCheck {
  const code = CHECK_CODES[i % 4];
  const b2b = i % 7 === 3;
  return {
    id: uuid("e", i),
    code,
    title: code === "custom" ? "Confirm the private boat transfer and the river-side picnic permit with the harbour office" : "",
    status: i % 5 === 4 ? "done" : "open",
    due_date: i % 6 === 5 ? null : "2026-09-28",
    assignee_ref: i % 3 === 0 ? null : `u-${pad(i % 4, 3)}`,
    assignee: i % 3 === 0 ? null : { id: `u-${pad(i % 4, 3)}`, name: i % 8 === 7 ? null : `${PERSON_NAMES[i % 4]} ${i}` },
    done_by: null,
    done_at: null,
    note: null,
    emergency_contact_name: null,
    emergency_contact_phone: null,
    cleared_at: null,
    created_by: null,
    created_at: EARLIER,
    updated_at: NOW,
    standard: code !== "custom",
    contact_allowed: !b2b,
    trip_case: {
      id: uuid("8", i),
      code: `TC-${pad(i + 1)}`,
      status: "supplier_confirming",
      channel: b2b ? "b2b" : "b2c",
      start_date: "2026-09-28",
      end_date: "2026-10-17",
    },
  };
}
const LIFECYCLE_RUNS: LifecycleRun[] = range(3).map((i) => ({
  id: uuid("f", i),
  kind: i % 2 ? "bk_ops_reminders" : "bk_case_lifecycle",
  status: "succeeded",
  created_by: null,
  created_at: EARLIER,
  started_at: EARLIER,
  finished_at: EARLIER,
  attempt: 1,
  result: i % 2 ? { checks_due_notified: 4, supplier_waiting_notified: 2, payments_notified: 1 } : { mode: "live", moves: { pre_departure: 2 }, blocked: 0 },
  error: null,
}));

// ── Agency billing (AA-1037: Agency billing tabs + an agency invoice; the visual run's fixture) ───────────────────────
// Invented agencies (tenant ids with letters in every group: no long digit run, lesson 179), every billing status
// (ok / warn / past_due), both invoice kinds (AAS- software fee, AAT- tour settlement), both currencies (VND amounts
// are the widest money strings), drafts without a number, an overdue issued invoice, paid and void rows, payments in
// every state and an invented issuing entity with an invented payment line. The admin of staff-me.json holds every
// agency billing permission, so the write and approve controls show.
const AB_AGENCIES: { tenant_id: string; agency_name: string; health: BillingHealth; country: string }[] = [
  { tenant_id: "a1b2c3d4-e5f6-4a7b-8c9d-ab10cdef001a", agency_name: "Lotus Lantern Travel and Heritage Journeys Company Limited", health: "past_due", country: "VN" },
  { tenant_id: "b1b2c3d4-e5f6-4a7b-8c9d-ab10cdef002a", agency_name: "Heron Coast Journeys", health: "warn", country: "TH" },
  { tenant_id: "c1b2c3d4-e5f6-4a7b-8c9d-ab10cdef003a", agency_name: "Blue Bamboo Holidays", health: "ok", country: "VN" },
  { tenant_id: "d1b2c3d4-e5f6-4a7b-8c9d-ab10cdef004a", agency_name: "Northern Pass Tours and Mountain Trekking Specialists", health: "ok", country: "LA" },
  { tenant_id: "e1b2c3d4-e5f6-4a7b-8c9d-ab10cdef005a", agency_name: "Quiet Lagoon Expeditions", health: "ok", country: "KH" },
  { tenant_id: "f1b2c3d4-e5f6-4a7b-8c9d-ab10cdef006a", agency_name: "Paper Crane Family Travel", health: "past_due", country: "VN" },
];
const AB_STATUSES: AgencyInvoiceStatus[] = ["issued", "issued", "draft", "paid", "void"];
const abId = (prefix: string, i: number) => `${prefix}0000000-ab10-4037-8000-f1c7e0a${String(i).padStart(5, "0")}`;

/** One agency invoice: even rows the software fee (USD), odd rows the tour settlement (VND, wider). */
function agencyInvoice(i: number): AgencyInvoice {
  const settlement = i % 2 === 1;
  const kind: InvoiceKind = settlement ? "settlement" : "saas";
  const status = AB_STATUSES[i % AB_STATUSES.length];
  const agency = AB_AGENCIES[i % AB_AGENCIES.length];
  const n = Math.floor(i / 2) + 1;
  const number = status === "draft" ? null : `${settlement ? "AAT" : "AAS"}-2026-${pad(n)}`;
  const currency = settlement ? "VND" : "USD";
  const total = settlement ? "1284500000" : "1299.00";
  const paid = status === "paid" ? total : status === "issued" && i % 3 === 0 ? (settlement ? "385350000" : "299.00") : "0";
  const balance = status === "issued" ? (settlement ? (i % 3 === 0 ? "899150000" : total) : i % 3 === 0 ? "1000.00" : total) : "0";
  const overdue = status === "issued" && i % 4 === 0 ? 9 + i : 0;
  return {
    id: abId("a", i),
    number,
    party_tenant_id: agency.tenant_id,
    agency_name: agency.agency_name,
    invoice_kind: kind,
    status,
    period_start: settlement ? "2026-09-01" : "2026-10-01",
    period_end: settlement ? "2026-09-30" : "2026-10-31",
    currency,
    subtotal: settlement ? total : "1180.91",
    tax_rate: settlement ? "0.0000" : "0.1000",
    tax_amount: settlement ? "0" : "118.09",
    total,
    paid,
    waiting: status === "issued" && i % 5 === 3 ? (settlement ? "120000000" : "500.00") : "0",
    balance,
    overdue_days: overdue,
    issued_on: status === "draft" ? null : "2026-10-01",
    due_date: status === "draft" ? null : overdue ? "2026-10-14" : "2026-10-31",
    issued_by: status === "draft" ? null : "u-002",
    e_invoice_no: status === "paid" ? `DEMO-EINV-${pad(n, 4)}` : null,
    staff_note: null,
    void_reason: status === "void" ? "Billed twice for the same month; the corrected invoice replaces it" : null,
    footer_wording: settlement ? null : "Invented footer: tax treatment wording printed under the totals of the software fee invoice.",
    issuer: { code: "DEMO-ISSUER", legal_entity: AB_ENTITY_NAME, payment_instructions: AB_PAYMENT_LINE },
    bill_to: { legal_entity_name: agency.agency_name, agency_name: agency.agency_name, tax_id: "DEMO-TAX-0420", billing_country: agency.country },
    source: i % 7 === 6 ? "staff" : "job",
    created_at: EARLIER,
  };
}
const AB_ENTITY_NAME = "Example Issuer Trading and Travel Services Company Limited";
const AB_PAYMENT_LINE = "Bank transfer to Example Bank, account name Example Issuer, account EXAMPLE-ACCOUNT-01; put the invoice number in the reference.";

function agencyPayment(i: number): AgencyPayment {
  const inv = agencyInvoice((i * 3) % ROWS);
  const status: AgencyPaymentStatus = (["recorded", "approved", "rejected"] as const)[i % 3];
  return {
    id: abId("b", i),
    invoice_id: inv.id,
    invoice_number: inv.number ?? `AAS-2026-${pad(i + 1)}`,
    invoice_kind: inv.invoice_kind,
    agency_name: inv.agency_name,
    amount: inv.currency === "VND" ? "385350000" : "299.00",
    currency: inv.currency,
    method: (["bank_transfer", "cash", "card_offline", "other"] as const)[i % 4],
    received_on: "2026-10-12",
    reference: `Example Bank transfer DEMO-REF-${pad(i + 1, 4)}`,
    document_ref: `Bank statement line DEMO-STMT-${pad(i + 1, 4)}`,
    note: null,
    status,
    // Row 4 was recorded by the signed-in admin: "Another person approves" instead of the buttons.
    recorded_by: i === 3 ? "u-layout" : `u-${pad((i % 4) + 1, 3)}`,
    recorded_at: EARLIER,
    approved_by: status === "approved" ? "u-003" : null,
    rejected_by: status === "rejected" ? "u-003" : null,
    reject_reason: status === "rejected" ? "Not on the bank statement of the received date" : null,
  };
}

/** The agency invoice page: AAS- = the software fee, AAT- = the tour settlement; issued and 9 days overdue. */
function agencyInvoiceDetail(ref: string): AgencyInvoiceDetail {
  const settlement = ref.toUpperCase().startsWith("AAT");
  const base = agencyInvoice(settlement ? 1 : 0);
  const currency = base.currency;
  const lines: AgencyInvoiceLine[] = settlement
    ? range(6).map((i) => ({
        line_no: i + 1,
        line_role: (["deposit", "balance", "full", "cancellation_fee", "deposit", "adjustment"] as const)[i],
        product: null,
        plan_key: null,
        source_hold_id: i < 5 ? abId("c", i) : null,
        trip_case_code: i < 5 ? `TC-${pad(i + 1)}` : null,
        description: i < 5 ? `Invented Highlands and Hidden Lagoons Discovery Journey ${i + 1}, departure 14 Jan 2027, 4 travellers` : "Goodwill credit agreed with the agency",
        quantity: "1",
        unit_price: i < 5 ? "256900000" : "-1000000",
        amount: i < 5 ? "256900000" : "-1000000",
        voided: i === 4,
      }))
    : [
        { role: "subscription", product: "content", plan: "content.starter", text: "Content studio, starter plan, October 2026", qty: "1", unit: "299.00" },
        { role: "extra_seats", product: "content", plan: "content.starter", text: "Extra staff seats above the plan, October 2026", qty: "7", unit: "125.13" },
        { role: "adjustment", product: null, plan: null, text: "First-month onboarding discount agreed in the invented contract", qty: "1", unit: "-1.00" },
      ].map((l, i) => ({
        line_no: i + 1,
        line_role: l.role,
        product: l.product,
        plan_key: l.plan,
        source_hold_id: null,
        trip_case_code: null,
        description: l.text,
        quantity: l.qty,
        unit_price: l.unit,
        amount: (Number(l.qty) * Number(l.unit)).toFixed(2),
        voided: false,
      }));
  const pay = (i: number, status: AgencyPaymentStatus, by: string): AgencyPayment => ({
    ...agencyPayment(i),
    invoice_id: base.id,
    invoice_number: ref,
    invoice_kind: base.invoice_kind,
    agency_name: base.agency_name,
    currency,
    amount: settlement ? "385350000" : "299.00",
    status,
    recorded_by: by,
    approved_by: status === "approved" ? "u-003" : null,
    rejected_by: status === "rejected" ? "u-003" : null,
    reject_reason: status === "rejected" ? "Not on the bank statement of the received date" : null,
  });
  return {
    ...base,
    number: ref,
    status: "issued",
    overdue_days: 9,
    due_date: "2026-10-14",
    e_invoice_no: null,
    lines,
    payments: [pay(0, "approved", "u-002"), pay(1, "recorded", "u-002"), pay(2, "rejected", "u-004")],
    reminders: [
      { step_days: 1, step_kind: "reminder", days_overdue: 1, outcome: "sent", skip_reason: null, sent_to: "staff.inbox@example.test", redirected: true, created_at: NOW },
      { step_days: 7, step_kind: "reminder", days_overdue: 7, outcome: "skipped", skip_reason: "no_billing_email", sent_to: null, redirected: false, created_at: NOW },
    ],
  };
}

const AB_SUMMARY: AgencyBillingSummary = {
  by_currency: [
    { currency: "USD", drafts: 4, open: "18450.00", overdue: "3897.00", waiting: "500.00" },
    { currency: "VND", drafts: 2, open: "8991500000", overdue: "1284500000", waiting: "120000000" },
  ],
};
const AB_NOTHING_OVERDUE: Record<string, string> = {};
const AB_STATUS_ROWS: BillingStatusRow[] = AB_AGENCIES.map((a, i): BillingStatusRow => ({
  tenant_id: a.tenant_id,
  agency_name: a.agency_name,
  partner_status: a.health === "past_due" ? "suspended" : "active",
  status: a.health,
  oldest_overdue_days: a.health === "ok" ? 0 : a.health === "warn" ? 3 : 21 + i,
  overdue_invoices: a.health === "ok" ? 0 : a.health === "warn" ? 1 : 3,
  // The API sends a JSON object; one row keeps the string form the view may send (overdueText reads both).
  overdue_amount: a.health === "ok" ? AB_NOTHING_OVERDUE : i === 5 ? JSON.stringify({ USD: "1299.00", VND: "1284500000" }) : { USD: "3897.00", VND: "2569000000" },
  paid_through: i % 3 === 2 ? null : "2026-10-31",
  warn_after_days: i === 1 ? 1 : 7,
  suspend_after_days: i === 1 ? 2 : 21,
}));
const AB_RUNS: DraftRun[] = range(3).map((i) => ({
  id: abId("d", i),
  kind: i % 2 ? "bk_settlement_draft" : "bk_saas_invoice_draft",
  status: "succeeded",
  payload: { period: i % 2 ? "2026-09" : "2026-10", trigger: "schedule" },
  result: { agencies: 6, drafted: 4, failed: 0 },
  error: null,
  created_by: "system:schedule",
  created_at: EARLIER,
  finished_at: EARLIER,
}));
const AB_ENTITIES: BillingEntity[] = [
  {
    id: abId("e", 0),
    code: "DEMO-ISSUER",
    legal_entity: AB_ENTITY_NAME,
    tax_code: "DEMO-TAX-0001",
    registered_office: "12 Invented Riverside Street, Example Ward, Example City",
    country_iso2: "VN",
    invoice_inbox: "invoices@example.test",
    payment_instructions: AB_PAYMENT_LINE,
    is_default: true,
    active: true,
  },
  {
    id: abId("e", 1),
    code: "DEMO-ISSUER-TWO",
    legal_entity: "Second Example Issuer Regional Services Limited",
    tax_code: null,
    registered_office: null,
    country_iso2: "TH",
    invoice_inbox: null,
    payment_instructions: null,
    is_default: false,
    active: true,
  },
  {
    id: abId("e", 2),
    code: "DEMO-OLD",
    legal_entity: "Former Example Issuer Company",
    tax_code: null,
    registered_office: null,
    country_iso2: "VN",
    invoice_inbox: null,
    payment_instructions: null,
    is_default: false,
    active: false,
  },
];
const AB_TAX_RULES: TaxRule[] = [
  { id: abId("f", 0), code: "VAT10-SAAS-VN", invoice_kind: "saas", recipient_country: "VN", rate: "0.1000", line_wording: "VAT 10% on the software fee (invented wording)", footer_wording: "Invented footer wording for the domestic software fee.", effective_from: "2026-07-01", effective_to: null, active: true },
  { id: abId("f", 1), code: "EXPORT-ZERO", invoice_kind: "saas", recipient_country: null, rate: "0.0000", line_wording: "Service supplied abroad, zero rate (invented wording)", footer_wording: null, effective_from: "2026-07-01", effective_to: null, active: true },
  { id: abId("f", 2), code: "SETTLEMENT-NONE", invoice_kind: "settlement", recipient_country: null, rate: "0.0000", line_wording: "No tax line on the tour settlement", footer_wording: null, effective_from: "2026-07-01", effective_to: "2026-12-31", active: false },
];
const AB_TERMS: BillingTerms[] = [
  { id: abId("1", 0), party_tenant_id: null, agency_name: null, deposit_pct: "0.3000", balance_days_before_departure: 45, saas_due_days: 14, settlement_due_days: 7, warn_after_days: 7, suspend_after_days: 21, saas_currency: "USD" },
  { id: abId("1", 1), party_tenant_id: AB_AGENCIES[1].tenant_id, agency_name: AB_AGENCIES[1].agency_name, deposit_pct: "0.3000", balance_days_before_departure: 30, saas_due_days: 14, settlement_due_days: 7, warn_after_days: 1, suspend_after_days: 2, saas_currency: "USD" },
  { id: abId("1", 2), party_tenant_id: AB_AGENCIES[3].tenant_id, agency_name: AB_AGENCIES[3].agency_name, deposit_pct: "0.2500", balance_days_before_departure: 60, saas_due_days: 30, settlement_due_days: 14, warn_after_days: 14, suspend_after_days: 45, saas_currency: "USD" },
];
const AB_CLAUSES: TermsClause[] = [
  {
    id: abId("2", 0),
    code: "price_error",
    position: 10,
    title: "Clearly wrong prices",
    body: "When a price shown to the agency is clearly wrong (for example a missing digit), AA tells the agency within two working days and offers the corrected price.\nThe agency may then cancel the booking free of charge within the window below.",
    free_cancel_days: 3,
    status: "approved",
    version: 2,
    updated_at: EARLIER,
    updated_by: "u-002",
    approved_by: "u-layout",
    approved_at: NOW,
  },
  {
    id: abId("2", 1),
    code: "late_payment",
    position: 20,
    title: "Late payment and new holds",
    body: "Past the suspend days of the agency's payment terms, new booking holds are paused until the overdue invoices are paid and approved.",
    free_cancel_days: null,
    status: "draft",
    version: 1,
    updated_at: NOW,
    updated_by: "u-002",
    approved_by: null,
    approved_at: null,
  },
];

export type FixtureAnswer = { status: number; body: unknown };

const ok = (body: unknown): FixtureAnswer => ({ status: 200, body });

/**
 * The answer for one BFF request (`path` = the part after `/api/booking/`, without the query string), or null when
 * no fixture covers it — the spec then fails the test, so a page that starts calling a new endpoint is noticed.
 */
export function answer(method: string, path: string, query: URLSearchParams): FixtureAnswer | null {
  if (method !== "GET") return null;
  const seg = path.split("/").filter(Boolean);
  const at = (i: number) => seg[i];
  const p = seg.join("/");

  if (p === "staff/me") return ok(STAFF_ME);
  if (p === "staff/me/preferences") return ok({ notify: {} });
  if (p === "staff/directory") return ok({ items: range(8).map((i) => ({ cis_user_id: `u-${pad(i, 3)}`, name: staffProfile(i).name, role: ROLES[i % 4] })) });
  if (p === "staff") return ok({ items: range(query.get("active") === "false" ? 3 : 18).map(staffProfile) });
  if (at(0) === "staff" && seg.length === 2) return ok(staffDetail(at(1)));

  if (p === "notifications") return ok(NOTIFICATIONS);
  if (p === "countries") return ok({ items: [{ iso2: "VN", name: "Vietnam" }, { iso2: "KH", name: "Cambodia" }, { iso2: "LA", name: "Laos" }, { iso2: "TH", name: "Thailand" }] });
  if (p === "audit-logs") return ok(auditLogs());

  if (p === "trip-cases/summary") return ok(TRIP_CASE_SUMMARY);
  if (p === "trip-cases") {
    const limit = Number(query.get("limit") || ROWS);
    return ok(page(range(Math.min(limit, ROWS)).map(tripCase), 214));
  }
  if (at(0) === "trip-cases" && seg.length === 2) return ok(tripCaseDetail(at(1)));
  if (at(0) === "trip-cases" && at(2) === "activity") return ok({ items: [], next_before: null });

  if (p === "versions") return ok(page(range(ROWS).map(versionListItem), 96));


  if (p === "suppliers") return ok(page(range(ROWS).map(supplier), 132) satisfies SupplierList);
  if (p === "suppliers/summary") return ok({ total: 132, active: 118, inactive: 14, countries: 6, contracts_ending_30d: 4 } satisfies SupplierSummary);
  if (at(0) === "suppliers" && at(2) === "contracts") return ok({ items: CONTRACTS });
  if (at(0) === "suppliers" && seg.length === 2) {
    const s: SupplierDetail = { ...supplier(1), code: at(1), contract_counts: { total: 5, drafts: 1, in_force: 1 }, recent_activity: ACTIVITY };
    return ok(s);
  }
  if (at(0) === "contracts" && at(2) === "rates") return ok({ items: [...range(8).map((i) => rate(i, false)), ...range(4).map((i) => rate(i, true))] });

  // AA-835: supplier photos (no preview URL: no network in the check) and the CIS content panel, long text.
  if ((at(0) === "hotels" || at(0) === "services") && at(2) === "media") {
    return ok({ items: range(3).map((i) => supplyMedia(i, at(0) === "hotels" ? "hotel" : "service")), max_bytes: 3145728, max_active: 40 } satisfies MediaList);
  }
  if (p === "hotels") return ok(page(range(ROWS).map(hotel), 86));
  if (at(0) === "hotels" && at(2) === "rates") return ok({ items: range(10).map((i) => rate(i, false)) });
  if (at(0) === "hotels" && seg.length === 2) {
    const h: HotelDetail = { ...hotel(0), room_types: range(4).map(roomType), recent_activity: ACTIVITY, content: CIS_CONTENT };
    return ok(h);
  }

  if (p === "services") return ok(page(range(ROWS).map(service), 203));
  if (at(0) === "services" && at(2) === "rates") return ok({ items: range(8).map((i) => rate(i, true)) });
  if (at(0) === "services" && seg.length === 2) {
    const s: ServiceDetail = { ...service(0), recent_activity: ACTIVITY, content: { ...CIS_CONTENT, content_status: "stale", description: null, highlights: [] } };
    return ok(s);
  }

  if (p === "pricing/rules") return ok(RULES);
  if (p === "pricing/fx-rates") return ok(FX);
  if (p === "pricing/partner-tiers") return ok(AGENCY_TIERS);
  if (p === "pricing/partner-tier-discounts") return ok(TIER_DISCOUNTS);
  if (p === "pricing/partner-terms") return ok({ today: "2026-10-23", items: range(ROWS).map(partnerTerms) });
  if (p === "pricing/partner-rate-cards") return ok(RATE_CARDS);
  if (p.startsWith("pricing/partner-tariffs/")) return ok(TARIFF);
  if (p === "catalog/price-readiness") return ok(READINESS);

  if (p === "b2b-desk/summary") return ok(DESK_SUMMARY);
  if (p === "b2b-desk/on-request") {
    const state = (query.get("state") || "open") as OnRequestPage["state"];
    return ok({ ...page(range(ROWS).map(onRequestLine), 63), state } satisfies OnRequestPage);
  }
  if (p === "b2b-desk/departures") return ok(deskDepartures());
  if (at(0) === "b2b-desk" && at(1) === "departures" && at(3) === "incidents") return ok({ items: [] });

  if (p === "catalog/geo/destinations") return ok(destinationPage());
  if (at(0) === "catalog" && at(1) === "geo" && at(2) === "destinations" && seg.length === 4) return ok(destinationDetail(at(3)));
  if (p === "catalog/geo/countries") return ok(COUNTRY_LIST);
  if (at(0) === "catalog" && at(1) === "geo" && at(2) === "countries" && seg.length === 4) return ok(countryDetail(at(3)));

  if (p === "trip-checks") return ok({ items: range(ROWS).map(tripCheck), total: 73, limit: ROWS, offset: 0 } satisfies TripCheckPage);
  if (p === "case-lifecycle/runs") return ok({ items: LIFECYCLE_RUNS, scheduled_mode: "live", schedule_utc_hour: 1 });
  if (p === "supplier-bookings/summary") return ok({ requested: 6, overdue: 2, pending: 3, answer_days: 3 } satisfies SupplierBookingSummary);
  if (p === "supplier-bills/summary") return ok(SUPPLIER_BILLS_SUMMARY);
  if (p === "supplier-bills") return ok(billingPage(range(ROWS).map(supplierBill), 24));

  // AA-1037: Agency billing (the four tabs) and an agency invoice; `tenant_id` filters like the API (the CIS deep link).
  if (p === "agency-invoices/summary") return ok(AB_SUMMARY);
  if (p === "agency-invoices") {
    const tenant = query.get("tenant_id");
    const rows = range(ROWS * 3).map(agencyInvoice).filter((r) => !tenant || r.party_tenant_id === tenant);
    return ok({ items: rows.slice(0, ROWS), total: tenant ? rows.length : 47 } satisfies AgencyBillingPage<AgencyInvoice>);
  }
  if (at(0) === "agency-invoices" && seg.length === 2) return ok(agencyInvoiceDetail(at(1)));
  if (p === "agency-payments") {
    const state = query.get("state") || "recorded";
    const rows = range(ROWS * 3).map(agencyPayment).filter((r) => state === "all" || r.status === state);
    return ok({ items: rows.slice(0, ROWS), total: state === "all" ? 38 : 13 } satisfies AgencyBillingPage<AgencyPayment>);
  }
  if (p === "agency-billing/status") return ok({ items: AB_STATUS_ROWS });
  if (p === "agency-billing/runs") return ok({ items: AB_RUNS });
  if (p === "billing-entities") return ok({ items: AB_ENTITIES });
  if (p === "tax-rules") return ok({ items: AB_TAX_RULES });
  if (p === "billing-terms") return ok({ items: AB_TERMS });
  if (p === "partner-terms-clauses") return ok({ items: AB_CLAUSES });

  if (p === "catalog/trip-plans/facets") return ok(TRIP_PLAN_FACETS);
  if (at(0) === "catalog" && at(1) === "trip-plans" && at(3) === "prices") return ok({ ...PRICE_MATRIX, source_id: at(2) });
  if (at(0) === "catalog" && at(1) === "trip-plans" && seg.length === 3) return ok(tripPlanDetail(at(2)));
  if (p === "catalog/trip-plans") {
    if (query.get("orphaned") === "true") return ok(page(range(9).map(orphanedPlan), 9));
    return ok(page(range(ROWS).map(tripPlan), 412));
  }

  return null;
}
