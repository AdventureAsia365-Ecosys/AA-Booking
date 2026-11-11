// lib/api/departureBoard.ts
// AA-1073 rest (bk-048) — typed calls for the departure board, the ops sheet of a departure, departure fees charged to
// agencies and per-departure price adjustments (backend routers/b2b_desk.py, app/b2b/departure_board.py,
// departure_ops.py, departure_fees.py, price_adjustments.py).
//
//   GET    /booking/b2b-desk/departure-board?from&to&country&source_id&status&at_risk&limit&offset   (b2b_desk.read)
//   GET    /booking/b2b-desk/departure-board/{id}         seats by agency, ops sheet, fees, price adjustments
//   PATCH  /booking/b2b-desk/departures/{id}/ops          meeting point / time / note (ops.write)
//   POST   /booking/b2b-desk/departures/{id}/staff        guide, tour leader or driver (ops.write)
//   DELETE /booking/b2b-desk/departure-staff/{id}
//   GET    /booking/b2b-desk/departure-fees?departure_id&partner_id&status
//   POST   /booking/b2b-desk/departures/{id}/fees         propose a fee (b2b_desk.write)
//   POST   /booking/b2b-desk/departure-fees/{id}/approve | /reject   another person (b2b_desk.approve)
//   POST   /booking/b2b-desk/departures/{id}/price-adjustments        propose (pricing.write)
//   POST   /booking/b2b-desk/price-adjustments/{id}/approve | /end    approve (another person) / reject or withdraw
//
// Seats are counted at read time (view v_departure_occupancy): never stored. A meeting point or time change tells the
// agencies on the departure (event departure.updated); the ops note and the staff never leave AA.

import { bkGet, bkSend } from "./client";
import type { DepartureStatus } from "./departures";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export interface PersonRef {
  id: string;
  name: string | null;
}

export interface EarlyBooking {
  days: number;
  pts: DecimalString;
  until: IsoDate;
}

export interface BoardPrice {
  hint: { amount: DecimalString; currency: string; basis: string | null } | null;
  adjustment: { id: string; adjust_pct: DecimalString | null; amount: DecimalString | null; currency: string | null } | null;
  early_booking: EarlyBooking | null;
}

export interface BoardDeparture {
  id: string;
  trip_plan_id: string;
  source_id: string;
  tour_title: string | null;
  country: string | null;
  start_date: IsoDate;
  status: DepartureStatus;
  capacity: number;
  held: number;
  sold: number;
  allotted: number;
  allotment_unused: number;
  free: number;
  occupancy_pct: DecimalString | null;
  min_to_operate: number;
  at_risk: boolean;
  open_incident: { id: string; incident_code: string } | null;
  agencies_count: number;
  auto_confirm: boolean;
  meeting_point: string | null;
  meeting_time: string | null;
  price: BoardPrice;
}

export interface BoardTotals {
  departures: number;
  capacity: number;
  held: number;
  sold: number;
  free: number;
  at_risk: number;
  incidents: number;
}

export interface BoardPage {
  items: BoardDeparture[];
  total: number;
  totals: BoardTotals;
  limit: number;
  offset: number;
  from: IsoDate;
  to: IsoDate;
  /** Countries and trip plans of every departure in the window (filter options, not only the filtered page). */
  countries: string[];
  plans: { source_id: string; title: string | null }[];
}

export interface BoardQuery {
  from?: string;
  to?: string;
  country?: string;
  source_id?: string;
  status?: string;
  at_risk?: boolean;
  limit?: number;
  offset?: number;
}

export interface AgencySeats {
  partner_id: string;
  agency_name: string;
  held: number;
  sold: number;
  cases: { id: string; code: string }[];
  allotment: { id: string; seats: number; state: "in_force" | "released"; release_date: IsoDate; used: number; unused: number } | null;
}

export type StaffRole = "guide" | "tour_leader" | "driver";

export interface StaffAssignment {
  id: string;
  role: StaffRole;
  staff: PersonRef | null;
  supplier: { id: string; code: string; name: string } | null;
  assigned_by: PersonRef;
  assigned_at: IsoDateTime;
}

export interface OpsSheet {
  meeting_point: string | null;
  meeting_time: string | null;
  ops_note: string | null;
  staff: StaffAssignment[];
  staff_options?: { id: string; name: string | null }[];
  supplier_options?: { id: string; code: string; name: string }[];
}

export type FeeReason = "min_shortfall" | "allotment_unreleased" | "other";
export type FeeStatus = "pending" | "approved" | "rejected";

export interface DepartureFee {
  id: string;
  departure_id: string;
  start_date: IsoDate;
  partner: { id: string; agency_name: string };
  reason: FeeReason;
  seats: number;
  price_per_seat: DecimalString;
  currency: string;
  amount: DecimalString;
  note: string | null;
  status: FeeStatus;
  created_by: PersonRef | null;
  created_at: IsoDateTime;
  decided_by: PersonRef | null;
  decided_at: IsoDateTime | null;
  billed: boolean;
  invoice_number: string | null;
  can_decide: boolean;
}

export interface FeeSuggestion {
  reason: FeeReason;
  partner_id: string | null;
  seats: number;
}

export interface NewFee {
  partner_id: string;
  reason: FeeReason;
  seats: number;
  price_per_seat: string;
  currency: string;
  note?: string | null;
}

export type AdjustmentState = "pending" | "in_force" | "ended";

export interface PriceAdjustment {
  id: string;
  departure_id: string;
  adjust_pct: DecimalString | null;
  amount: DecimalString | null;
  currency: string | null;
  reason: string;
  state: AdjustmentState;
  replaces_id: string | null;
  created_by: PersonRef | null;
  created_at: IsoDateTime;
  approved_by: PersonRef | null;
  approved_at: IsoDateTime | null;
  ended_by: PersonRef | null;
  ended_at: IsoDateTime | null;
  end_reason: "replaced" | "withdrawn" | "rejected" | null;
  can_approve: boolean;
}

export interface NewAdjustment {
  adjust_pct?: string;
  amount?: string;
  currency?: string;
  reason: string;
}

export interface BoardDetail {
  departure: BoardDeparture;
  agencies: AgencySeats[];
  ops: OpsSheet;
  fees: DepartureFee[];
  fee_suggestions: FeeSuggestion[];
  price_adjustments: PriceAdjustment[];
  partner_options: { id: string; agency_name: string }[];
}

export const STAFF_ROLE_LABEL: Record<StaffRole, string> = { guide: "Guide", tour_leader: "Tour leader", driver: "Driver" };

export const FEE_REASON_LABEL: Record<FeeReason, string> = {
  min_shortfall: "Minimum not reached",
  allotment_unreleased: "Allotted seats not released",
  other: "Other",
};

export const FEE_STATUS: Record<FeeStatus, { label: string; tone: "warning" | "success" | "neutral" }> = {
  pending: { label: "Waiting for approval", tone: "warning" },
  approved: { label: "Charged", tone: "success" },
  rejected: { label: "Rejected", tone: "neutral" },
};

// Pure helpers (calendar weeks, month moves, labels) live import-free in ui/booking/departureBoard.ts (node tests).
export { adjustmentLabel, monthRange, monthWeeks, occupancy, shiftMonth } from "@/ui/booking/departureBoard";

export const departureBoardApi = {
  board: (q: BoardQuery) =>
    bkGet<BoardPage>("b2b-desk/departure-board", {
      ...q,
      at_risk: q.at_risk ? "true" : undefined,
      limit: q.limit,
      offset: q.offset,
    }),
  detail: (id: string) => bkGet<BoardDetail>(`b2b-desk/departure-board/${enc(id)}`),
  updateOps: (id: string, body: { meeting_point?: string | null; meeting_time?: string | null; ops_note?: string | null }) =>
    bkSend<OpsSheet>(`b2b-desk/departures/${enc(id)}/ops`, { method: "PATCH", body }),
  assign: (id: string, body: { role: StaffRole; staff_user_id?: string; supplier_id?: string }) =>
    bkSend<StaffAssignment>(`b2b-desk/departures/${enc(id)}/staff`, { body }),
  unassign: (assignmentId: string) => bkSend<{ removed: boolean }>(`b2b-desk/departure-staff/${enc(assignmentId)}`, { method: "DELETE" }),
  fees: (q: { departure_id?: string; partner_id?: string; status?: FeeStatus; limit?: number; offset?: number }) =>
    bkGet<{ items: DepartureFee[]; total: number; pending: number }>("b2b-desk/departure-fees", q),
  proposeFee: (departureId: string, body: NewFee) => bkSend<DepartureFee>(`b2b-desk/departures/${enc(departureId)}/fees`, { body }),
  decideFee: (feeId: string, approve: boolean) =>
    bkSend<DepartureFee>(`b2b-desk/departure-fees/${enc(feeId)}/${approve ? "approve" : "reject"}`),
  proposeAdjustment: (departureId: string, body: NewAdjustment) =>
    bkSend<PriceAdjustment>(`b2b-desk/departures/${enc(departureId)}/price-adjustments`, { body }),
  approveAdjustment: (id: string) => bkSend<PriceAdjustment>(`b2b-desk/price-adjustments/${enc(id)}/approve`),
  endAdjustment: (id: string) => bkSend<PriceAdjustment>(`b2b-desk/price-adjustments/${enc(id)}/end`),
};
