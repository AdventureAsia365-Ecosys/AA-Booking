// lib/api/operability.ts
// AA-1149 (B13) — the can-operate check of a tour and its admin sale exception (backend routers/operability.py,
// app/catalog/operability.py, app/catalog/sell_exceptions.py). A tour is sold to agencies (tailor-made) only when every
// night has a hotel (N1, or the night is marked "no hotel needed"), every component has a published rate in the next
// 365 days (N2) and every supplier and item is active (N3). A transfer missing on a day the hotel moves (N4) and a guide
// on some days only (N5) are warnings. An administrator can allow the sale for a while despite N1 or N3 (a missing rate
// or an inactive item can never be overridden).
//
//   GET  /booking/catalog/trip-plans/{source_id}/operability            (pricing.read)
//   POST /booking/catalog/trip-plans/{source_id}/sell-exception         (operable_exception.approve: admin)
//   POST /booking/catalog/trip-plans/{source_id}/sell-exception/revoke  (operable_exception.approve)
//   PUT  /booking/catalog/trip-plans/{source_id}/no-hotel-nights        (trip_plan.write; a reason of 20-500 characters)

import { bkGet, bkSend } from "./client";
import type { IsoDateTime } from "./types";

const enc = encodeURIComponent;

export const OPERABLE_STATUSES = ["ready", "warnings", "blocked", "exception"] as const;
export type OperableStatus = (typeof OPERABLE_STATUSES)[number];
export type OperableRule = "N1" | "N2" | "N3" | "N4" | "N5";

export interface OperableFinding {
  rule: OperableRule;
  level: "block" | "warn";
  /** e.g. N1:night:2, N3:supplier:SUP-000045 */
  key: string;
  /** English text from the server (night 2 has no hotel). */
  detail: string;
  overridable: boolean;
  covered: boolean;
}

/** The short form each price readiness row carries. */
export interface OperableSummary {
  status: OperableStatus;
  findings: OperableFinding[];
  findings_total: number;
  exception_until: IsoDateTime | null;
}

export interface SellException {
  id: string;
  rules: OperableRule[];
  findings: string[];
  reason: string;
  expires_at: IsoDateTime;
  created_by: { id: string; name: string | null };
  created_at: IsoDateTime;
}

export interface Operability extends OperableSummary {
  source_id: string;
  trip_plan_id: string;
  days: number;
  no_hotel_nights: number[];
  /** Why the nights were last set (staff text); null until first set. */
  no_hotel_nights_reason: string | null;
  exception: SellException | null;
  /** The rules an exception can cover now, with their finding keys. */
  overridable: Partial<Record<"N1" | "N3", string[]>>;
  not_overridable: string;
  exception_default_days: number;
  exception_max_days: number;
}

export const operabilityApi = {
  get: (sourceId: string) => bkGet<Operability>(`catalog/trip-plans/${enc(sourceId)}/operability`),
  allow: (sourceId: string, body: { rules: ("N1" | "N3")[]; reason: string; days?: number | null }) =>
    bkSend<Operability>(`catalog/trip-plans/${enc(sourceId)}/sell-exception`, { body }),
  revoke: (sourceId: string, reason: string) =>
    bkSend<Operability>(`catalog/trip-plans/${enc(sourceId)}/sell-exception/revoke`, { body: { reason } }),
  noHotelNights: (sourceId: string, nights: number[], reason: string) =>
    bkSend<Operability>(`catalog/trip-plans/${enc(sourceId)}/no-hotel-nights`, { method: "PUT", body: { nights, reason } }),
};
