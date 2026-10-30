// lib/api/billing.ts
// AA-682 S3 (bk-028) — typed calls for invoices, the payment schedule and manual payments (backend routers/billing.py,
// app/ops/invoices.py, app/ops/payments.py). Read billing.read, write billing.write (admin, sales), verify
// payment.verify (admin, never the person who recorded the payment). Money is a decimal string, never a float.
//
//   GET    /booking/invoices?status&overdue&case&q&from&to&limit&offset     invoices (not deleted), newest first
//   GET    /booking/invoices/{number}                                      lines, schedule (derived state), payments
//   GET    /booking/trip-cases/{ref}/billing                                case Billing tab
//   POST   /booking/trip-cases/{ref}/invoices          (Idempotency-Key)   draft from the approved version
//   PATCH  /booking/invoices/{number} · DELETE (draft, soft)
//   POST   /booking/invoices/{number}/send | cancel | amend | remind
//   POST   /booking/invoices/{number}/payments         (Idempotency-Key)   record a receipt or a refund
//   GET    /booking/payments?state&kind&method&invoice&case&q&from&to       payments, newest first
//   GET    /booking/payments/{ref} · PATCH (reference, note before verify)
//   POST   /booking/payments/{ref}/verify | void
//   GET    /booking/billing/summary                                         KPIs
//
// No payment provider: staff mark money by hand, a second person verifies (design D6). An invoice is a proforma
// payment request; the customer sees it in My Trips (no e-mail).

import { bkGet, bkSend } from "./client";
import type { OpsFile } from "./supplierBookings";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type InvoiceStatus = "draft" | "sent" | "partially_paid" | "paid" | "cancelled";
export type PaymentState = "recorded" | "verified" | "voided";
export type PaymentKind = "receipt" | "refund";
export type PaymentMethod = "bank_transfer" | "card_offline" | "cash" | "other";
export type ScheduleKind = "deposit" | "second" | "final" | "other";
export type ScheduleState = "scheduled" | "due" | "overdue" | "paid" | "cancelled";

export const PAYMENT_METHOD_LABEL: Record<PaymentMethod, string> = {
  bank_transfer: "Bank transfer",
  card_offline: "Card (offline terminal)",
  cash: "Cash",
  other: "Other",
};
export const SCHEDULE_KIND_LABEL: Record<ScheduleKind, string> = {
  deposit: "Deposit",
  second: "Second payment",
  final: "Balance",
  other: "Other",
};
export const CASE_BLOCK_REASON: Record<string, string> = {
  b2b_no_invoice: "A B2B trip has no customer invoice: the agency settles outside this module.",
  invoice_exists: "This trip already has an invoice.",
  case_not_invoiceable: "Invoices are made for a confirmed trip.",
  no_approved_version: "Approve a proposal version first: the invoice starts from its total.",
  version_not_priced: "The approved version has no price yet: price it first.",
};

export interface Person {
  id: string;
  name: string | null;
}

export interface InvoiceSummary {
  id: string;
  invoice_number: string;
  status: InvoiceStatus;
  document_type: "proforma";
  currency: string;
  total_amount: DecimalString;
  net_paid: DecimalString;
  verified_received: DecimalString;
  verified_refunded: DecimalString;
  unverified_amount: DecimalString;
  unverified_count: number;
  balance: DecimalString | null;
  next_due_date: IsoDate | null;
  overdue_amount: DecimalString;
  oldest_overdue_date: IsoDate | null;
  overdue: boolean;
  bill_to_name: string | null;
  bill_to_email: string | null;
  bill_to_address: string | null;
  notes: string | null;
  terms: string | null;
  issued_at: IsoDateTime | null;
  sent_by: Person | null;
  cancelled_at: IsoDateTime | null;
  cancel_reason: string | null;
  created_by: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  trip_case: { id: string; code: string; status: string; channel: "b2c" | "b2b"; start_date: IsoDate | null };
  customer: { id: string; code: string; name: string | null } | null;
  version: { id: string; number: number; title: string | null; fx_rate_locked: DecimalString | null };
  /** What this staff member may do (list rows and the detail; the staff list's row menu reads `cancel`). */
  actions?: InvoiceActions;
}

export interface InvoiceLine {
  id: string;
  position: number;
  kind: "version_total" | "adjustment";
  description: string;
  quantity: DecimalString;
  unit_price: DecimalString;
  amount: DecimalString;
  reason: string | null;
  created_by: string | null;
  created_at: IsoDateTime;
}

export interface ScheduleRow {
  id: string;
  seq: number;
  kind: ScheduleKind;
  amount: DecimalString;
  due_date: IsoDate;
  covered_amount: DecimalString;
  remaining_amount: DecimalString;
  state: ScheduleState;
  days_overdue: number | null;
}

export interface Payment {
  id: string;
  ref: string;
  kind: PaymentKind;
  amount: DecimalString;
  currency: string;
  method: PaymentMethod;
  received_on: IsoDate;
  reference: string | null;
  note: string | null;
  received_amount: DecimalString | null;
  received_currency: string | null;
  evidence_file_id: string | null;
  state: PaymentState;
  recorded_by: Person | null;
  recorded_at: IsoDateTime;
  verified_by: Person | null;
  verified_at: IsoDateTime | null;
  voided_by: Person | null;
  voided_at: IsoDateTime | null;
  void_reason: string | null;
  invoice: {
    id: string;
    number: string;
    status: InvoiceStatus;
    currency?: string;
    total_amount?: DecimalString;
    net_paid?: DecimalString;
    balance?: DecimalString | null;
  };
  /** AA-996: the payment detail adds the trip facts (Related trip card); list rows carry id and code only. */
  trip_case: {
    id: string;
    code: string;
    status?: string;
    trip_style?: string | null;
    start_date?: IsoDate | null;
    end_date?: IsoDate | null;
    pax?: number | null;
    channel?: "b2c" | "b2b";
  };
  /** AA-996: the invoice's customer (null on a B2B trip); e-mail and phone on the payment detail with customer.read. */
  customer?: { id: string; code: string | null; name: string | null; email?: string | null; phone?: string | null } | null;
  files: OpsFile[];
  needs_evidence: boolean;
  actions: { edit: boolean; attach: boolean; verify: boolean; void: boolean };
}

export interface InvoiceActions {
  edit: boolean;
  send: boolean;
  delete: boolean;
  amend: boolean;
  cancel: boolean;
  remind: boolean;
  record_payment: boolean;
}

export interface InvoiceDetail extends InvoiceSummary {
  items: InvoiceLine[];
  schedule: ScheduleRow[];
  payments: Payment[];
  issuer: { name?: string; address?: string } | null;
  payment_instructions: string | null;
  actions: InvoiceActions;
}

export interface CaseBilling {
  trip_case: { id: string; code: string; status: string; channel: "b2c" | "b2b"; start_date: IsoDate | null };
  invoiceable: boolean;
  reason: string | null;
  version: { id: string; number: number; title: string | null; total_price: DecimalString | null; currency: string | null } | null;
  defaults: {
    currency: string;
    version_total: DecimalString;
    schedule: { seq: number; kind: ScheduleKind; amount: DecimalString; due_date: IsoDate }[];
  } | null;
  invoice: InvoiceDetail | null;
  history: InvoiceSummary[];
}

export interface Kpi {
  count: number;
  by_currency: { currency: string; amount: DecimalString }[];
  usd: DecimalString;
  unconverted: number;
}

export interface BillingSummary {
  outstanding: Kpi;
  overdue: Kpi;
  received_this_month: Kpi;
  awaiting_verification: Kpi;
  invoices: Record<InvoiceStatus, number>;
  month_start: IsoDate;
  today: IsoDate;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdjustmentIn {
  description: string;
  quantity?: DecimalString;
  unit_price: DecimalString;
  reason: string;
}

export interface ScheduleRowIn {
  kind: ScheduleKind;
  amount: DecimalString;
  due_date: IsoDate;
}

export interface NewInvoice {
  adjustments: AdjustmentIn[];
  schedule?: ScheduleRowIn[] | null;
  notes?: string | null;
  terms?: string | null;
  send_now: boolean;
}

export interface NewPayment {
  kind: PaymentKind;
  amount: DecimalString;
  method: PaymentMethod;
  received_on: IsoDate;
  reference?: string | null;
  note?: string | null;
  received_amount?: DecimalString | null;
  received_currency?: string | null;
  allow_duplicate_reference?: boolean;
}

/** One key per user action (a retry of the same action reuses it; the API returns the stored row). */
export function newIdempotencyKey(prefix: string): string {
  const rand = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${rand}`;
}

export const billingApi = {
  invoices: (q: {
    status?: string;
    overdue?: boolean;
    case?: string;
    q?: string;
    from?: string;
    to?: string;
    limit?: number;
    offset?: number;
  }) => bkGet<Page<InvoiceSummary>>("invoices", q),
  invoice: (number: string) => bkGet<InvoiceDetail>(`invoices/${enc(number)}`),
  forCase: (ref: string) => bkGet<CaseBilling>(`trip-cases/${enc(ref)}/billing`),
  summary: () => bkGet<BillingSummary>("billing/summary"),
  create: (ref: string, body: NewInvoice, idempotencyKey: string) =>
    bkSend<InvoiceDetail & { replayed: boolean }>(`trip-cases/${enc(ref)}/invoices`, { body, idempotencyKey }),
  update: (number: string, body: Partial<Pick<NewInvoice, "adjustments" | "schedule" | "notes" | "terms">>) =>
    bkSend<InvoiceDetail & { changed: string[] }>(`invoices/${enc(number)}`, { method: "PATCH", body }),
  remove: (number: string) => bkSend<{ deleted: boolean }>(`invoices/${enc(number)}`, { method: "DELETE" }),
  send: (number: string) => bkSend<InvoiceDetail>(`invoices/${enc(number)}/send`),
  cancel: (number: string, reason: string) => bkSend<InvoiceDetail>(`invoices/${enc(number)}/cancel`, { body: { reason } }),
  amend: (number: string, body: { adjustments: AdjustmentIn[]; schedule?: ScheduleRowIn[] }) =>
    bkSend<InvoiceDetail>(`invoices/${enc(number)}/amend`, { body }),
  remind: (number: string) => bkSend<InvoiceDetail & { reminded: boolean }>(`invoices/${enc(number)}/remind`),
  record: (number: string, body: NewPayment, idempotencyKey: string) =>
    bkSend<Payment & { replayed: boolean }>(`invoices/${enc(number)}/payments`, { body, idempotencyKey }),
  payments: (q: {
    state?: string;
    kind?: string;
    method?: string;
    invoice?: string;
    case?: string;
    q?: string;
    from?: string;
    to?: string;
    limit?: number;
    offset?: number;
  }) => bkGet<Page<Payment>>("payments", q),
  payment: (ref: string) => bkGet<Payment>(`payments/${enc(ref)}`),
  updatePayment: (ref: string, body: { reference?: string | null; note?: string | null }) =>
    bkSend<Payment & { changed: string[] }>(`payments/${enc(ref)}`, { method: "PATCH", body }),
  verify: (ref: string) => bkSend<Payment>(`payments/${enc(ref)}/verify`),
  void: (ref: string, reason: string) => bkSend<Payment>(`payments/${enc(ref)}/void`, { body: { reason } }),
  uploadEvidence: (body: { entity_id: string; file_name: string; content_type: string; data_base64: string }) =>
    bkSend<OpsFile>("ops-files", { body: { ...body, kind: "payment_evidence", entity_type: "payment" } }),
  download: (fileId: string) => bkGet<{ url: string; expires_in: number }>(`ops-files/${enc(fileId)}/download`),
};
