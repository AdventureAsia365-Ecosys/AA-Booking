// lib/api/billing.ts
// What the supplier bill and payout screens share (AA-682 S4, bk-030; app/(staff)/billing/_parts/BillDrawer.tsx):
// the payment methods and the Idempotency-Key helper. The customer invoices and payments of AA-682 S3 (bk-028) are
// retired with the B2C half of Booking (AA-1046, root ADR 0019: agencies are the only customers; agency money is
// lib/api/agencyBilling.ts), and so are their API calls.

export type PaymentMethod = "bank_transfer" | "card_offline" | "cash" | "other";

export const PAYMENT_METHOD_LABEL: Record<PaymentMethod, string> = {
  bank_transfer: "Bank transfer",
  card_offline: "Card (offline terminal)",
  cash: "Cash",
  other: "Other",
};

/** One key per user action (a retry of the same action reuses it; the API returns the stored row). */
export function newIdempotencyKey(prefix: string): string {
  const rand = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `${prefix}-${rand}`;
}

export interface Person {
  id: string;
  name: string | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
