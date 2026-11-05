// lib/api/agencyBilling.ts
// AA-1037 R4 — typed calls for agency billing (backend routers/agency_billing.py, app/agency_billing). Every invoice
// from AA to an agency: the software fee (saas, AAS- numbers) and the tour settlement (settlement, AAT- numbers).
// Read agency_billing.read, write agency_billing.write, approve / reject a payment agency_payment.approve (never the
// person who recorded it), configuration billing_config.write: admin and finance.

import { BFF_BASE, ApiError, bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type InvoiceKind = "saas" | "settlement";
export type AgencyInvoiceStatus = "draft" | "issued" | "paid" | "void";
export type AgencyPaymentStatus = "recorded" | "approved" | "rejected";
export type BillingHealth = "ok" | "warn" | "past_due";
export type AgencyPaymentMethod = "bank_transfer" | "cash" | "card_offline" | "other";

export const KIND_LABEL: Record<InvoiceKind, string> = { saas: "Software fee", settlement: "Tour settlement" };
export const LINE_ROLE_LABEL: Record<string, string> = {
  subscription: "Subscription",
  extra_seats: "Extra seats",
  adjustment: "Adjustment",
  deposit: "Deposit",
  balance: "Balance",
  full: "Full payment",
  cancellation_fee: "Cancellation fee",
};
export const AGENCY_PAYMENT_METHOD_LABEL: Record<AgencyPaymentMethod, string> = {
  bank_transfer: "Bank transfer",
  cash: "Cash",
  card_offline: "Card (offline terminal)",
  other: "Other",
};

export interface AgencyInvoice {
  id: string;
  number: string | null;
  party_tenant_id: string;
  agency_name: string;
  invoice_kind: InvoiceKind;
  status: AgencyInvoiceStatus;
  period_start: IsoDate;
  period_end: IsoDate;
  currency: string;
  subtotal: string;
  tax_rate: string;
  tax_amount: string;
  total: string;
  paid: string;
  waiting: string;
  balance: string;
  overdue_days: number;
  issued_on: IsoDate | null;
  due_date: IsoDate | null;
  issued_by: string | null;
  e_invoice_no: string | null;
  staff_note: string | null;
  void_reason: string | null;
  footer_wording: string | null;
  issuer: Record<string, string | null> | null;
  bill_to: Record<string, string | null> | null;
  source: "job" | "staff";
  created_at: IsoDateTime;
}

export interface AgencyInvoiceLine {
  line_no: number;
  line_role: string;
  product: string | null;
  plan_key: string | null;
  source_hold_id: string | null;
  trip_case_code: string | null;
  description: string;
  quantity: string;
  unit_price: string;
  amount: string;
  voided: boolean;
}

export interface AgencyPayment {
  id: string;
  invoice_id: string;
  invoice_number?: string | null;
  invoice_kind?: InvoiceKind;
  agency_name?: string;
  amount: string;
  currency: string;
  method: AgencyPaymentMethod;
  received_on: IsoDate;
  reference: string | null;
  document_ref: string;
  note: string | null;
  status: AgencyPaymentStatus;
  recorded_by: string;
  recorded_at: IsoDateTime;
  approved_by: string | null;
  rejected_by: string | null;
  reject_reason: string | null;
}

/** AA-1037 extras G4: an overdue reminder step handled for an invoice (bk-044). */
export interface InvoiceReminder {
  step_days: number;
  step_kind: "reminder" | "credit_stop";
  days_overdue: number;
  outcome: "sent" | "skipped";
  skip_reason: string | null;
  sent_to: string | null;
  redirected: boolean;
  created_at: IsoDateTime;
}

export interface AgencyInvoiceDetail extends AgencyInvoice {
  lines: AgencyInvoiceLine[];
  payments: AgencyPayment[];
  reminders?: InvoiceReminder[];
}

/** AA-1037 extras G6: a clause of AA's terms to agencies (bk-044); agencies see approved clauses only. */
export interface TermsClause {
  id: string;
  code: string;
  position: number;
  title: string;
  body: string;
  free_cancel_days: number | null;
  status: "draft" | "approved";
  version: number;
  updated_at: IsoDateTime;
  updated_by: string;
  approved_by: string | null;
  approved_at: IsoDateTime | null;
}

export type ExportWhat = "invoices" | "payments";

/** A staff download (CSV, HTML) as a Blob; the API's error code on failure (fetch through the BFF, like bkGet). */
async function download(path: string, query: Record<string, string>): Promise<Blob> {
  const qs = new URLSearchParams(query).toString();
  const res = await fetch(`${BFF_BASE}/${path}${qs ? `?${qs}` : ""}`, { cache: "no-store" });
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

/** Save a Blob under a file name (browser only). */
export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export interface Page<T> {
  items: T[];
  total: number;
}

export interface BillingStatusRow {
  tenant_id: string;
  agency_name: string;
  partner_status: string;
  status: BillingHealth;
  oldest_overdue_days: number;
  overdue_invoices: number;
  overdue_amount: Record<string, string | number> | string;
  paid_through: IsoDate | null;
  warn_after_days: number | null;
  suspend_after_days: number | null;
}

export interface DraftRun {
  id: string;
  kind: string;
  status: string;
  payload: unknown;
  result: unknown;
  error: string | null;
  created_by: string;
  created_at: IsoDateTime;
  finished_at: IsoDateTime | null;
}

export interface BillingEntity {
  id: string;
  code: string;
  legal_entity: string;
  tax_code: string | null;
  registered_office: string | null;
  country_iso2: string | null;
  invoice_inbox: string | null;
  payment_instructions: string | null;
  is_default: boolean;
  active: boolean;
}

export interface TaxRule {
  id: string;
  code: string;
  invoice_kind: InvoiceKind | null;
  recipient_country: string | null;
  rate: string;
  line_wording: string;
  footer_wording: string | null;
  effective_from: IsoDate;
  effective_to: IsoDate | null;
  active: boolean;
}

export interface BillingTerms {
  id?: string;
  party_tenant_id?: string | null;
  agency_name?: string | null;
  deposit_pct: string;
  balance_days_before_departure: number;
  saas_due_days: number;
  settlement_due_days: number;
  warn_after_days: number;
  suspend_after_days: number;
  saas_currency: string;
}

export interface Summary {
  by_currency: { currency: string; drafts: number; open: string; overdue: string; waiting: string }[];
}

export const agencyBillingApi = {
  invoices: (q: { kind?: string; status?: string; q?: string; limit?: number; offset?: number }) =>
    bkGet<Page<AgencyInvoice>>("agency-invoices", q),
  summary: () => bkGet<Summary>("agency-invoices/summary"),
  invoice: (ref: string) => bkGet<AgencyInvoiceDetail>(`agency-invoices/${enc(ref)}`),
  patch: (ref: string, body: { staff_note?: string | null; e_invoice_no?: string }) =>
    bkSend<AgencyInvoiceDetail>(`agency-invoices/${enc(ref)}`, { method: "PATCH", body }),
  deleteDraft: (ref: string) => bkSend<void>(`agency-invoices/${enc(ref)}`, { method: "DELETE" }),
  addLine: (ref: string, body: { description: string; quantity: string; unit_price: string }) =>
    bkSend<AgencyInvoiceDetail>(`agency-invoices/${enc(ref)}/lines`, { body }),
  removeLine: (ref: string, lineNo: number) =>
    bkSend<AgencyInvoiceDetail>(`agency-invoices/${enc(ref)}/lines/${lineNo}`, { method: "DELETE" }),
  issue: (ref: string, dueDate?: string) =>
    bkSend<AgencyInvoiceDetail>(`agency-invoices/${enc(ref)}/issue`, { body: dueDate ? { due_date: dueDate } : {} }),
  void: (ref: string, reason: string) =>
    bkSend<AgencyInvoiceDetail>(`agency-invoices/${enc(ref)}/void`, { body: { reason } }),
  recordPayment: (
    ref: string,
    body: { amount: string; method: AgencyPaymentMethod; received_on: string; reference?: string; document_ref: string; note?: string },
    idempotencyKey: string,
  ) => bkSend<AgencyPayment>(`agency-invoices/${enc(ref)}/payments`, { body, idempotencyKey }),
  payments: (q: { state?: string; invoice?: string; limit?: number; offset?: number }) =>
    bkGet<Page<AgencyPayment>>("agency-payments", q),
  approve: (id: string) => bkSend<AgencyPayment>(`agency-payments/${enc(id)}/approve`),
  reject: (id: string, reason: string) => bkSend<AgencyPayment>(`agency-payments/${enc(id)}/reject`, { body: { reason } }),
  status: () => bkGet<{ items: BillingStatusRow[] }>("agency-billing/status"),
  runs: () => bkGet<{ items: DraftRun[] }>("agency-billing/runs"),
  run: (body: { kind: InvoiceKind; period?: string; tenant_id?: string }) =>
    bkSend<{ job_id: string | null; kind: string; period: string }>("agency-billing/runs", { body }),
  entities: () => bkGet<{ items: BillingEntity[] }>("billing-entities"),
  createEntity: (body: Partial<BillingEntity>) => bkSend<BillingEntity>("billing-entities", { body }),
  updateEntity: (id: string, body: Partial<BillingEntity>) =>
    bkSend<BillingEntity>(`billing-entities/${enc(id)}`, { method: "PATCH", body }),
  taxRules: () => bkGet<{ items: TaxRule[] }>("tax-rules"),
  createTaxRule: (body: Partial<TaxRule>) => bkSend<TaxRule>("tax-rules", { body }),
  updateTaxRule: (id: string, body: Partial<TaxRule>) =>
    bkSend<TaxRule>(`tax-rules/${enc(id)}`, { method: "PATCH", body }),
  terms: () => bkGet<{ items: BillingTerms[] }>("billing-terms"),
  putDefaultTerms: (body: BillingTerms) => bkSend<BillingTerms>("billing-terms/default", { method: "PUT", body }),
  putAgencyTerms: (tenantId: string, body: BillingTerms) =>
    bkSend<BillingTerms>(`billing-terms/agencies/${enc(tenantId)}`, { method: "PUT", body }),
  deleteAgencyTerms: (tenantId: string) => bkSend<void>(`billing-terms/agencies/${enc(tenantId)}`, { method: "DELETE" }),
  // AA-1037 extras: the accountant export (G5) and the partner terms clauses (G6).
  exportCsv: (what: ExportWhat, from: string, to: string) => download("agency-billing/export.csv", { what, from, to }),
  clauses: () => bkGet<{ items: TermsClause[] }>("partner-terms-clauses"),
  putClause: (code: string, body: { title: string; body: string; free_cancel_days: number | null }) =>
    bkSend<TermsClause>(`partner-terms-clauses/${enc(code)}`, { method: "PUT", body }),
  approveClause: (code: string, version: number) =>
    bkSend<TermsClause>(`partner-terms-clauses/${enc(code)}/approve`, { body: { version } }),
  // AA-1037 extras G2: the booking confirmation of a B2B booking (the HTML file the agency gets).
  confirmation: (caseRef: string) => download(`b2b-desk/bookings/${enc(caseRef)}/confirmation`, {}),
};

/** "2026-11-01" -> "November 2026" (the invoice period is a calendar month). */
export function periodLabel(start: string): string {
  const [y, m] = start.split("-").map(Number);
  if (!y || !m) return start;
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

/** The overdue amounts of a billing status row as "USD 120.00, VND 1500000" ("—" when nothing is overdue). */
export function overdueText(value: BillingStatusRow["overdue_amount"]): string {
  const obj = typeof value === "string" ? (JSON.parse(value || "{}") as Record<string, string | number>) : value;
  const parts = Object.entries(obj ?? {}).map(([c, a]) => `${c} ${a}`);
  return parts.length ? parts.join(", ") : "—";
}
