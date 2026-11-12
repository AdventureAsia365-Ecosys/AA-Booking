// lib/api/priceWorkbench.ts
// AA-1091 — typed calls for Pricing → Price setup (backend routers/catalog.py + app/catalog/price_workbench.py,
// price_import.py, component_suggest.py). Every trip plan with what it still needs (checks), the next step, the CIS
// reference price and a provisional price (the CIS reference shown as the price hint while no supplier price exists:
// a display price only, never quoted to agencies).
//
//   GET  /booking/catalog/price-workbench?status=&country=&q=&missing=&next_step=        (pricing.read)
//   POST /booking/catalog/price-workbench/actions {action, tour_keys}                       (trip_plan.write)
//   GET  /booking/catalog/price-import/template?…same filters…&blank=                       (pricing.read, .xlsx)
//   POST /booking/catalog/price-import[?dry_run=true] {data_base64, filename}               (trip_plan.write)
//   GET  /booking/catalog/trip-plans/{key}/component-suggestions                           (supply.read)

import { ApiError, BFF_BASE, bkGet, bkSend, buildQuery } from "./client";
import type { ReadinessItem, ReadinessStatus, StatusCounts } from "./priceReadiness";
import type { DecimalString, IsoDate } from "./types";

export const WORKBENCH_CHECKS = ["components", "rates", "fx", "rule", "price", "sellable", "departures"] as const;
export type WorkbenchCheck = (typeof WORKBENCH_CHECKS)[number];

export const NEXT_STEPS = [
  "add_components",
  "fix_inactive",
  "load_rates",
  "approve_fx",
  "approve_rule",
  "recompute",
  "make_sellable",
  "done",
] as const;
export type NextStep = (typeof NEXT_STEPS)[number];

export const WORKBENCH_ACTIONS = [
  "make_sellable",
  "make_not_sellable",
  "apply_provisional",
  "clear_provisional",
  "recompute",
] as const;
export type WorkbenchAction = (typeof WORKBENCH_ACTIONS)[number];

export interface WorkbenchChecks {
  components: { ok: boolean; count: number };
  rates: { ok: boolean; rated: number; total: number };
  fx: { ok: boolean; missing: string[] };
  rule: { ok: boolean };
  price: { ok: boolean; kind: "rates" | "hand" | "provisional" | null };
  sellable: { ok: boolean };
  departures: { ok: boolean; count: number; next: IsoDate | null };
}

export interface WorkbenchItem extends ReadinessItem {
  checks: WorkbenchChecks;
  missing: WorkbenchCheck[];
  next_step: NextStep;
  /** The CIS source price (reference only; never a selling price). */
  reference: { raw: string | null; amount: DecimalString | null; currency: string | null; reviewed: boolean } | null;
  /** The price hint shown while no supplier price exists ("no supplier price yet"): RRP display only. */
  provisional: { amount: DecimalString; currency: string; from_reference: boolean } | null;
  supplier_price: boolean;
}

export interface Workbench {
  today: IsoDate;
  horizon_to: IsoDate;
  rules: string[];
  fx_currencies: string[];
  summary: {
    total: number;
    by_status: StatusCounts;
    by_country: ({ country: string; total: number } & StatusCounts)[];
    by_next_step: Record<NextStep, number>;
    by_missing: Record<WorkbenchCheck, number>;
    provisional: number;
  };
  items: WorkbenchItem[];
  degraded: string[];
}

export interface WorkbenchFilters {
  status?: string;
  country?: string;
  q?: string;
  missing?: string;
  next_step?: string;
}

export interface ActionResult {
  action: WorkbenchAction;
  results: { tour_key: string; outcome: "done" | "unchanged" | "skipped"; reason: string | null }[];
  done: number;
  unchanged: number;
  skipped: number;
}

export interface ImportProblem {
  sheet: string;
  row: number | null;
  column: string | null;
  code: string;
  message: string;
}

export interface ImportTour {
  tour_key: string;
  title: string | null;
  country: string | null;
  sellable_before: boolean;
  sellable_after: boolean;
  components_before: number;
  components_after: number;
  components_changed: boolean;
  hand_rows_before: number;
  hand_rows_after: number;
  computed_rows_replaced: number;
  /** Rows typed with a net cost on the single price form that a hand row of the file replaces. */
  net_rows_replaced: number;
  departures_new: IsoDate[];
  departures_existing: IsoDate[];
  status_before: ReadinessStatus | null;
  status_after: ReadinessStatus | null;
}

export interface ImportResult {
  dry_run: boolean;
  tours: ImportTour[];
  totals: {
    tours: number;
    components_changed: number;
    price_tours: number;
    departures_new: number;
    departures_existing: number;
    sellable_changes: number;
    would_be_priced: number;
  };
  ignored_sheets: string[];
  recomputed?: number;
}

export interface SuggestionCandidate {
  kind: string;
  hotel_room_type_id: string | null;
  service_id: string | null;
  code: string;
  label: string;
  room_type: string | null;
  room_types: { id: string; name: string }[];
  supplier_code: string | null;
  score: number;
  confidence: "high" | "medium" | "low";
  reasons: string[];
}

export interface SuggestionNeed {
  kind: string;
  keywords: string[];
  place: string | null;
  status: "suggested" | "covered" | "unmatched";
  current: { day_no: number; kind: string; label: string }[];
  suggestion: SuggestionCandidate | null;
  alternatives: SuggestionCandidate[];
}

export interface ComponentSuggestions {
  tour_key: string;
  title?: string | null;
  country?: string | null;
  duration_days?: number | null;
  days: { day_no: number; title: string | null; places: string[]; overnight_place: string | null; needs: SuggestionNeed[] }[];
  unmatched: { day_no: number; kind: string; keywords: string[]; place: string | null; why: string }[];
  counts: { suggested: number; covered: number; unmatched: number };
  degraded: string[];
}

export interface PlanComponent {
  component_id: string;
  kind: string;
  day_no: number;
  hotel_room_type_id: string | null;
  service_id: string | null;
  label: string;
  active: boolean;
}

export interface PlanComponents {
  trip_plan_id: string;
  source_id: string;
  items: PlanComponent[];
}

export type ComponentIn = { day_no: number; hotel_room_type_id?: string | null; service_id?: string | null };

async function template(filters: WorkbenchFilters, blank = false): Promise<{ blob: Blob; filename: string }> {
  const query = buildQuery(blank ? { blank: "true" } : { ...filters });
  const res = await fetch(`${BFF_BASE}/catalog/price-import/template${query}`, { cache: "no-store" });
  if (!res.ok) throw new ApiError("The workbook could not be downloaded.", res.status);
  const named = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "");
  return { blob: await res.blob(), filename: named?.[1] ?? "aa-price-setup.xlsx" };
}

const enc = encodeURIComponent;

export const priceWorkbenchApi = {
  get: (filters: WorkbenchFilters) => bkGet<Workbench>("catalog/price-workbench", { ...filters }),
  action: (action: WorkbenchAction, tourKeys: string[]) =>
    bkSend<ActionResult>("catalog/price-workbench/actions", { body: { action, tour_keys: tourKeys } }),
  template,
  importWorkbook: (dataBase64: string, filename: string, dryRun: boolean) =>
    bkSend<ImportResult>(`catalog/price-import${dryRun ? "?dry_run=true" : ""}`, {
      body: { data_base64: dataBase64, filename },
    }),
  components: (sourceId: string) => bkGet<PlanComponents>(`catalog/trip-plans/${enc(sourceId)}/components`),
  /** PUT semantics: the plan's whole set (backend app/b2b/plan_components.py). */
  saveComponents: (sourceId: string, items: ComponentIn[]) =>
    bkSend<PlanComponents>(`catalog/trip-plans/${enc(sourceId)}/components`, { method: "PUT", body: { items } }),
  suggestions: (sourceId: string) => bkGet<ComponentSuggestions>(`catalog/trip-plans/${enc(sourceId)}/component-suggestions`),
};
