// lib/api/supplierBills.ts
// AA-682 S4 (bk-030) — typed calls for supplier bills and payouts (backend routers/supplier_bills.py,
// app/ops/supplier_bills.py). Read bill.read (admin, sales, contracting); enter / edit / cancel bill.write and approve
// bill.approve (admin, contracting; never the person who entered the bill nor the one who last changed its amounts or
// lines, facts_edited_by); payouts payout.write (admin; never on a bill they entered or last changed). Money is a decimal string, never a float.
//
//   GET    /booking/supplier-bills?status&supplier_id&case&overdue&due_before&q&limit&offset
//   GET    /booking/supplier-bills/summary                                  KPIs
//   GET    /booking/supplier-bills/billable?supplier_id&case&q              confirmed bookings a bill can name
//   POST   /booking/supplier-bills                     (Idempotency-Key)    enter a bill (received)
//   GET    /booking/supplier-bills/{id} · PATCH (received: facts + lines; later: notes only)
//   POST   /booking/supplier-bills/{id}/approve | cancel
//   POST   /booking/supplier-bills/{id}/payouts        (Idempotency-Key)    record a payout
//   POST   /booking/supplier-payouts/{id}/void
//   GET    /booking/trip-cases/{ref}/supplier-costs                         cost vs billed (needs pricing.read too)
//
// "Paid" is set by the database from the payouts only; a bill is never deleted (cancel with a reason).

import { bkGet, bkSend } from "./client";
import type { OpsFile } from "./supplierBookings";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";
import type { Page, PaymentMethod, Person } from "./billing";

const enc = encodeURIComponent;

export type SupplierBillStatus = "received" | "approved" | "paid" | "cancelled";
export type PayoutState = "live" | "voided";
export type ApproveBlocker = "not_received" | "permission" | "maker_checker" | "no_lines" | "lines_mismatch";

export const APPROVE_BLOCKER_LABEL: Record<ApproveBlocker, string> = {
  not_received: "Only a received bill is approved.",
  permission: "Approving needs the contracting or admin role.",
  maker_checker: "You entered this bill or last changed its amounts or lines: another person approves it.",
  no_lines: "Match the bill to at least one confirmed supplier booking first.",
  lines_mismatch: "The lines do not add up to the bill amount yet.",
};

export interface SupplierBill {
  id: string;
  code: string;
  supplier: { id: string; name: string };
  trip_case: { id: string; code: string; channel: "b2c" | "b2b" } | null;
  bill_number: string;
  currency: string;
  amount: DecimalString;
  issue_date: IsoDate;
  due_date: IsoDate;
  status: SupplierBillStatus;
  notes: string | null;
  document_file_id: string | null;
  created_by: Person | null;
  /** The last person who set the money facts or the lines; never the approver nor the payout recorder. */
  facts_edited_by: Person | null;
  approved_by: Person | null;
  approved_at: IsoDateTime | null;
  paid_at: IsoDateTime | null;
  cancelled_by: Person | null;
  cancelled_at: IsoDateTime | null;
  cancel_reason: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  paid_amount: DecimalString;
  balance: DecimalString;
  live_payouts: number;
  line_count: number;
  lines_amount: DecimalString;
  overdue: boolean;
  approve_blockers: ApproveBlocker[];
  actions: { edit: boolean; attach: boolean; approve: boolean; cancel: boolean; pay: boolean };
}

export interface SupplierBillLine {
  supplier_booking_id: string;
  amount: DecimalString;
  code: string;
  status: string;
  item_description: string;
  service_date: IsoDate | null;
  nights: number | null;
  pax: number | null;
  net_amount: DecimalString | null;
  net_currency: string | null;
  trip_case_id: string;
  trip_case_code: string;
  trip_case_channel: "b2c" | "b2b";
}

export interface SupplierPayout {
  id: string;
  code: string;
  bill_id: string;
  amount: DecimalString;
  currency: string;
  method: PaymentMethod;
  paid_on: IsoDate;
  reference: string | null;
  note: string | null;
  evidence_file_id: string | null;
  state: PayoutState;
  recorded_by: Person | null;
  recorded_at: IsoDateTime;
  voided_by: Person | null;
  voided_at: IsoDateTime | null;
  void_reason: string | null;
  files: OpsFile[];
  needs_evidence: boolean;
  actions: { void: boolean; attach: boolean };
}

export interface SupplierBillDetail extends SupplierBill {
  lines: SupplierBillLine[];
  payouts: SupplierPayout[];
  files: OpsFile[];
}

export interface BillableBooking {
  id: string;
  code: string;
  item_description: string;
  service_date: IsoDate | null;
  nights: number | null;
  pax: number | null;
  net_amount: DecimalString | null;
  net_currency: string | null;
  confirmation_ref: string | null;
  trip_case_id: string;
  trip_case_code: string;
  trip_case_channel: "b2c" | "b2b";
  billed: { currency: string; amount: DecimalString }[];
}

export interface BillKpi {
  count: number;
  by_currency: { currency: string; amount: DecimalString }[];
}

export interface SupplierBillsSummary {
  to_approve: BillKpi;
  to_pay: BillKpi;
  overdue: BillKpi;
  paid_this_month: BillKpi;
  bills: Record<SupplierBillStatus, number>;
  month_start: IsoDate;
  today: IsoDate;
}

export interface CaseCostLine {
  supplier_booking: { id: string; code: string; status: string };
  supplier: { id: string; name: string };
  item_description: string;
  service_date: IsoDate | null;
  net_amount: DecimalString | null;
  net_currency: string | null;
  billed: { currency: string; amount: DecimalString; bills: string[] }[];
  variance: DecimalString | null;
  currency_mismatch: boolean;
  unbilled: boolean;
}

export interface CaseCosts {
  trip_case: { id: string; code: string; channel: "b2c" | "b2b" };
  lines: CaseCostLine[];
  /** variance = the sum of the line variances in that currency (billed bookings only); null when none compares. */
  totals: { currency: string; net: DecimalString; billed: DecimalString; variance: DecimalString | null }[];
}

export interface BillLineIn {
  supplier_booking_id: string;
  amount: DecimalString;
}

export interface NewSupplierBill {
  supplier_id: string;
  trip_case?: string | null;
  bill_number: string;
  currency: string;
  amount: DecimalString;
  issue_date: IsoDate;
  due_date: IsoDate;
  notes?: string | null;
  lines: BillLineIn[];
}

export interface NewPayout {
  amount: DecimalString;
  method: PaymentMethod;
  paid_on: IsoDate;
  reference?: string | null;
  note?: string | null;
  allow_duplicate_reference?: boolean;
}

export interface BillQuery {
  status?: string;
  supplier_id?: string;
  case?: string;
  overdue?: boolean;
  due_before?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export const supplierBillsApi = {
  list: (q: BillQuery) => bkGet<Page<SupplierBill>>("supplier-bills", { ...q }),
  summary: () => bkGet<SupplierBillsSummary>("supplier-bills/summary"),
  billable: (q: { supplier_id: string; case?: string; q?: string }) => bkGet<{ items: BillableBooking[] }>("supplier-bills/billable", q),
  get: (id: string) => bkGet<SupplierBillDetail>(`supplier-bills/${enc(id)}`),
  create: (body: NewSupplierBill, idempotencyKey: string) =>
    bkSend<SupplierBillDetail & { replayed: boolean }>("supplier-bills", { body, idempotencyKey }),
  update: (id: string, body: Partial<Omit<NewSupplierBill, "supplier_id" | "trip_case">>) =>
    bkSend<SupplierBillDetail & { changed: string[] }>(`supplier-bills/${enc(id)}`, { method: "PATCH", body }),
  approve: (id: string) => bkSend<SupplierBillDetail>(`supplier-bills/${enc(id)}/approve`),
  cancel: (id: string, reason: string) => bkSend<SupplierBillDetail>(`supplier-bills/${enc(id)}/cancel`, { body: { reason } }),
  pay: (id: string, body: NewPayout, idempotencyKey: string) =>
    bkSend<SupplierPayout & { bill: SupplierBillDetail; replayed: boolean }>(`supplier-bills/${enc(id)}/payouts`, { body, idempotencyKey }),
  voidPayout: (id: string, reason: string) =>
    bkSend<SupplierPayout & { bill: SupplierBillDetail }>(`supplier-payouts/${enc(id)}/void`, { body: { reason } }),
  caseCosts: (ref: string) => bkGet<CaseCosts>(`trip-cases/${enc(ref)}/supplier-costs`),
  uploadDocument: (body: { entity_id: string; file_name: string; content_type: string; data_base64: string }) =>
    bkSend<OpsFile>("ops-files", { body: { ...body, kind: "supplier_bill", entity_type: "supplier_bill" } }),
  uploadPayoutEvidence: (body: { entity_id: string; file_name: string; content_type: string; data_base64: string }) =>
    bkSend<OpsFile>("ops-files", { body: { ...body, kind: "payout_evidence", entity_type: "supplier_payout" } }),
  removeFile: (fileId: string, reason: string) => bkSend<OpsFile>(`ops-files/${enc(fileId)}/remove`, { body: { reason } }),
  download: (fileId: string) => bkGet<{ url: string; expires_in: number }>(`ops-files/${enc(fileId)}/download`),
};
