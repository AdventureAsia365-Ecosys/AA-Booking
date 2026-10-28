// ui/booking/tripPlans.ts
// AA-996 (Figma audit slice 2) — pure helpers of the trip plan pages: the "N days / N-1 nights" duration, the list
// row subtitle (destinations, else SKU, else a short CIS id), pax band labels and the price matrix editor's draft
// rows → PUT …/prices body with the same row rules as the API (app/catalog/trip_plan_prices.py: validity order, pax
// band order, a price or a net cost, a net cost with its currency, no overlap per channel + currency).
//
// Import-free on purpose: tripPlans.test.mjs loads this file with Node's type stripping (`npm run test:unit`).

/** "7 days / 6 nights", "2 days / 1 night", "1 day"; null when the duration is unknown. */
export function daysNights(days: number | null | undefined): string | null {
  if (!days || days < 1 || !Number.isFinite(days)) return null;
  if (days === 1) return "1 day";
  const nights = days - 1;
  return `${days} days / ${nights} ${nights === 1 ? "night" : "nights"}`;
}

/** The second line of a trip plan row: its places, else its SKU, else a short CIS id (never "No SKU"). */
export function planSubtitle(row: { destinations?: string[] | null; sku: string | null; source_id: string }): string {
  const places = (row.destinations ?? []).filter((p) => p && p.trim());
  if (places.length) return places.join(" · ");
  if (row.sku) return row.sku;
  return `CIS ${row.source_id.slice(0, 8)}`;
}

/** "2 pax", "3–4 pax", "11+ pax". */
export function paxBandLabel(min: number, max: number | null): string {
  if (max === null) return `${min}+ pax`;
  if (max === min) return `${min} pax`;
  return `${min}–${max} pax`;
}

/** "28.5 %" from the API's 4-decimal fraction ("0.2850"); null when missing. */
export function marginLabel(fraction: string | null | undefined): string | null {
  if (fraction === null || fraction === undefined || fraction === "") return null;
  const n = Number(fraction);
  if (!Number.isFinite(n)) return null;
  return `${(Math.round(n * 1000) / 10).toFixed(1)} %`;
}

// ── Price matrix editor ─────────────────────────────────────────────────────

export type Channel = "b2c_tailor_made" | "fixed_departure" | "b2b";

/** One editor row, every field as typed (strings), `key` only for React. */
export interface PriceDraft {
  key: string;
  channel: Channel;
  season: string;
  peak: boolean;
  valid_from: string;
  valid_to: string;
  pax_min: string;
  pax_max: string;
  currency: string;
  amount: string;
  net_amount: string;
  net_currency: string;
}

export interface PriceRowBody {
  channel: Channel;
  season: string | null;
  peak: boolean;
  valid_from: string;
  valid_to: string;
  pax_min: number;
  pax_max: number | null;
  currency: string;
  amount: string | null;
  net_amount: string | null;
  net_currency: string | null;
}

export interface DraftError {
  /** 0-based row index. */
  row: number;
  field: keyof PriceDraft | "row";
  message: string;
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const MONEY = /^\d{1,10}(\.\d{1,2})?$/;
const PAX = /^\d{1,3}$/;

let seq = 0;

const DAY_MS = 86_400_000;

function isoDayMs(day: string): number | null {
  if (!ISO_DAY.test(day)) return null;
  const ms = Date.parse(`${day}T00:00:00Z`);
  return Number.isFinite(ms) ? ms : null;
}

function msIsoDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** A new row that does not overlap the last one: the next season after it (same channel, currency, band and window
 *  length, starting the day after it ends); when its dates are not usable yet, the next pax band on the same dates.
 *  Without a row: a fresh 2-pax B2C row for next year. */
export function newDraft(after?: PriceDraft, today?: string): PriceDraft {
  seq += 1;
  const year = Number((today ?? "2026-01-01").slice(0, 4)) + 1;
  if (after) {
    const next: PriceDraft = { ...after, key: `new-${seq}`, season: "", peak: false, amount: "", net_amount: "" };
    const from = isoDayMs(after.valid_from);
    const to = isoDayMs(after.valid_to);
    if (from !== null && to !== null && to >= from) {
      next.valid_from = msIsoDay(to + DAY_MS);
      next.valid_to = msIsoDay(to + DAY_MS + (to - from));
    } else if (PAX.test(after.pax_max.trim()) && Number(after.pax_max) < 999) {
      next.pax_min = String(Number(after.pax_max) + 1);
      next.pax_max = "";
    }
    return next;
  }
  return {
    key: `new-${seq}`,
    channel: "b2c_tailor_made",
    season: "",
    peak: false,
    valid_from: `${year}-01-01`,
    valid_to: `${year}-12-31`,
    pax_min: "2",
    pax_max: "2",
    currency: "USD",
    amount: "",
    net_amount: "",
    net_currency: "USD",
  };
}

/** Editor rows from the API rows (a price computed from a net cost is shown blank: it is computed again on save; an
 *  override is kept). */
export function draftsFrom(
  rows: {
    position: number;
    channel: Channel;
    season: string | null;
    peak: boolean;
    valid_from: string;
    valid_to: string;
    pax_min: number;
    pax_max: number | null;
    currency: string;
    amount: string;
    net_amount: string | null;
    net_currency: string | null;
    priced: boolean;
    overridden?: boolean;
  }[],
): PriceDraft[] {
  return rows.map((r) => ({
    key: `row-${r.position}`,
    channel: r.channel,
    season: r.season ?? "",
    peak: r.peak,
    valid_from: r.valid_from,
    valid_to: r.valid_to,
    pax_min: String(r.pax_min),
    pax_max: r.pax_max === null ? "" : String(r.pax_max),
    currency: r.currency,
    amount: r.priced && !r.overridden ? "" : r.amount,
    net_amount: r.net_amount ?? "",
    net_currency: r.net_currency ?? "USD",
  }));
}

function overlaps(a: PriceRowBody, b: PriceRowBody): boolean {
  if (a.channel !== b.channel || a.currency !== b.currency) return false;
  const dates = a.valid_from <= b.valid_to && b.valid_from <= a.valid_to;
  const pax = a.pax_min <= (b.pax_max ?? 999) && b.pax_min <= (a.pax_max ?? 999);
  return dates && pax;
}

/** Validate the editor rows and build the PUT body; `errors` is empty when the body can be sent. */
export function draftsToBody(drafts: PriceDraft[]): { items: PriceRowBody[]; errors: DraftError[] } {
  const items: PriceRowBody[] = [];
  const errors: DraftError[] = [];
  drafts.forEach((d, row) => {
    const bad = (field: DraftError["field"], message: string) => errors.push({ row, field, message });
    const amount = d.amount.trim();
    const net = d.net_amount.trim();
    if (!ISO_DAY.test(d.valid_from)) bad("valid_from", "Pick the first day.");
    if (!ISO_DAY.test(d.valid_to)) bad("valid_to", "Pick the last day.");
    else if (ISO_DAY.test(d.valid_from) && d.valid_to < d.valid_from) bad("valid_to", "Ends before it starts.");
    const min = PAX.test(d.pax_min.trim()) ? Number(d.pax_min) : NaN;
    const max = d.pax_max.trim() === "" ? null : PAX.test(d.pax_max.trim()) ? Number(d.pax_max) : NaN;
    if (!(min >= 1 && min <= 999)) bad("pax_min", "1 to 999.");
    if (max !== null && !(max >= 1 && max <= 999)) bad("pax_max", "1 to 999, or empty for no top.");
    else if (max !== null && min >= 1 && max < min) bad("pax_max", "Below the minimum.");
    if (amount && (!MONEY.test(amount) || Number(amount) <= 0)) bad("amount", "An amount above 0, up to 2 decimals.");
    if (net && !MONEY.test(net)) bad("net_amount", "An amount, up to 2 decimals.");
    else if (net && !amount && Number(net) <= 0) bad("net_amount", "A net cost above 0, or enter the price.");
    if (!amount && !net) bad("amount", "Enter a price, or a net cost to price it from.");
    if (d.season.trim().length > 80) bad("season", "At most 80 characters.");
    items.push({
      channel: d.channel,
      season: d.season.trim() || null,
      peak: d.peak,
      valid_from: d.valid_from,
      valid_to: d.valid_to,
      pax_min: min,
      pax_max: max,
      currency: d.currency,
      amount: amount || null,
      net_amount: net || null,
      net_currency: net ? d.net_currency : null,
    });
  });
  if (!errors.length) {
    items.forEach((a, i) => {
      for (let j = 0; j < i; j++) {
        if (overlaps(items[j], a)) {
          errors.push({ row: i, field: "row", message: `Overlaps row ${j + 1}: same channel and currency, overlapping dates and pax.` });
          break;
        }
      }
    });
  }
  return { items, errors };
}
