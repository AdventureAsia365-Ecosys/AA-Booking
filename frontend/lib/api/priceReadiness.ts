// lib/api/priceReadiness.ts
// AA-1029 — typed calls for Pricing → Price readiness (backend routers/catalog.py + app/catalog/price_readiness.py).
// Which trip plans price and why the others do not: one status per plan (the cost sheet's own reasons), the components
// without a published rate in the next 365 days (codes only, never a net) and the cost currencies without an approved
// FX rate. The summary counts every plan; `items` are the plans that match the filters.
//
//   GET /booking/catalog/price-readiness?status=&country=&q=&format=json|csv     (pricing.read)
//
// The rate-sheet dry run (POST /booking/suppliers/{ref}/rate-sheets?dry_run=true) carries the sheet's `impact`
// (lib/api/suppliers.ts RateSheetImpact).

import { ApiError, BFF_BASE, bkGet, buildQuery } from "./client";
import type { DecimalString, IsoDate } from "./types";

export const READINESS_STATUSES = [
  "priced",
  "hand_priced",
  "missing_rates",
  "missing_fx",
  "no_rule",
  "no_components",
  "inactive_component",
] as const;
export type ReadinessStatus = (typeof READINESS_STATUSES)[number];

export interface UnratedComponent {
  day_no: number;
  kind: "hotel" | "service";
  /** HTL- or SVC- code. */
  code: string | null;
  room_type: string | null;
  supplier_code: string | null;
}

export interface ReadinessItem {
  tour_key: string;
  title: string | null;
  country: string | null;
  duration_days: number | null;
  in_catalog: boolean | null;
  sellable: boolean;
  status: ReadinessStatus;
  /** The cost sheet's reason today (null = nothing stops it). */
  reason: ReadinessStatus | null;
  components: number;
  components_without_rate: UnratedComponent[];
  missing_fx: string[];
  computed_rows: number;
  hand_rows: number;
  hand_rows_expired: number;
  from_price: { amount: DecimalString; currency: string; season: string | null } | null;
  pending_recompute: boolean;
  note: string | null;
}

export type StatusCounts = Record<ReadinessStatus, number>;

export interface PriceReadiness {
  today: IsoDate;
  horizon_to: IsoDate;
  rules: string[];
  fx_currencies: string[];
  summary: {
    total: number;
    by_status: StatusCounts;
    by_country: ({ country: string; total: number } & StatusCounts)[];
  };
  items: ReadinessItem[];
  degraded: string[];
}

export interface ReadinessFilters {
  status?: string;
  country?: string;
  q?: string;
}

/** The staff CSV (fixed columns, no net or margin), with the server's file name. */
async function csv(filters: ReadinessFilters): Promise<{ blob: Blob; filename: string }> {
  const res = await fetch(`${BFF_BASE}/catalog/price-readiness${buildQuery({ ...filters, format: "csv" })}`, { cache: "no-store" });
  if (!res.ok) throw new ApiError("The report could not be downloaded.", res.status);
  const named = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "");
  return { blob: await res.blob(), filename: named?.[1] ?? "aa-price-readiness.csv" };
}

export const priceReadinessApi = {
  get: (filters: ReadinessFilters) => bkGet<PriceReadiness>("catalog/price-readiness", { ...filters }),
  csv,
};
