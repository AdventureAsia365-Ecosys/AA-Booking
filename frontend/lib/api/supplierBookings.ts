// lib/api/supplierBookings.ts
// AA-682 S1 (bk-026) — typed calls for supplier bookings and operations documents (backend
// routers/supplier_bookings.py, app/ops/supplier_bookings.py, app/ops/files.py). Read ops.read, write ops.write (admin,
// sales, contracting); confirming / declining a booking of an on-request B2B line also needs b2b_desk.write.
//
//   GET   /booking/trip-cases/{ref}/supplier-bookings                 bookings, supply state, items still to book
//   POST  /booking/trip-cases/{ref}/supplier-bookings/generate        one pending booking per approved supply item
//   POST  /booking/trip-cases/{ref}/supplier-bookings                 add one by hand (extra item or replacement)
//   GET   /booking/supplier-bookings?supplier_id&status&from&to       the supplier Bookings tab
//   GET   /booking/supplier-bookings/summary                          dashboard tile
//   PATCH /booking/supplier-bookings/{id}                             facts while pending / requested, notes always
//   POST  /booking/supplier-bookings/{id}/request | confirm | decline | cancel | rerequest
//   POST  /booking/ops-files  ·  GET /booking/ops-files/{id}/download  ·  POST /booking/ops-files/{id}/remove
//
// "Confirmed & Booked" is the supply state `booked` (design D1): a computed badge, never a case status.

import { bkGet, bkSend } from "./client";
import { DECLINE_CODES, DECLINE_LABEL, type DeclineCode } from "./b2bDesk";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

export { DECLINE_CODES, DECLINE_LABEL, type DeclineCode };

const enc = encodeURIComponent;

export type SupplierBookingStatus = "pending" | "requested" | "confirmed" | "declined" | "cancelled";
export type SupplyStateName = "none" | "confirming" | "booked";

export interface SupplyState {
  state: SupplyStateName;
  items_to_book: number;
  items_booked: number;
  live: number;
  confirmed: number;
  waiting: number;
  declined: number;
}

export interface OpsFile {
  id: string;
  kind: "supplier_voucher" | "supplier_confirmation" | "payment_evidence" | "supplier_bill" | "payout_evidence";
  entity_type: string;
  entity_id: string;
  content_type: string;
  bytes: number;
  file_name: string;
  status: "active" | "removed";
  uploaded_by: { id: string; name: string | null } | null;
  created_at: IsoDateTime;
}

export interface SupplierBooking {
  id: string;
  code: string;
  trip_case: { id: string; code: string; status: string };
  channel: "b2c" | "b2b";
  version_item_id: string | null;
  item_position: number | null;
  supplier: { id: string; code: string; name: string };
  hotel_room_type_id: string | null;
  service_id: string | null;
  item_description: string;
  service_date: IsoDate | null;
  nights: number | null;
  pax: number | null;
  net_amount: DecimalString | null;
  net_currency: string | null;
  status: SupplierBookingStatus;
  live: boolean;
  requested_at: IsoDateTime | null;
  requested_by: { id: string; name: string | null } | null;
  confirmed_at: IsoDateTime | null;
  confirmed_by: { id: string; name: string | null } | null;
  confirmation_ref: string | null;
  declined_at: IsoDateTime | null;
  decline_code: DeclineCode | null;
  cancelled_at: IsoDateTime | null;
  cancel_reason: string | null;
  voucher_file_id: string | null;
  b2b_line: { hold_id: string; line_ref: string; status: string } | null;
  notes: string | null;
  files: OpsFile[];
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface UnbookedItem {
  version_item_id: string;
  position: number;
  title: string;
  supply_label: string | null;
  b2b_line_status: string | null;
  bookable: boolean;
  skip_reason: "booked" | "no_supplier" | "b2b_line_closed" | null;
}

export interface CaseSupplierBookings {
  trip_case: { id: string; code: string; status: string; channel: "b2c" | "b2b" };
  supply: SupplyState;
  items: SupplierBooking[];
  unbooked_items: UnbookedItem[];
  documents_allowed: boolean;
  created?: number;
}

export interface SupplierBookingPage {
  items: SupplierBooking[];
  total: number;
  limit: number;
  offset: number;
}

export interface SupplierBookingSummary {
  requested: number;
  overdue: number;
  pending: number;
  answer_days: number;
}

export interface NewSupplierBooking {
  supplier_id: string;
  version_item_id?: string | null;
  hotel_room_type_id?: string | null;
  service_id?: string | null;
  item_description?: string | null;
  service_date?: IsoDate | null;
  nights?: number | null;
  pax?: number | null;
  net_amount?: string | null;
  net_currency?: string | null;
  notes?: string | null;
}

export const SUPPLIER_BOOKING_STATUSES: SupplierBookingStatus[] = ["pending", "requested", "confirmed", "declined", "cancelled"];

export const supplierBookingsApi = {
  forCase: (ref: string) => bkGet<CaseSupplierBookings>(`trip-cases/${enc(ref)}/supplier-bookings`),
  generate: (ref: string) => bkSend<CaseSupplierBookings>(`trip-cases/${enc(ref)}/supplier-bookings/generate`),
  create: (ref: string, body: NewSupplierBooking) =>
    bkSend<SupplierBooking>(`trip-cases/${enc(ref)}/supplier-bookings`, { body }),
  list: (q: { supplier_id?: string; status?: string; from?: string; to?: string; limit?: number; offset?: number }) =>
    bkGet<SupplierBookingPage>("supplier-bookings", q),
  summary: () => bkGet<SupplierBookingSummary>("supplier-bookings/summary"),
  update: (id: string, body: Partial<NewSupplierBooking>) =>
    bkSend<SupplierBooking & { changed: string[] }>(`supplier-bookings/${enc(id)}`, { method: "PATCH", body }),
  request: (id: string) => bkSend<SupplierBooking>(`supplier-bookings/${enc(id)}/request`),
  confirm: (id: string, confirmation_ref: string, voucher_file_id?: string | null) =>
    bkSend<SupplierBooking>(`supplier-bookings/${enc(id)}/confirm`, {
      body: { confirmation_ref, ...(voucher_file_id ? { voucher_file_id } : {}) },
    }),
  decline: (id: string, decline_code: DeclineCode) =>
    bkSend<SupplierBooking>(`supplier-bookings/${enc(id)}/decline`, { body: { decline_code } }),
  cancel: (id: string, reason: string) => bkSend<SupplierBooking>(`supplier-bookings/${enc(id)}/cancel`, { body: { reason } }),
  rerequest: (id: string, reason: string) =>
    bkSend<SupplierBooking>(`supplier-bookings/${enc(id)}/rerequest`, { body: { reason } }),
  upload: (body: {
    kind: "supplier_voucher" | "supplier_confirmation";
    entity_id: string;
    file_name: string;
    content_type: string;
    data_base64: string;
  }) => bkSend<OpsFile>("ops-files", { body: { ...body, entity_type: "supplier_booking" } }),
  download: (fileId: string) => bkGet<{ url: string; expires_in: number }>(`ops-files/${enc(fileId)}/download`),
  removeFile: (fileId: string, reason: string) => bkSend<OpsFile>(`ops-files/${enc(fileId)}/remove`, { body: { reason } }),
};
