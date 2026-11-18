// ui/booking/caseFacts.ts
// AA-996 (Figma audit slice 4) — pure readers for the case workspace and the trip-check board, kept free of imports so
// Node's test runner can load them (caseFacts.test.mjs). Nothing here formats dates or money: the pages do that with
// the kit helpers.

/** What the inquiry panel shows from `inquiry_snapshot` besides the route (UI spec §4 inquiry panel, Q12). */
export interface InquiryPrefs {
  message: string | null;
  budget: string | null;
  /** ISO days of the travel window, or free text when the snapshot gave text. */
  travelWindow: { start: string | null; end: string | null; text: string | null } | null;
  travelStyle: string | null;
  party: string | null;
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);
const num = (v: unknown): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : typeof v === "string" && v.trim() && Number.isFinite(Number(v)) ? Number(v) : null;

/** The first value found under one of `keys`, looked up in the snapshot itself and in the sub-objects the contract may
 *  group them in (`preferences`, `trip`, `itinerary`, `customer`). Tolerant on purpose: the snapshot is stored as
 *  received, and the v1.2 fields (budget, message, travel window, pax split) are optional (Q12). */
function pick(snapshot: Obj, keys: string[]): unknown {
  const scopes = [snapshot, snapshot.preferences, snapshot.trip, snapshot.itinerary, snapshot.customer].filter(isObj);
  for (const scope of scopes) {
    for (const k of keys) {
      if (scope[k] !== undefined && scope[k] !== null && scope[k] !== "") return scope[k];
    }
  }
  return undefined;
}

function money(amount: number, currency: string | null): string {
  const n = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 }).format(amount);
  return currency ? `${n} ${currency}` : n;
}

function budgetText(raw: unknown): string | null {
  if (typeof raw === "number" || typeof raw === "string") {
    const n = num(raw);
    return n != null ? money(n, null) : text(raw);
  }
  if (!isObj(raw)) return null;
  const currency = text(raw.currency);
  const amount = num(raw.amount ?? raw.total ?? raw.value);
  if (amount != null) return `${money(amount, currency)}${raw.per_person ? " per person" : ""}`;
  const min = num(raw.min);
  const max = num(raw.max);
  if (min != null && max != null) return `${money(min, null)} – ${money(max, currency)}`;
  if (min != null) return `from ${money(min, currency)}`;
  if (max != null) return `up to ${money(max, currency)}`;
  return text(raw.label) ?? text(raw.text);
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}/;

function windowOf(snapshot: Obj): InquiryPrefs["travelWindow"] {
  const raw = pick(snapshot, ["travel_window", "travel_dates", "dates"]);
  const day = (v: unknown) => (typeof v === "string" && ISO_DAY.test(v) ? v.slice(0, 10) : null);
  if (isObj(raw)) {
    const start = day(raw.start ?? raw.from ?? raw.start_date ?? raw.earliest);
    const end = day(raw.end ?? raw.to ?? raw.end_date ?? raw.latest);
    if (start || end) return { start, end, text: null };
    const t = text(raw.text) ?? text(raw.label);
    return t ? { start: null, end: null, text: t } : null;
  }
  if (typeof raw === "string") return day(raw) ? { start: day(raw), end: null, text: null } : { start: null, end: null, text: raw.trim() };
  const start = day(pick(snapshot, ["start_date", "travel_start"]));
  const end = day(pick(snapshot, ["end_date", "travel_end"]));
  return start || end ? { start, end, text: null } : null;
}

function partyText(snapshot: Obj): string | null {
  const raw = pick(snapshot, ["pax", "party", "travellers", "travelers"]);
  const adults = num(isObj(raw) ? raw.adults : pick(snapshot, ["adults", "pax_adults"]));
  const children = num(isObj(raw) ? raw.children : pick(snapshot, ["children", "pax_children"]));
  if (adults != null) {
    const kids = children ?? 0;
    return `${adults} adult${adults === 1 ? "" : "s"}${kids ? `, ${kids} child${kids === 1 ? "" : "ren"}` : ""}`;
  }
  const total = num(raw);
  return total != null ? `${total} traveller${total === 1 ? "" : "s"}` : null;
}

/** The customer's own words and preferences from the inquiry, or null when the snapshot carries none of them. */
export function inquiryPrefs(snapshot: Record<string, unknown> | null | undefined): InquiryPrefs | null {
  if (!isObj(snapshot)) return null;
  const out: InquiryPrefs = {
    message: text(pick(snapshot, ["message", "notes", "comment", "customer_message"])),
    budget: budgetText(pick(snapshot, ["budget", "estimated_budget"])),
    travelWindow: windowOf(snapshot),
    travelStyle: text(pick(snapshot, ["travel_style", "trip_style", "style"])),
    party: partyText(snapshot),
  };
  return out.message || out.budget || out.travelWindow || out.travelStyle || out.party ? out : null;
}

/** The countries the case page shows: what staff set, else the ones derived from the legs (AA-996). */
export function caseCountries(c: { countries: string[]; leg_countries?: string[] | null }): { codes: string[]; fromLegs: boolean } {
  if (c.countries.length) return { codes: c.countries, fromLegs: false };
  const legs = c.leg_countries ?? [];
  return { codes: legs, fromLegs: legs.length > 0 };
}

/** "Days" between two ISO days, both ends included (a 14-night trip from the 12th to the 26th is 15 days). */
export function inclusiveDays(start: string | null, end: string | null): number | null {
  if (!start || !end || !ISO_DAY.test(start) || !ISO_DAY.test(end)) return null;
  const a = Date.UTC(+start.slice(0, 4), +start.slice(5, 7) - 1, +start.slice(8, 10));
  const b = Date.UTC(+end.slice(0, 4), +end.slice(5, 7) - 1, +end.slice(8, 10));
  const days = Math.round((b - a) / 86_400_000) + 1;
  return days >= 1 ? days : null;
}

/** How far a trip is from ready: done and not-applicable checks count as resolved (the board's open = the rest; AA-1128:
 *  the label says "resolved", since a not-applicable check is not done). */
export function checkProgress(checks: { total: number; done: number; not_applicable: number } | null | undefined): {
  resolved: number;
  total: number;
  pct: number;
  label: string;
} | null {
  if (!checks || checks.total <= 0) return null;
  const resolved = Math.min(checks.total, checks.done + checks.not_applicable);
  const pct = Math.round((resolved / checks.total) * 100);
  return { resolved, total: checks.total, pct, label: `${resolved} of ${checks.total} checks resolved` };
}

/** /trip-checks status tabs (AA-996 review fix): may the tab badges show the API's counts? The counts apply the
 *  owner / check / search filters but never the due filter (GET /booking/trip-checks `counts`). A badge is only shown
 *  when it equals what the list under that tab shows:
 *  - no due filter: yes;
 *  - the "overdue" filter on the Overdue tab: yes (Overdue = open and due before today, and leaving the tab clears
 *    that filter, so every other badge is what its tab will show);
 *  - any other due window (7 / 14 / 30 days, or "overdue" picked on another tab): no, the rows are a subset of the
 *    badge's number. */
export function tabCountsShown(tab: string, due: string): boolean {
  if (!due) return true;
  return due === "overdue" && tab === "overdue";
}

/** AA-1046: who a case is for, in one line. The B2C half is retired, so a b2c case names no traveller (no customer
 *  record is kept); a B2B case is an agency booking. */
export function casePartyLabel(c: { channel: "b2c" | "b2b" | string; party_agency_name?: string | null }): string {
  if (c.channel !== "b2b") return "Traveller (B2C history)";
  return c.party_agency_name?.trim() || "B2B agency";
}

/** AA-1128 B3: the line under a case's party: never the raw tenant UUID. A B2B case reads "B2B agency booking" when
 *  the agency has a name, "Agency not in Booking yet" when the API sent none; a B2C case is read-only history. */
export function casePartySub(c: { channel: "b2c" | "b2b" | string; party_tenant_id?: string | null; party_agency_name?: string | null }): string {
  if (c.channel !== "b2b") return "read-only";
  if (!c.party_tenant_id) return "No agency on this case";
  return c.party_agency_name?.trim() ? "B2B agency booking" : "Agency not in Booking yet";
}

/** AA-1128 B4: the wording of a locked version's price. A B2B version is approved by the agency's hold confirm and
 *  never sent, so its price is "saved on approval" and the agency confirmed it; a sent B2C version keeps its words. */
export function lockedPriceText(channel: string, sentAt: string | null | undefined, approved: boolean): { note: string; footnote: string } {
  const b2b = channel === "b2b";
  const note = b2b || !sentAt
    ? "The price saved on this version when it was approved. It no longer changes."
    : "The price saved on this version when it was sent. It no longer changes.";
  if (!approved) return { note, footnote: "This is the price the customer received. To change it, create a new version from the case." };
  const footnote = b2b
    ? "The agency confirmed this price with its booking. Agency invoices are made from it."
    : "The customer approved this price. Invoices are made from it on the Billing tab.";
  return { note, footnote };
}

/** Who approved a locked version, for the preview banner. */
export function approvedByText(channel: string): string {
  return channel === "b2b" ? "Approved by the agency" : "Approved by the customer";
}
