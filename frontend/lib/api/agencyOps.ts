// lib/api/agencyOps.ts
// AA-1075 (bk-047) — typed calls for the agency booking operations (backend routers/b2b_desk.py, agency_billing.py,
// catalog.py, supplier_bookings.py; app/b2b/desk_bookings.py, amendments.py, vouchers.py, manifest.py).
//
//   GET  /booking/b2b-desk/agencies                                      active agencies (b2b_desk.book)
//   GET  /booking/b2b-desk/entries?status&limit&offset · POST (Idempotency-Key)  staff-entered agency bookings
//   POST /booking/b2b-desk/entries/{id}/approve | /reject                 a price below the floor (b2b_desk.approve)
//   POST /booking/b2b-desk/bookings/{ref}/cancel?dry_run (Idempotency-Key) · GET …/cancellation
//   POST /booking/b2b-desk/bookings/{ref}/cancellation/override | /approve | /reject
//   POST /booking/b2b-desk/bookings/{ref}/lines/{line_ref}/reduce?dry_run · GET …/reductions
//   GET  /booking/b2b-desk/bookings/{ref}/vouchers (HTML) · GET /booking/b2b-desk/departures/{id}/manifest
//   GET|PUT|DELETE /booking/tour-terms/{source_id}                        a tour's deposit terms (billing_config.write)
//   PUT  /booking/catalog/trip-plans/{source_id}/age-bands                child / infant ages (trip_plan.write)
//   POST /booking/supplier-bookings/{id}/cancel-fee                       the supplier's cancellation fee
//
// No traveller data anywhere: travellers are counts by age band, references are the agency's own ids.

import { ApiError, BFF_BASE, bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export interface Pax {
  adult: number;
  child: number;
  infant: number;
}
export interface MoneyText {
  amount: string;
  currency: string;
}
export interface PersonRef {
  id: string;
  name: string | null;
}

export interface Agency {
  partner_id: string;
  tenant_id: string;
  agency_name: string;
  billing_currency: string;
  tier_code: string;
}

export type EntryStatus = "awaiting_approval" | "booked" | "rejected" | "lapsed";
export const ENTRY_STATUS_LABEL: Record<EntryStatus, string> = {
  awaiting_approval: "Waiting for approval",
  booked: "Booked",
  rejected: "Rejected",
  lapsed: "Lapsed",
};

export interface DeskEntry {
  id: string;
  status: EntryStatus;
  agency: { partner_id: string; name: string; tenant_id: string };
  agency_ref: string;
  quote_id: string;
  hold_id: string;
  hold_expires_at: IsoDateTime;
  trip_case: { id: string; code: string } | null;
  currency: string;
  total: string;
  quoted_total: string;
  /** null without pricing.read (a margin). */
  floor_total: string | null;
  override_total: string | null;
  override_reason: string | null;
  entered_by: PersonRef;
  entered_at: IsoDateTime;
  decided_by: PersonRef | null;
  decided_at: IsoDateTime | null;
  can_decide: boolean;
}

export interface EntryPage {
  items: DeskEntry[];
  total: number;
  awaiting_approval: number;
  limit: number;
  offset: number;
}

export interface EntryLine {
  line_ref: string;
  component_id: string;
  departure_id?: string | null;
  service_date: IsoDate;
  nights?: number | null;
  pax: Pax;
}

export interface NewEntry {
  partner_id: string;
  agency_ref: string;
  currency: string;
  lines: EntryLine[];
  override_total?: string | null;
  override_reason?: string | null;
}

export const CANCEL_REASONS = ["client_cancelled", "agency_cancelled", "departure_cancelled", "other"] as const;
export type CancelReason = (typeof CANCEL_REASONS)[number];
export const CANCEL_REASON_LABEL: Record<CancelReason, string> = {
  client_cancelled: "The agency's client cancelled",
  agency_cancelled: "The agency cancelled",
  departure_cancelled: "The departure was cancelled",
  other: "Other",
};
export const REDUCE_REASONS = ["client_cancelled", "agency_cancelled", "other"] as const;
export type ReduceReason = (typeof REDUCE_REASONS)[number];

export interface FeeBasis {
  policy_id: string;
  days_before_departure: number;
  fee_pct: string;
}

export interface CancelResult {
  booking_id: string;
  dry_run: boolean;
  fee: MoneyText;
  refundable_b2b_amount: MoneyText;
  fee_basis: FeeBasis;
  cancelled_at: IsoDateTime | null;
  trip_case_code: string;
  cancellation: Cancellation | null;
}

export interface Cancellation {
  id: string;
  reason_code: CancelReason;
  cancelled_at: IsoDateTime;
  entered_via: "api" | "staff";
  total: MoneyText;
  fee_pct: string;
  policy_fee: MoneyText;
  fee: MoneyText;
  refundable: MoneyText;
  override: {
    fee_pct: string;
    reason: string;
    requested_by: PersonRef;
    requested_at: IsoDateTime;
    approved_by: PersonRef | null;
    approved_at: IsoDateTime | null;
    status: "pending" | "approved";
  } | null;
  can_approve_override: boolean;
}

export interface Reduction {
  booking_id: string;
  line_ref: string;
  dry_run: boolean;
  removed: Pax;
  seats_after: number;
  removed_b2b_amount: MoneyText;
  fee: MoneyText;
  refundable_b2b_amount: MoneyText;
  fee_basis: FeeBasis;
  reduced_at: IsoDateTime | null;
  entered_via?: "api" | "staff";
  reason_code?: ReduceReason;
}

export interface ManifestBooking {
  trip_case_id: string;
  trip_case_code: string;
  agency: string;
  agency_ref: string;
  entered_via: "api" | "staff";
  line_ref: string;
  line_status: "confirmed" | "on_request" | "declined" | "cancelled";
  travellers: Pax;
  rooms: { occupancy: string; count: number }[];
}

export interface Manifest {
  departure: {
    id: string;
    start_date: IsoDate;
    status: "scheduled" | "guaranteed" | "cancelled";
    capacity: number;
    min_to_operate: number;
    source_id: string;
    title: string | null;
  };
  generated_at: IsoDateTime;
  totals: Pax & { seats: number; bookings: number };
  rooms: { occupancy: string; count: number }[];
  bookings: ManifestBooking[];
  supplier_bookings: {
    code: string;
    trip_case_code: string | null;
    service: string;
    supplier: string;
    service_date: IsoDate | null;
    service_time: string | null;
    pickup_point: string | null;
    status: string;
    confirmation_ref: string | null;
  }[];
}

export interface TourTerms {
  id: string;
  trip_plan_id: string;
  source_id: string;
  party_tenant_id: string | null;
  agency_name?: string | null;
  deposit_pct: string;
  balance_days_before_departure: number;
  updated_at: IsoDateTime;
  updated_by: string | null;
}

/** One key per user action: a retry (double click, timeout) is the same write. */
export function actionKey(prefix: string): string {
  const rand = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${rand}`;
}

async function html(path: string): Promise<Blob> {
  const res = await fetch(`${BFF_BASE}/${path}`, { cache: "no-store" });
  if (!res.ok) {
    let code = "";
    let message = "The file could not be downloaded.";
    try {
      const body = (await res.json()) as { detail?: unknown; message?: string };
      if (typeof body.detail === "string") code = body.detail;
      if (typeof body.message === "string") message = body.message;
    } catch {
      // not JSON: keep the generic message
    }
    throw new ApiError(message, res.status, code);
  }
  return res.blob();
}

export const agencyOpsApi = {
  agencies: () => bkGet<{ items: Agency[] }>("b2b-desk/agencies"),
  entries: (q: { status?: EntryStatus; limit?: number; offset?: number }) => bkGet<EntryPage>("b2b-desk/entries", q),
  enter: (body: NewEntry, key: string) => bkSend<DeskEntry>("b2b-desk/entries", { body, idempotencyKey: key }),
  approveEntry: (id: string) => bkSend<DeskEntry>(`b2b-desk/entries/${enc(id)}/approve`),
  rejectEntry: (id: string) => bkSend<DeskEntry>(`b2b-desk/entries/${enc(id)}/reject`),

  cancel: (
    ref: string,
    body: { reason_code: CancelReason; fee_override_pct?: string | null; waive_reason?: string | null },
    key: string,
    dryRun: boolean,
  ) => bkSend<CancelResult>(`b2b-desk/bookings/${enc(ref)}/cancel${dryRun ? "?dry_run=true" : ""}`, { body, idempotencyKey: key }),
  cancellation: (ref: string) => bkGet<{ cancellation: Cancellation | null }>(`b2b-desk/bookings/${enc(ref)}/cancellation`),
  requestOverride: (ref: string, body: { fee_override_pct: string; waive_reason: string }) =>
    bkSend<Cancellation>(`b2b-desk/bookings/${enc(ref)}/cancellation/override`, { body }),
  approveOverride: (ref: string) => bkSend<Cancellation>(`b2b-desk/bookings/${enc(ref)}/cancellation/override/approve`),
  rejectOverride: (ref: string) => bkSend<Cancellation>(`b2b-desk/bookings/${enc(ref)}/cancellation/override/reject`),
  reduce: (
    ref: string,
    lineRef: string,
    body: Pax & { reason_code: ReduceReason },
    key: string,
    dryRun: boolean,
  ) =>
    bkSend<Reduction>(`b2b-desk/bookings/${enc(ref)}/lines/${enc(lineRef)}/reduce${dryRun ? "?dry_run=true" : ""}`, {
      body,
      idempotencyKey: key,
    }),
  reductions: (ref: string) => bkGet<{ items: Reduction[] }>(`b2b-desk/bookings/${enc(ref)}/reductions`),
  vouchers: (ref: string) => html(`b2b-desk/bookings/${enc(ref)}/vouchers`),
  manifest: (departureId: string) => bkGet<Manifest>(`b2b-desk/departures/${enc(departureId)}/manifest`),
  manifestHtml: (departureId: string) => html(`b2b-desk/departures/${enc(departureId)}/manifest?format=html`),

  tourTerms: (sourceId: string) => bkGet<{ items: TourTerms[] }>(`tour-terms/${enc(sourceId)}`),
  putTourTerms: (
    sourceId: string,
    body: { tenant_id?: string | null; deposit_pct: string; balance_days_before_departure: number },
  ) => bkSend<TourTerms>(`tour-terms/${enc(sourceId)}`, { method: "PUT", body }),
  deleteTourTerms: (sourceId: string, tenantId: string | null) =>
    bkSend<null>(`tour-terms/${enc(sourceId)}${tenantId ? `?tenant_id=${enc(tenantId)}` : ""}`, { method: "DELETE" }),
  ageBands: (sourceId: string, body: { child_age_max: number | null; infant_age_max: number | null }) =>
    bkSend<unknown>(`catalog/trip-plans/${enc(sourceId)}/age-bands`, { method: "PUT", body }),
  supplierCancelFee: (bookingId: string, fee: string | null, currency: string | null) =>
    bkSend<unknown>(`supplier-bookings/${enc(bookingId)}/cancel-fee`, {
      body: { supplier_cancel_fee: fee, supplier_cancel_fee_currency: currency },
    }),
};

export function paxText(p: Pax): string {
  const parts = [`${p.adult} adult${p.adult === 1 ? "" : "s"}`];
  if (p.child) parts.push(`${p.child} child${p.child === 1 ? "" : "ren"}`);
  if (p.infant) parts.push(`${p.infant} infant${p.infant === 1 ? "" : "s"}`);
  return parts.join(", ");
}

export function openBlob(blob: Blob): void {
  const url = URL.createObjectURL(blob);
  window.open(url, "_blank", "noopener");
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
