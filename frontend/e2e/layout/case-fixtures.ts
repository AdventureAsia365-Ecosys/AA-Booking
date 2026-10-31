// e2e/layout/case-fixtures.ts
// AA-996 (Figma audit slice 4) — fixture answers for the case workspace / proposals / trip checks check
// (case-workspace.spec.ts). Kept apart from fixtures.ts so the slices of the audit do not edit the same file: the spec
// asks caseAnswer() first and falls back to the shared answer() for everything else (staff, countries, directory).
//
//   TC-000101  a TripPlanner custom trip in proposal_sent: no legs, countries derived from the legs API field, the
//              inquiry carries a message / budget / travel window; V1 superseded, V2 approved (locked price, items),
//              V3 draft with items and net costs (live quote → 403 e2e_read_only, as on the audit's E2E session).
//              V4 (items only, not in the case's version list; AA-1013): a draft copied day by day from a CIS tour
//              that CIS has since rewritten (two stale day titles) plus a multi-day item of a second tour.
//   TC-000102  a new inquiry (Convert / Decline dialogs).
//   /trip-checks  25 rows over a few trips with progress, tab counts, sales owners.
//
// Nothing here is real: names, codes and e-mails are invented (example.test domains).

import type { CaseVersion, TripCaseDetail, TripCaseStatus, VersionItem, VersionWithItems } from "../../lib/api/types";
import type { TripCheck, TripCheckPage } from "../../lib/api/tripChecks";
import type { FixtureAnswer } from "./fixtures";

const NOW = "2026-09-28T08:15:00+00:00";
const EARLIER = "2026-09-20T03:40:00+00:00";
const pad = (n: number, w = 6) => String(n).padStart(w, "0");
const range = (n: number) => Array.from({ length: n }, (_, i) => i);
const ok = (body: unknown): FixtureAnswer => ({ status: 200, body });

export const CUSTOM_CASE = "TC-000101";
export const INQUIRY_CASE = "TC-000102";

function version(n: number, badge: CaseVersion["badge"], items: number): CaseVersion {
  const sent = badge !== "draft";
  return {
    id: `eeee0000-0000-4000-8000-${pad(n, 12)}`,
    version_number: n,
    title: n === 3 ? "Slower pace in Luang Prabang with a cooking class and a private boat to the Pak Ou caves" : `Laos river and caves V${n}`,
    notes: null,
    total_price: badge === "draft" ? null : "4385.00",
    currency: "EUR",
    channel: "b2c_tailor_made",
    valid_until: sent ? "2026-10-12" : null,
    fx_locked: sent ? { rate: "0.920000", as_of: EARLIER } : null,
    is_sent_to_customer: sent,
    sent_at: sent ? EARLIER : null,
    sent_by: sent ? "u-001" : null,
    is_approved: badge === "approved",
    approved_at: badge === "approved" ? NOW : null,
    approved_by: badge === "approved" ? "u-003" : null,
    approval_note: null,
    created_by: "u-001",
    created_at: EARLIER,
    updated_by: "u-001",
    updated_at: NOW,
    badge,
    item_count: items,
  };
}

const VERSIONS: CaseVersion[] = [version(1, "superseded", 3), version(2, "approved", 3), version(3, "draft", 4)];

function caseDetail(code: string, status: TripCaseStatus): TripCaseDetail {
  const inquiry = status === "new_inquiry";
  return {
    id: `cccc0000-0000-4000-8000-${code.slice(3)}`,
    code,
    status,
    customer_id: "dddd0000-0000-4000-8000-000000000101",
    channel: "b2c",
    party_tenant_id: null,
    trip_style: inquiry ? null : "Slow travel, boutique stays",
    start_date: inquiry ? null : "2026-11-12",
    end_date: inquiry ? null : "2026-11-26",
    pax: 3,
    pax_adults: 2,
    pax_children: null,
    currency: "EUR",
    notes: null,
    source_system: "tripplanner",
    source_id: `trip-${code}`,
    created_at: EARLIER,
    updated_at: NOW,
    customer_code: "CUS-000101",
    customer_name: "Alexandra Catherine Montgomery-Whitfield",
    customer_email: "alexandra.montgomery.whitfield@very-long-customer-domain.example.test",
    customer_phone: "+44 20 7946 0958",
    customer_verified: false,
    customer_needs_merge_review: false,
    assignees: inquiry ? [] : [{ cis_user_id: "u-001", role: "sales", name: "Pham Quoc Bao-Long Sales Lead", assigned_at: EARLIER }],
    countries: [],
    leg_countries: ["LA", "VN"],
    trip_title: null,
    leg_titles: [],
    leg_count: 0,
    duration_days: null,
    trip_end_date: null,
    inquiry_snapshot: {
      itinerary: { legs: [], days: [{ day: 1, name: "Pak Ou Caves and the Mekong at dawn" }], custom_days: [{ kind: "rest_day" }] },
      message:
        "We would like a slow trip with time to cook with local families. One of us has a bad knee, so please avoid long stair climbs and early starts.",
      budget: { min: 4000, max: 5500, currency: "EUR" },
      travel_window: { start: "2026-11-10", end: "2026-11-30" },
      travel_style: "Slow travel, boutique stays",
      pax: { adults: 2, children: 1 },
    },
    inbox_event_id: null,
    legs: [],
    versions: inquiry ? [] : VERSIONS,
    status_history: [{ from_status: null, to_status: "new_inquiry", changed_by: null, note: null, changed_at: EARLIER }],
    assignments: [],
    allowed_transitions: inquiry ? ["discovery", "cancelled"] : ["cancelled"],
    tour_alerts: [],
    supply: undefined,
    money: null,
  };
}

function items(n: number, count: number): VersionItem[] {
  return range(count).map((i) => ({
    id: `aaaa${n}000-0000-4000-8000-${pad(i, 12)}`,
    position: i + 1,
    title: ["Luang Prabang boutique riverside hotel", "Pak Ou caves private longtail boat", "Cooking class with a local family", "Kuang Si falls and bear rescue"][i % 4],
    day_from: 1 + i * 2,
    day_to: 2 + i * 2,
    source_tour_id: null,
    notes: i === 0 ? "Ground floor room (knee)." : null,
    costs: [{ id: `bbbb${n}000-0000-4000-8000-${pad(i, 12)}`, net_amount: String(400 + i * 125), net_currency: "USD", supplier_note: null }],
    hotel_room_type_id: null,
    service_id: null,
    supply: null,
  }));
}

// AA-1013: CIS rewrote "Lantern Ridge" after V4 was made (days 1 and 3); day 2 is unchanged.
export const READBACK_VERSION = 4;
const LANTERN = { source_id: "LR-5D", tour_title: "Lantern Ridge Trek and the Seven Springs Monastery — 5 Days", tour_updated_at: NOW };
const READBACK_ITEMS: [string, number, number, VersionItem["cis"]][] = [
  [
    "Arrival at Lantern Ridge and transfer to the lakeside lodge above the old salt road",
    1,
    1,
    { ...LANTERN, day_number: 1, title: "Lantern Ridge arrival and the lakeside lodge transfer", changed: true },
  ],
  ["Upper lake walk", 2, 2, { ...LANTERN, day_number: 2, title: "Upper lake walk", changed: false }],
  [
    "Ridge crossing to the monastery of the seven springs above the cloud line",
    3,
    3,
    { ...LANTERN, day_number: 3, title: "Seven Springs monastery ridge crossing", changed: true },
  ],
  [
    "Hotel room · line 4",
    4,
    6,
    { source_id: "SB-3D", tour_title: "Salt Bay Escape — 3 Days", tour_updated_at: EARLIER, day_number: null, title: null, changed: false },
  ],
];

function readbackVersion(): VersionWithItems {
  const v: Partial<CaseVersion> = version(READBACK_VERSION, "draft", READBACK_ITEMS.length);
  delete v.item_count;
  return {
    ...(v as Omit<CaseVersion, "item_count">),
    title: "Lantern Ridge Trek and the Seven Springs Monastery — 5 Days",
    items: READBACK_ITEMS.map(([title, day_from, day_to, cis], i) => ({
      id: `aaaa4000-0000-4000-8000-${pad(i, 12)}`,
      position: i + 1,
      title,
      day_from,
      day_to,
      source_tour_id: cis?.source_id ?? null,
      notes: null,
      costs: [],
      hotel_room_type_id: null,
      service_id: null,
      supply: null,
      cis,
    })),
  };
}

function versionWithItems(n: number): VersionWithItems | null {
  if (n === READBACK_VERSION) return readbackVersion();
  const v = VERSIONS.find((x) => x.version_number === n);
  if (!v) return null;
  const { item_count, ...rest } = v;
  return { ...rest, items: items(n, item_count) };
}

const OWNERS = [null, { id: "u-002", name: "Tran Van Contracting-Officer" }, { id: "u-001", name: "Pham Quoc Bao-Long Sales Lead" }];
const CHECK_CODES = ["pre_trip_recheck", "emergency_contact", "insurance", "custom"] as const;

function tripCheck(i: number): TripCheck {
  const trip = i % 6;
  const code = CHECK_CODES[i % 4];
  const status = i % 5 === 4 ? "done" : "open";
  return {
    id: `9999${pad(trip, 4)}-0000-4000-8000-${pad(i, 12)}`,
    code,
    title: code === "custom" ? "Confirm the vegetarian menu with the Mekong cruise and the step-free cabin" : code,
    status,
    due_date: i % 3 === 0 ? "2026-10-18" : "2026-11-02",
    assignee_ref: OWNERS[i % 3]?.id ?? null,
    assignee: OWNERS[i % 3],
    done_by: null,
    done_at: null,
    note: null,
    emergency_contact_name: null,
    emergency_contact_phone: null,
    cleared_at: null,
    created_by: "system",
    created_at: EARLIER,
    updated_at: NOW,
    standard: code !== "custom",
    contact_allowed: code === "emergency_contact" && trip !== 3,
    trip_case: {
      id: `cccc${pad(trip, 4)}-0000-4000-8000-000000000000`,
      code: `TC-${pad(200 + trip)}`,
      status: ["confirmed", "supplier_confirming", "invoice_sent", "paid", "pre_departure", "on_trip"][trip],
      channel: trip === 3 ? "b2b" : "b2c",
      start_date: "2026-11-12",
      end_date: "2026-11-26",
      customer_name: trip === 3 ? null : `Alexandra Catherine Montgomery-Whitfield ${trip}`,
      checks: { total: 3 + (trip % 4), done: trip % 3, not_applicable: trip === 3 ? 1 : 0 },
      sales_name: trip % 2 ? "Pham Quoc Bao-Long Sales Lead" : null,
    },
  };
}

const CHECK_PAGE: TripCheckPage = {
  items: range(25).map(tripCheck),
  total: 42,
  limit: 25,
  offset: 0,
  counts: { open: 42, overdue: 18, done: 102, not_applicable: 7, all: 151 },
};

/** The answer for one BFF request, or null to fall back to the shared fixtures. `quote` decides the live quote: the
 *  audit's read-only E2E session (403 e2e_read_only) by default. */
export function caseAnswer(method: string, path: string): FixtureAnswer | null {
  const seg = path.split("/").filter(Boolean);
  const p = seg.join("/");
  if (method === "POST" && p === "pricing/quote-preview") {
    return { status: 403, body: { detail: "e2e_read_only", message: "e2e_read_only" } };
  }
  if (method !== "GET") return null;
  if (p === `trip-cases/${CUSTOM_CASE}`) return ok(caseDetail(CUSTOM_CASE, "proposal_sent"));
  if (p === `trip-cases/${INQUIRY_CASE}`) return ok(caseDetail(INQUIRY_CASE, "new_inquiry"));
  if (seg[0] === "trip-cases" && seg[1] === CUSTOM_CASE && seg[2] === "versions" && seg[4] === "items") {
    const v = versionWithItems(Number(seg[3]));
    return v ? ok(v) : { status: 404, body: { detail: "version_not_found" } };
  }
  if (p === "trip-checks") return ok(CHECK_PAGE);
  if (p === "case-lifecycle/runs") return ok({ items: [], scheduled_mode: "live", schedule_utc_hour: 1 });
  return null;
}
