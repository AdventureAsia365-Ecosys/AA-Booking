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
  CustomerDetail,
  CustomerSummary,
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
import type { Hotel, HotelDetail, Page, RoomType, Service, ServiceDetail, SupplierRate } from "../../lib/api/supply";
import type { FxRateRow, PolicyList, PricingRuleRow } from "../../lib/api/pricingPolicy";
import type { OrphanedPlanItem, TripPlanFacets, TripPlanListItem } from "../../lib/api/tripPlans";
import type { DeskDepartures, DeskSummary, OnRequestLine, OnRequestPage } from "../../lib/api/b2bDesk";
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
      customer_name: `Alexandra Catherine Montgomery-Whitfield ${i}`,
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
    customer_id: uuid("d", i),
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
    customer_code: `CUS-${pad(i + 1)}`,
    customer_name: `Alexandra Catherine Montgomery-Whitfield ${i}`,
    customer_email: `alexandra.montgomery.whitfield.${i}@very-long-customer-domain.example.test`,
    customer_phone: "+44 20 7946 0958 ext 1234",
    customer_verified: i % 2 === 0,
    customer_needs_merge_review: i % 9 === 0,
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
    start_date: "2027-01-14",
    end_date: "2027-02-02",
    sales_id: "u-001",
    customer: { id: uuid("d", i), name: `Alexandra Catherine Montgomery-Whitfield ${i}` },
    updated_by: { id: "u-001", name: PERSON_NAMES[3] },
    leg_titles: tripCase(i).leg_titles,
    trip_title: tripCase(i).trip_title,
    duration_days: 20,
  };
}

// ── Customers ────────────────────────────────────────────────────────────────
const CUSTOMER_STATUSES = ["lead", "active", "past", "blocked"] as const;
function customer(i: number): CustomerSummary {
  return {
    id: uuid("d", i),
    code: `CUS-${pad(i + 1)}`,
    name: `Alexandra Catherine Montgomery-Whitfield ${i}`,
    email: `alexandra.montgomery.whitfield.${i}@very-long-customer-domain.example.test`,
    phone: i % 5 === 4 ? null : "+44 20 7946 0958 ext 1234",
    address: "Flat 12, 221B Long Riverside Embankment Road, Kensington and Chelsea, London SW3 5XX",
    verified: i % 2 === 0,
    is_blocked: i % 11 === 10,
    // Rows 10 and 21 carry three badges in the status cell: their status, Blocked and Merge review.
    needs_merge_review: i % 9 === 0 || i % 11 === 10,
    merge_candidate_id: i % 9 === 0 ? uuid("d", i + 100) : null,
    source: (["manual", "otp", "tripplanner_lead"] as const)[i % 3],
    contact_consent: i % 2 === 1,
    contact_consent_at: i % 2 === 1 ? EARLIER : null,
    case_count: 7,
    open_case_count: 3,
    last_activity_at: NOW,
    status: CUSTOMER_STATUSES[i % 4],
    created_at: EARLIER,
    updated_at: NOW,
  };
}

function customerDetail(ref: string): CustomerDetail {
  return {
    ...customer(0),
    code: ref,
    merge_candidate: { id: uuid("d", 100), code: "CUS-000101", name: "Alexandra C. Montgomery Whitfield", verified: false, archived: false },
    cases: range(12).map(tripCase),
    inquiries: range(6).map((i) => {
      const c = tripCase(i);
      return { id: c.id, code: c.code, status: c.status, created_at: c.created_at, trip_title: c.trip_title, leg_titles: c.leg_titles, duration_days: c.duration_days };
    }),
    recent_activity: ACTIVITY,
  };
}

// ── Notifications + audit ────────────────────────────────────────────────────
const NOTIFICATIONS: NotificationFeed = {
  items: range(5).map((i) => ({
    id: `n-${i}`,
    type: "inquiry.received",
    title: "New inquiry from TripPlanner: Grand Indochina Discovery for a family of six",
    message: "Alexandra Catherine Montgomery-Whitfield asked for 20 days from 14 January 2027.",
    entity_type: "trip_case",
    entity_id: uuid("c", i),
    data: {},
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

// ── Supply ───────────────────────────────────────────────────────────────────
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
  };
}

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

// ── Router ───────────────────────────────────────────────────────────────────
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

  if (p === "customers") {
    const limit = Number(query.get("limit") || ROWS);
    return ok(page(range(Math.min(limit, ROWS)).map(customer), 1284));
  }
  if (at(0) === "customers" && seg.length === 2) return ok(customerDetail(at(1)));

  if (p === "suppliers") return ok(page(range(ROWS).map(supplier), 132) satisfies SupplierList);
  if (p === "suppliers/summary") return ok({ total: 132, active: 118, inactive: 14, countries: 6, contracts_ending_30d: 4 } satisfies SupplierSummary);
  if (at(0) === "suppliers" && at(2) === "contracts") return ok({ items: CONTRACTS });
  if (at(0) === "suppliers" && seg.length === 2) {
    const s: SupplierDetail = { ...supplier(1), code: at(1), contract_counts: { total: 5, drafts: 1, in_force: 1 }, recent_activity: ACTIVITY };
    return ok(s);
  }
  if (at(0) === "contracts" && at(2) === "rates") return ok({ items: [...range(8).map((i) => rate(i, false)), ...range(4).map((i) => rate(i, true))] });

  if (p === "hotels") return ok(page(range(ROWS).map(hotel), 86));
  if (at(0) === "hotels" && at(2) === "rates") return ok({ items: range(10).map((i) => rate(i, false)) });
  if (at(0) === "hotels" && seg.length === 2) {
    const h: HotelDetail = { ...hotel(0), room_types: range(4).map(roomType), recent_activity: ACTIVITY };
    return ok(h);
  }

  if (p === "services") return ok(page(range(ROWS).map(service), 203));
  if (at(0) === "services" && at(2) === "rates") return ok({ items: range(8).map((i) => rate(i, true)) });
  if (at(0) === "services" && seg.length === 2) {
    const s: ServiceDetail = { ...service(0), recent_activity: ACTIVITY };
    return ok(s);
  }

  if (p === "pricing/rules") return ok(RULES);
  if (p === "pricing/fx-rates") return ok(FX);

  if (p === "b2b-desk/summary") return ok(DESK_SUMMARY);
  if (p === "b2b-desk/on-request") {
    const state = (query.get("state") || "open") as OnRequestPage["state"];
    return ok({ ...page(range(ROWS).map(onRequestLine), 63), state } satisfies OnRequestPage);
  }
  if (p === "b2b-desk/departures") return ok(deskDepartures());
  if (at(0) === "b2b-desk" && at(1) === "departures" && at(3) === "incidents") return ok({ items: [] });

  if (p === "catalog/trip-plans/facets") return ok(TRIP_PLAN_FACETS);
  if (p === "catalog/trip-plans") {
    if (query.get("orphaned") === "true") return ok(page(range(9).map(orphanedPlan), 9));
    return ok(page(range(ROWS).map(tripPlan), 412));
  }

  return null;
}
