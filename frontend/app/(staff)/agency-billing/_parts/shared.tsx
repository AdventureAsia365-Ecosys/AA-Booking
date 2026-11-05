"use client";
// app/(staff)/agency-billing/_parts/shared.tsx — small pieces shared by the agency billing pages (AA-1037 R4).

import { ApiError } from "@/lib/api/client";
import type { AgencyInvoiceStatus, AgencyPaymentStatus, BillingHealth } from "@/lib/api/agencyBilling";
import { Pill, type BookingTone } from "@/ui";

const INVOICE_STATUS: Record<AgencyInvoiceStatus, { label: string; tone: BookingTone }> = {
  draft: { label: "Draft", tone: "neutral" },
  issued: { label: "Issued", tone: "info" },
  paid: { label: "Paid", tone: "success" },
  void: { label: "Void", tone: "neutral" },
};
const PAYMENT_STATUS: Record<AgencyPaymentStatus, { label: string; tone: BookingTone }> = {
  recorded: { label: "Waiting for approval", tone: "warning" },
  approved: { label: "Approved", tone: "success" },
  rejected: { label: "Rejected", tone: "danger" },
};
const HEALTH: Record<BillingHealth, { label: string; tone: BookingTone; help: string }> = {
  ok: { label: "OK", tone: "success", help: "Nothing overdue past the warning days" },
  warn: { label: "Warning", tone: "warning", help: "Overdue past the warning days: the agency is warned" },
  past_due: { label: "Past due", tone: "danger", help: "Overdue past the suspend days: the account is suspended and new booking holds are paused" },
};

export function InvoiceStatusPill({ status, overdueDays = 0 }: { status: AgencyInvoiceStatus; overdueDays?: number }) {
  if (status === "issued" && overdueDays > 0) {
    return <Pill tone="danger">{`Overdue ${overdueDays} d`}</Pill>;
  }
  const m = INVOICE_STATUS[status] ?? { label: status, tone: "neutral" as BookingTone };
  return <Pill tone={m.tone}>{m.label}</Pill>;
}

export function PaymentStatusPill({ status }: { status: AgencyPaymentStatus }) {
  const m = PAYMENT_STATUS[status] ?? { label: status, tone: "neutral" as BookingTone };
  return <Pill tone={m.tone}>{m.label}</Pill>;
}

export function HealthPill({ status }: { status: BillingHealth }) {
  const m = HEALTH[status] ?? { label: status, tone: "neutral" as BookingTone, help: "" };
  return (
    <Pill tone={m.tone} title={m.help}>
      {m.label}
    </Pill>
  );
}

export function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

/** One Idempotency-Key per opening of a money form (a retry of the same submit is not a second payment). */
export function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** The current month as YYYY-MM (UTC), the default period of a SaaS draft run. */
export function thisMonth(): string {
  return new Date().toISOString().slice(0, 7);
}
