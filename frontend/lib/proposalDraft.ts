// lib/proposalDraft.ts
// AA-683 (builder lane) — the editable state of a proposal version in the builder, as pure functions.
//
// The bk-003 item model is a free-text row (title, day_from, day_to, optional source_tour_id, notes),
// ordered by position. The builder shows items grouped under the day they start on, so the saved order is
// always "by start day, then the order staff arranged inside that day" (normalise()).
//
// Net cost lines live with the item they price and are saved with it: PUT …/items carries `costs[]` per
// item (bk-004 version_item_costs, AA-810). They feed the live quote preview; "Apply price" (POST
// …/versions/{n}/price) prices the SAVED lines on the server and stores the total.
//
// An item may book a hotel room type or a service (bk-010, AA-823). Saved with no cost line, such an item is sent
// without `costs`, and the API fills its net from the supplier rate in force (the line keeps `supplier_rate_id`).

import type { VersionItem, VersionItemCis, VersionItemSupply } from "./api/types";
import type { CostInput, ItemInput, QuotePreviewInput } from "./api/proposals";

export interface CostLine {
  key: string;
  net_amount: string;
  net_currency: string;
  supplier_note: string;
  /** Set on a line the API defaulted from a supplier rate; sent back as is. */
  supplier_rate_id?: string | null;
}

export interface DraftItem {
  /** Stable React key; equals `id` for saved items. */
  key: string;
  id?: string;
  title: string;
  day_from: number;
  day_to: number;
  source_tour_id: string | null;
  notes: string;
  costs: CostLine[];
  /** The hotel room type or service this item books, if any. */
  supply?: VersionItemSupply | null;
  /** AA-1013: the CIS read-back of the saved item (display only, never sent back). */
  cis?: VersionItemCis | null;
}

export const MAX_ITEMS = 200;
export const MAX_DAY = 366;
export const TITLE_MAX = 500;
export const NOTES_MAX = 5000;
/** Up to 16 digits with at most 2 decimals (bk-004 Money: max_digits 16, decimal_places 2). */
const AMOUNT_RE = /^\d{1,14}(\.\d{1,2})?$/;
const CURRENCY_RE = /^[A-Z]{3}$/;

let seq = 0;
export function newKey(prefix = "k"): string {
  seq += 1;
  return `${prefix}-${Date.now().toString(36)}-${seq}`;
}

export function fromApi(items: VersionItem[]): DraftItem[] {
  return [...items]
    .sort((a, b) => a.position - b.position)
    .map((it) => ({
      key: it.id,
      id: it.id,
      title: it.title,
      day_from: it.day_from,
      day_to: it.day_to,
      source_tour_id: it.source_tour_id,
      notes: it.notes ?? "",
      costs: (it.costs ?? []).map((c) => ({
        key: c.id,
        net_amount: c.net_amount,
        net_currency: c.net_currency.trim(),
        supplier_note: c.supplier_note ?? "",
        supplier_rate_id: c.supplier_rate_id ?? null,
      })),
      supply: it.supply ?? null,
      cis: it.cis ?? null,
    }));
}

/** Stable sort by start day: the order inside one day is kept. */
export function normalise(items: DraftItem[]): DraftItem[] {
  return items
    .map((it, i) => ({ it, i }))
    .sort((a, b) => a.it.day_from - b.it.day_from || a.i - b.i)
    .map(({ it }) => it);
}

export function costInput(c: Pick<CostLine, "net_amount" | "net_currency" | "supplier_note" | "supplier_rate_id">): CostInput {
  return {
    net_amount: c.net_amount.trim(),
    net_currency: c.net_currency.trim().toUpperCase(),
    supplier_note: c.supplier_note.trim() ? c.supplier_note.trim() : null,
    ...(c.supplier_rate_id ? { supplier_rate_id: c.supplier_rate_id } : {}),
  };
}

export function toInput(items: DraftItem[]): ItemInput[] {
  return normalise(items).map((it) => ({
    ...(it.id ? { id: it.id } : {}),
    title: it.title.trim(),
    day_from: it.day_from,
    day_to: it.day_to,
    source_tour_id: it.source_tour_id,
    notes: it.notes.trim() ? it.notes.trim() : null,
    hotel_room_type_id: it.supply?.kind === "room_type" ? it.supply.id : null,
    service_id: it.supply?.kind === "service" ? it.supply.id : null,
    // No line on an item that books supply: let the API take the rate in force.
    ...(it.supply && it.costs.length === 0 ? {} : { costs: it.costs.map(costInput) }),
  }));
}

/** What PUT …/items saves (items and their cost lines), to compare with the last saved state. Amounts are
 *  compared as numbers' canonical text so "100" and "100.00" (the API's form) are the same line. */
export function itemsSignature(items: DraftItem[]): string {
  return JSON.stringify(
    toInput(items).map((it) => ({
      ...it,
      costs: it.costs?.map((c) => ({ ...c, net_amount: canonicalAmount(c.net_amount) })),
    })),
  );
}

function canonicalAmount(v: string): string {
  if (!AMOUNT_RE.test(v)) return v;
  const [whole, frac = ""] = v.split(".");
  return `${whole.replace(/^0+(?=\d)/, "")}.${frac.padEnd(2, "0")}`;
}

/** Index of the first item whose cost lines cannot be saved, with the error, or null. */
export function firstCostError(items: DraftItem[]): { item: DraftItem; error: string } | null {
  for (const item of normalise(items)) {
    for (const c of item.costs) {
      const error = costLineError(c);
      if (error) return { item, error };
    }
  }
  return null;
}

export interface ItemErrors {
  title?: string;
  days?: string;
  notes?: string;
}

export function validateItem(it: Pick<DraftItem, "title" | "day_from" | "day_to" | "notes">): ItemErrors {
  const e: ItemErrors = {};
  if (!it.title.trim()) e.title = "Give the item a title.";
  else if (it.title.length > TITLE_MAX) e.title = `At most ${TITLE_MAX} characters.`;
  if (!Number.isInteger(it.day_from) || !Number.isInteger(it.day_to) || it.day_from < 1 || it.day_to > MAX_DAY) {
    e.days = `Days run from 1 to ${MAX_DAY}.`;
  } else if (it.day_to < it.day_from) e.days = "The last day cannot be before the first day.";
  if (it.notes.length > NOTES_MAX) e.notes = `At most ${NOTES_MAX} characters.`;
  return e;
}

/** Move the item one place up or down among the items that start on the same day. */
export function moveWithinDay(items: DraftItem[], key: string, dir: -1 | 1): DraftItem[] {
  const list = normalise(items);
  const i = list.findIndex((x) => x.key === key);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= list.length || list[j].day_from !== list[i].day_from) return list;
  const next = [...list];
  [next[i], next[j]] = [next[j], next[i]];
  return next;
}

/** Drop `key` before `beforeKey` (same start day), or move it to start on `toDay` keeping its length. */
export function dropItem(items: DraftItem[], key: string, target: { beforeKey?: string; toDay?: number }): DraftItem[] {
  const list = normalise(items);
  const moving = list.find((x) => x.key === key);
  if (!moving) return list;
  const rest = list.filter((x) => x.key !== key);
  if (target.beforeKey) {
    const anchor = rest.find((x) => x.key === target.beforeKey);
    if (!anchor) return list;
    const len = moving.day_to - moving.day_from;
    const moved = { ...moving, day_from: anchor.day_from, day_to: Math.min(MAX_DAY, anchor.day_from + len) };
    const at = rest.indexOf(anchor);
    return normalise([...rest.slice(0, at), moved, ...rest.slice(at)]);
  }
  if (target.toDay) {
    const len = moving.day_to - moving.day_from;
    const moved = { ...moving, day_from: target.toDay, day_to: Math.min(MAX_DAY, target.toDay + len) };
    return normalise([...rest, moved]);
  }
  return list;
}

export interface DayGroup {
  day: number;
  /** Items that start on this day. */
  starts: DraftItem[];
  /** Items that started earlier and still run on this day. */
  continues: DraftItem[];
}

/** Days 1…max(last item day, minDays), each with the items starting on it and the ones still running. */
export function groupByDay(items: DraftItem[], minDays = 0): DayGroup[] {
  const list = normalise(items);
  const last = Math.max(minDays, list.reduce((m, it) => Math.max(m, it.day_to), 0));
  const out: DayGroup[] = [];
  for (let d = 1; d <= last; d++) {
    out.push({
      day: d,
      starts: list.filter((it) => it.day_from === d),
      continues: list.filter((it) => it.day_from < d && d <= it.day_to),
    });
  }
  return out;
}

// ── Cost lines → quote preview ───────────────────────────────────────────────
export function costLineError(c: Pick<CostLine, "net_amount" | "net_currency">): string | null {
  if (!c.net_amount.trim()) return "Enter the net amount.";
  // 0 is valid (an included item), like the API (bk-004 net_amount >= 0; review finding 15).
  if (!AMOUNT_RE.test(c.net_amount.trim())) return "An amount of 0 or more, at most 2 decimals.";
  if (!CURRENCY_RE.test(c.net_currency.trim().toUpperCase())) return "A 3-letter currency code.";
  return null;
}

/** An override total: strictly positive (the API's override_total is > 0). */
export function isValidAmount(value: string): boolean {
  return AMOUNT_RE.test(value.trim()) && Number(value) > 0;
}

/** The request for POST /booking/pricing/quote-preview, or null when a line is incomplete or none exist. */
export function quoteRequest(
  lines: Pick<CostLine, "net_amount" | "net_currency">[],
  opts: { channel: QuotePreviewInput["channel"]; currency: string; peak: boolean; override: string },
): QuotePreviewInput | null {
  if (lines.length === 0 || lines.some((c) => costLineError(c))) return null;
  const override = opts.override.trim();
  if (override && !isValidAmount(override)) return null;
  return {
    channel: opts.channel,
    currency: opts.currency,
    peak: opts.peak,
    items: lines.map((c) => ({ net_amount: c.net_amount.trim(), net_currency: c.net_currency.trim().toUpperCase() })),
    ...(override ? { override_total: override } : {}),
  };
}

/** "0.3000" → "30%", "0.0275" → "2.75%". */
export function pct(fraction: string | number | null | undefined): string {
  const n = typeof fraction === "number" ? fraction : Number(fraction);
  if (fraction === null || fraction === undefined || !Number.isFinite(n)) return "—";
  return `${Number((n * 100).toFixed(2))}%`;
}

// ── Send ────────────────────────────────────────────────────────────────────
export function addDays(base: Date, days: number): string {
  const d = new Date(Date.UTC(base.getFullYear(), base.getMonth(), base.getDate()));
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function defaultSendMessage(p: { customerName: string | null; tripTitle: string; staffName: string }): string {
  const first = p.customerName?.trim().split(/\s+/)[0];
  return [
    `Hi ${first || "there"},`,
    "",
    `Here is our proposal for your ${p.tripTitle} trip. Please review the days and the price, and tell us what you would like to change.`,
    "",
    "Best regards,",
    p.staffName,
    "Adventure Asia",
  ].join("\n");
}
