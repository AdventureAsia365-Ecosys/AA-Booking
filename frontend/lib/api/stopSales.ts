// lib/api/stopSales.ts
// AA-1159 — stop-sales on supplier contracts waiting for a second person (backend routers/supply_model.py,
// app/supply/restrictions.py; AA-1145 bk-060). A stop-sale counts from the moment it is entered (pending); a second
// person approves or rejects it, and ending an approved one also takes a second person (maker-checker, 403
// maker_checker). Reading needs supply.read; deciding needs stop_sale.write + stop_sale.approve, approving an end
// stop_sale.approve.
//
//   GET  /booking/supply/restrictions?kind=stop_sale&review=waiting|me&limit=   the review queue, longest waiting first
//   POST /booking/supply/stop-sales/{id}/decision   {action: approve | reject, reason?}   (reject needs a reason)
//   POST /booking/supply/stop-sales/{id}/end         approve the end request of an approved stop-sale

import { bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

/** `waiting`: every stop-sale waiting for a second person; `me`: those the caller may decide (not entered nor asked
 *  to end by them), the view the My work tile counts. */
export type StopSaleReview = "waiting" | "me";

export interface StopSale {
  id: string;
  contract_id: string;
  contract_title: string;
  contract_status: string;
  supplier_id: string;
  supplier_code: string;
  supplier_name: string;
  component_id: string;
  hotel_code: string | null;
  hotel_name: string | null;
  room_type_name: string | null;
  service_code: string | null;
  service_title: string | null;
  date_from: IsoDate;
  date_to: IsoDate;
  note: string | null;
  /** pending (waits for review) or approved (with an end request waiting). */
  status: "pending" | "approved" | "rejected" | "withdrawn";
  created_by: string;
  created_by_name: string | null;
  created_at: IsoDateTime;
  end_requested_by: string | null;
  end_requested_by_name: string | null;
  end_requested_at: IsoDateTime | null;
  end_reason: string | null;
}

export interface StopSalePage {
  items: StopSale[];
  total: number;
}

/** The stop-sale waits for a review of itself (pending) or of its end request (approved, end asked). */
export function waitsFor(s: StopSale): "review" | "end" {
  return s.status === "pending" ? "review" : "end";
}

/** The person whose request waits: the maker of a pending stop-sale, the requester of an end. */
export function requester(s: StopSale): { id: string; name: string } {
  const id = waitsFor(s) === "review" ? s.created_by : (s.end_requested_by ?? "");
  const name = waitsFor(s) === "review" ? s.created_by_name : s.end_requested_by_name;
  return { id, name: name || id };
}

/** What the stop-sale closes: a hotel room type or a service. */
export function componentLabel(s: StopSale): string {
  if (s.hotel_name) return s.room_type_name ? `${s.hotel_name} · ${s.room_type_name}` : s.hotel_name;
  return s.service_title ?? s.component_id;
}

export const stopSalesApi = {
  list: (review: StopSaleReview, limit = 200) =>
    bkGet<StopSalePage>("supply/restrictions", { kind: "stop_sale", review, limit }),
  approve: (id: string) => bkSend<StopSale>(`supply/stop-sales/${enc(id)}/decision`, { body: { action: "approve" } }),
  reject: (id: string, reason: string) =>
    bkSend<StopSale>(`supply/stop-sales/${enc(id)}/decision`, { body: { action: "reject", reason } }),
  approveEnd: (id: string) => bkSend<StopSale>(`supply/stop-sales/${enc(id)}/end`),
};
