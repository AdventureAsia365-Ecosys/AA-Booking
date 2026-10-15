// lib/api/proposals.ts
// AA-683 (builder lane) — typed calls for the proposal builder, preview, send and the pricing calculator.
// Kept apart from lib/api/client.ts so the parallel UI lanes do not edit the same file; it reuses that
// file's BFF helpers (bkGet / bkSend → /api/booking/<path>) and error type.
//
// Endpoints (backend feat/aa-681 routers/trip_cases.py, feat/aa-810 routers/pricing.py):
//   POST   /booking/trip-cases/{ref}/versions                       create (V1 from legs, Vn copies latest)
//   GET    /booking/trip-cases/{ref}/versions/{n}                   one version (+ trip_case, item_count)
//   PATCH  /booking/trip-cases/{ref}/versions/{n}                   title / notes of a draft
//   DELETE /booking/trip-cases/{ref}/versions/{n}                   soft-delete an unsent draft
//   PUT    /booking/trip-cases/{ref}/versions/{n}/items             save items (upsert by id, D2) + their costs[]
//   POST   /booking/trip-cases/{ref}/versions/{n}/price             price the stored costs, save the total
//   POST   /booking/trip-cases/{ref}/versions/{n}/send              mark sent + lock (no e-mail)
//   POST   /booking/trip-cases/{ref}/versions/{n}/approve           record the customer's approval
//   POST   /booking/pricing/quote-preview                           price net costs (writes nothing)

import { bkSend } from "./client";
import type { DecimalString, IsoDate, TripCaseStatus, Version, VersionItem, VersionWithItems } from "./types";

const enc = encodeURIComponent;

export interface CostInput {
  net_amount: string;
  net_currency: string;
  supplier_note?: string | null;
  supplier_rate_id?: string | null;
}

/** One item as PUT …/items takes it. `id` keeps an existing row; `costs` replaces its net cost lines. */
export interface ItemInput {
  id?: string;
  title: string;
  day_from: number;
  day_to: number;
  source_tour_id?: string | null;
  notes?: string | null;
  /** Omitted on an item that books a room type or service: the API then takes the rate in force (AA-823). */
  costs?: CostInput[];
  hotel_room_type_id?: string | null;
  service_id?: string | null;
}

export interface SendInput {
  /** Must equal the customer's e-mail (Q14); omit when the customer has none. */
  to?: string;
  subject: string;
  message: string;
  valid_until?: IsoDate;
}

export interface SentVersion extends Version {
  trip_case: { id: string; code: string; status: TripCaseStatus };
  notification_id: string | null;
  email_sent: false;
}

// ── Pricing (bk-004, AA-810) ─────────────────────────────────────────────────
export const CHANNELS = [
  { value: "b2c_tailor_made", label: "B2C tailor-made" },
  { value: "fixed_departure", label: "Fixed departure" },
  { value: "b2b", label: "B2B" },
] as const;
export type Channel = (typeof CHANNELS)[number]["value"];

/** Selling currencies (bk-004 engine.SELLING_CURRENCIES, ADR 0005 decision 5). */
export const SELLING_CURRENCIES = ["USD", "EUR", "GBP", "AUD", "SGD"] as const;

export interface QuotePreviewInput {
  channel: Channel;
  currency: string;
  peak: boolean;
  items: { net_amount: string; net_currency: string }[];
  override_total?: string;
}

export interface QuotePreview {
  channel: Channel;
  currency: string;
  peak: boolean;
  priced_on: IsoDate;
  rule: {
    channel: Channel;
    target_margin: DecimalString;
    floor_margin: DecimalString;
    peak_uplift_pts: DecimalString;
    card_fee_pct: DecimalString;
    valid_from: IsoDate;
  };
  fx_rates: { currency: string; rate_per_usd: DecimalString; buffer_pct: DecimalString; as_of: IsoDate; source: string }[];
  margin_applied: DecimalString;
  /** Same order as the request `items`. */
  lines: { net_amount: DecimalString; net_currency: string; net_usd: DecimalString; sell_usd: DecimalString; sell_amount: DecimalString }[];
  net_total_usd: DecimalString;
  subtotal: DecimalString;
  rounding_increment: DecimalString;
  total: DecimalString;
  override_total: DecimalString | null;
  final_total: DecimalString;
  effective_margin: DecimalString;
  floor_margin: DecimalString;
  below_floor: boolean;
}

export interface PriceInput {
  channel: Channel;
  currency: string;
  peak: boolean;
  override_total?: string;
  override_reason?: string;
}

/** POST …/versions/{n}/price: the quote it applied and the version with the saved total. */
export interface PricedVersion {
  quote: QuotePreview;
  version: Version;
}

function versionPath(ref: string, n: number) {
  return `trip-cases/${enc(ref)}/versions/${n}`;
}

export const proposalsApi = {
  create: (ref: string, notes?: string) =>
    bkSend<VersionWithItems>(`trip-cases/${enc(ref)}/versions`, { body: notes ? { notes } : {} }),
  update: (ref: string, n: number, body: { title?: string; notes?: string | null }) =>
    bkSend<Version & { changed: string[] }>(versionPath(ref, n), { method: "PATCH", body }),
  remove: (ref: string, n: number) => bkSend<void>(versionPath(ref, n), { method: "DELETE" }),
  saveItems: (ref: string, n: number, items: ItemInput[]) =>
    bkSend<Version & { items: VersionItem[] }>(`${versionPath(ref, n)}/items`, { method: "PUT", body: { items } }),
  price: (ref: string, n: number, body: PriceInput) => bkSend<PricedVersion>(`${versionPath(ref, n)}/price`, { body }),
  send: (ref: string, n: number, body: SendInput) => bkSend<SentVersion>(`${versionPath(ref, n)}/send`, { body }),
  approve: (ref: string, n: number, note?: string) =>
    bkSend<Version>(`${versionPath(ref, n)}/approve`, { body: note ? { note } : {} }),
  quotePreview: (body: QuotePreviewInput) => bkSend<QuotePreview>("pricing/quote-preview", { body }),
};
