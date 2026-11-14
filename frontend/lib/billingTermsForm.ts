// lib/billingTermsForm.ts — AA-1096 (S280 settings UI audit): the payment terms form of Agency billing › Settings
// (default terms and an agency's own terms) as typed text and back. No imports but types, so
// lib/billingTermsForm.test.mjs runs it under node.
//
// The deposit is kept as the text staff typed ("7", "12,5") and converted only on save: recomputing it from the
// stored fraction on every keystroke showed float noise (7 -> "7.000000000000001"). Every day count must be filled
// in (an empty field used to be sent as 0) and inside the API's range (backend routers/agency_billing.py
// BillingTermsIn); the suspend day is after the warning day.

import type { BillingTerms } from "./api/agencyBilling";

export type DayKey =
  | "balance_days_before_departure"
  | "saas_due_days"
  | "settlement_due_days"
  | "warn_after_days"
  | "suspend_after_days";

export interface TermsText {
  deposit: string;
  balance_days_before_departure: string;
  saas_due_days: string;
  settlement_due_days: string;
  warn_after_days: string;
  suspend_after_days: string;
  saas_currency: string;
}

/** The allowed range of each day count (the API's Field bounds). */
export const DAY_RANGE: Record<DayKey, { min: number; max: number }> = {
  balance_days_before_departure: { min: 0, max: 365 },
  saas_due_days: { min: 0, max: 120 },
  settlement_due_days: { min: 0, max: 120 },
  warn_after_days: { min: 0, max: 365 },
  suspend_after_days: { min: 1, max: 365 },
};
export const DAY_KEYS = Object.keys(DAY_RANGE) as DayKey[];

/** A stored fraction ("0.3000") as the percentage staff read ("30"; "12.5"). */
export function pctText(fraction: string | number): string {
  return String(Number((Number(fraction) * 100).toFixed(2)));
}

export function termsToText(t: BillingTerms): TermsText {
  const days = Object.fromEntries(DAY_KEYS.map((k) => [k, String(t[k] ?? "")])) as Record<DayKey, string>;
  return { deposit: pctText(t.deposit_pct), ...days, saas_currency: t.saas_currency || "USD" };
}

export type TermsErrors = Partial<Record<DayKey | "deposit", string>>;

function dayError(key: DayKey, text: string): string | null {
  const { min, max } = DAY_RANGE[key];
  if (text.trim() === "") return "Required.";
  if (!/^\d+$/.test(text.trim())) return "Whole days only.";
  const n = Number(text);
  return n < min || n > max ? `Allowed: ${min}–${max} days.` : null;
}

/** The API body, or the errors per field (never an empty field sent as 0). */
export function parseTerms(f: TermsText): { body: BillingTerms | null; errors: TermsErrors } {
  const errors: TermsErrors = {};
  const deposit = f.deposit.trim().replace(",", ".");
  if (!/^\d{1,3}(\.\d{1,2})?$/.test(deposit) || Number(deposit) > 100) errors.deposit = "A percentage from 0 to 100, at most 2 decimals.";
  for (const key of DAY_KEYS) {
    const problem = dayError(key, f[key]);
    if (problem) errors[key] = problem;
  }
  if (!errors.warn_after_days && !errors.suspend_after_days && Number(f.suspend_after_days) <= Number(f.warn_after_days)) {
    errors.suspend_after_days = "Must be more than the warning days.";
  }
  if (Object.keys(errors).length) return { body: null, errors };
  const days = Object.fromEntries(DAY_KEYS.map((k) => [k, Number(f[k])])) as Record<DayKey, number>;
  return { body: { deposit_pct: (Number(deposit) / 100).toFixed(4), ...days, saas_currency: f.saas_currency }, errors };
}
