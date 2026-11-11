// lib/api/finance.ts
// AA-1074 (bk-045) — typed calls for Booking finance (backend app/routers/finance.py): departure shared costs, supplier
// purchase orders and profit. Money is a decimal string, never a float; USD figures are sums of per-line cents.
//
//   GET    /booking/finance/departures?from&to                 departures with sold seats, shared costs, profit (USD)
//   GET    /booking/departures/{id}/costs                      shared costs, cases, bills, planned vs actual (USD)
//   POST   /booking/departures/{id}/costs                      add a shared cost (rate: service_id; manual: amount)
//   PATCH  /booking/departure-costs/{id} · POST …/order · POST …/remove (reason)
//   GET    /booking/supplier-orders?status&supplier_id · POST /booking/supplier-orders (trip_cases, departures)
//   GET    /booking/supplier-orders/{ref} · …/document (printable HTML) · POST …/send (note) · …/cancel (reason)
//   POST   /booking/supplier-orders/{ref}/lines/{booking_id}/remove
//   GET    /booking/finance/profit/cases | departures | agencies   (profit.read: admin, finance)
//
// Costs and orders need bill.read / ops.read plus pricing.read (net amounts); profit needs profit.read.

import { BFF_BASE, bkGet, bkSend } from "./client";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type CostBasis = "group" | "vehicle" | "day" | "person";
export type CostPricing = "rate" | "manual";
export type OrderStatus = "draft" | "sending" | "sent" | "cancelled";
export type OrderState = OrderStatus | "confirmed" | "partly_confirmed";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface DepartureCost {
  id: string;
  departure_id: string;
  supplier: { id: string; code: string; name: string };
  service: { id: string; code: string; title: string } | null;
  supplier_rate_id: string | null;
  item_description: string;
  service_date: IsoDate;
  basis: CostBasis;
  quantity: number;
  pricing: CostPricing;
  unit_amount: DecimalString;
  net_amount: DecimalString;
  currency: string;
  priced_for_pax: number;
  priced_at: IsoDateTime | null;
  frozen_at: IsoDateTime | null;
  supplier_booking: { id: string; code: string; status: string; order_code: string | null } | null;
  status: "active" | "cancelled";
  cancel_reason: string | null;
  notes: string | null;
  planned_usd: DecimalString | null;
  billed: boolean | null;
  billed_usd: DecimalString | null;
  cost_usd: DecimalString | null;
  actions: { edit: boolean; order: boolean; remove: boolean };
}

export interface DepartureCaseRow {
  trip_case_id: string;
  code: string;
  status: string;
  agency_name: string | null;
  currency: string;
  // Realised revenue / profit / margin: null without profit.read (sales, contracting).
  revenue: DecimalString | null;
  revenue_usd: DecimalString | null;
  planned_cost_usd: DecimalString;
  actual_cost_usd: DecimalString;
  shared_cost_usd: DecimalString;
  cost_usd: DecimalString;
  profit_usd: DecimalString | null;
  margin: DecimalString | null;
  unbilled_bookings: number;
  fx_missing: boolean;
  cancelled: boolean;
  seats: number;
}

export interface DepartureCosts {
  departure: {
    id: string;
    trip_plan_source_id: string;
    start_date: IsoDate;
    capacity: number;
    status: string;
    sold_seats: number;
  };
  shared_costs: DepartureCost[];
  cases: DepartureCaseRow[];
  bills: { id: string; code: string; status: string; currency: string; amount: DecimalString; bill_number: string; supplier_name: string }[];
  totals_usd: {
    shared_planned: DecimalString;
    shared_billed: DecimalString;
    shared_cost: DecimalString;
    // revenue, profit, profit_per_seat, margin: null without profit.read.
    revenue: DecimalString | null;
    case_cost: DecimalString;
    cost: DecimalString;
    profit: DecimalString | null;
    profit_per_seat: DecimalString | null;
    margin: DecimalString | null;
    fx_missing: boolean;
  };
}

export interface FinanceDeparture {
  id: string;
  start_date: IsoDate;
  status: string;
  capacity: number;
  trip_plan_source_id: string;
  sold_seats: number;
  cases: number;
  shared_cost_lines: number;
  revenue_usd: DecimalString | null;
  cost_usd: DecimalString | null;
  profit_usd: DecimalString | null;
  profit_per_seat_usd: DecimalString | null;
  margin: DecimalString | null;
  fx_missing: boolean;
}

export interface NewDepartureCost {
  pricing?: CostPricing;
  service_id?: string;
  supplier_id?: string;
  item_description?: string;
  service_date?: IsoDate;
  basis?: CostBasis;
  quantity?: number;
  unit_amount?: DecimalString;
  currency?: string;
  notes?: string;
}

export interface OrderLine {
  id: string;
  code: string;
  status: string;
  item_description: string;
  service_date: IsoDate | null;
  nights: number | null;
  pax: number | null;
  net_amount: DecimalString | null;
  net_currency: string | null;
  confirmation_ref: string | null;
  trip_case_code: string | null;
  departure_start_date: IsoDate | null;
}

export interface SupplierOrder {
  id: string;
  code: string;
  supplier: { id: string; code: string; name: string; email: string | null };
  currency: string;
  service_month: IsoDate;
  status: OrderStatus;
  state: OrderState;
  total_net: DecimalString | null;
  live_net: DecimalString | null;
  sent_to: string | null;
  redirected: boolean;
  sent_at: IsoDateTime | null;
  send_error: string | null;
  note: string | null;
  cancel_reason: string | null;
  created_at: IsoDateTime;
  lines: number | OrderLine[];
  line_counts: Record<string, number>;
  actions: { send: boolean; cancel: boolean; remove_lines: boolean };
}

export interface CaseProfit {
  trip_case_id: string;
  code: string;
  status: string;
  start_date: IsoDate | null;
  agency_name: string | null;
  departure_id: string | null;
  trip_plan_source_id: string | null;
  currency: string;
  cancelled: boolean;
  revenue: DecimalString;
  revenue_usd: DecimalString;
  quoted_cost_usd: DecimalString | null;
  planned_cost_usd: DecimalString;
  actual_cost_usd: DecimalString;
  shared_cost_usd: DecimalString;
  cost_basis: "quote" | "bookings";
  cost_usd: DecimalString;
  profit_usd: DecimalString;
  planned_profit_usd: DecimalString;
  profit: DecimalString;
  margin: DecimalString | null;
  unbilled_bookings: number;
  fx_missing: boolean;
}

export interface DepartureProfit {
  departure_id: string;
  trip_plan_source_id: string;
  start_date: IsoDate;
  status: string;
  capacity: number;
  sold_seats: number;
  cases: number;
  revenue_usd: DecimalString;
  case_cost_usd: DecimalString;
  shared_cost_usd: DecimalString;
  cost_usd: DecimalString;
  profit_usd: DecimalString;
  profit_per_seat_usd: DecimalString | null;
  margin: DecimalString | null;
  fx_missing: boolean;
}

export interface AgencyProfitMonth {
  partner_id: string;
  agency_name: string | null;
  travel_month: IsoDate;
  cases: number;
  cancelled_cases: number;
  revenue_usd: DecimalString;
  cost_usd: DecimalString;
  profit_usd: DecimalString;
  margin: DecimalString | null;
  fx_missing: boolean;
}

export interface ProfitTotals {
  revenue_usd: DecimalString;
  cost_usd: DecimalString;
  profit_usd: DecimalString;
  margin: DecimalString | null;
  fx_missing: boolean;
}

type Window = { from?: string; to?: string; limit?: number; offset?: number };

export const ORDER_STATE_LABEL: Record<OrderState, string> = {
  draft: "Draft",
  sending: "Sending",
  sent: "Sent",
  confirmed: "Confirmed",
  partly_confirmed: "Partly confirmed",
  cancelled: "Cancelled",
};

export const BASIS_LABEL: Record<CostBasis, string> = {
  group: "Per group",
  vehicle: "Per vehicle",
  day: "Per day",
  person: "Per person",
};

/** Margin as a percentage with one decimal ("0.8278" -> "82.8 %"); null -> "—". */
export function marginLabel(margin: DecimalString | null | undefined): string {
  if (margin === null || margin === undefined) return "—";
  const n = Number(margin);
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)} %` : "—";
}

export const financeApi = {
  departures: (q: Window = {}) => bkGet<Page<FinanceDeparture>>("finance/departures", q),
  departureCosts: (id: string) => bkGet<DepartureCosts>(`departures/${enc(id)}/costs`),
  addCost: (departureId: string, body: NewDepartureCost) => bkSend<DepartureCost>(`departures/${enc(departureId)}/costs`, { body }),
  updateCost: (id: string, body: Partial<NewDepartureCost>) => bkSend<DepartureCost>(`departure-costs/${enc(id)}`, { method: "PATCH", body }),
  orderCost: (id: string) => bkSend<DepartureCost>(`departure-costs/${enc(id)}/order`),
  removeCost: (id: string, reason: string) => bkSend<DepartureCost>(`departure-costs/${enc(id)}/remove`, { body: { reason } }),
  orders: (q: { status?: string; supplier_id?: string; limit?: number; offset?: number } = {}) =>
    bkGet<Page<SupplierOrder>>("supplier-orders", q),
  order: (ref: string) => bkGet<SupplierOrder & { lines: OrderLine[] }>(`supplier-orders/${enc(ref)}`),
  createOrders: (body: { trip_cases?: string[]; departures?: string[] }) =>
    bkSend<{ orders: SupplierOrder[]; lines: number; skipped: { no_net: number; no_date: number } }>("supplier-orders", { body }),
  sendOrder: (ref: string, note?: string) => bkSend<SupplierOrder>(`supplier-orders/${enc(ref)}/send`, { body: { note: note || null } }),
  cancelOrder: (ref: string, reason: string) => bkSend<SupplierOrder>(`supplier-orders/${enc(ref)}/cancel`, { body: { reason } }),
  removeOrderLine: (ref: string, bookingId: string) => bkSend<SupplierOrder>(`supplier-orders/${enc(ref)}/lines/${enc(bookingId)}/remove`),
  /** The printable order, opened in a new tab through the BFF (the session cookie authenticates it). */
  documentHref: (ref: string) => `${BFF_BASE}/supplier-orders/${enc(ref)}/document`,
  profitCases: (q: Window & { partner_id?: string } = {}) =>
    bkGet<Page<CaseProfit> & { totals: ProfitTotals & { planned_cost_usd: DecimalString; actual_cost_usd: DecimalString } }>(
      "finance/profit/cases",
      q,
    ),
  profitDepartures: (q: Window = {}) => bkGet<Page<DepartureProfit> & { totals: ProfitTotals }>("finance/profit/departures", q),
  profitAgencies: (q: Window = {}) =>
    bkGet<{ items: AgencyProfitMonth[]; total: number; totals: ProfitTotals }>("finance/profit/agencies", q),
};
