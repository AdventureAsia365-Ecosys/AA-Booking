// lib/bookingConfig.ts
// AA-683 — commercial defaults (Nghiệp S236 decision 4). The source of truth is the Booking API
// `GET /booking/settings` (bk-005 seed rows `deposit_pct` and `quote_validity_days`); these constants are
// the same values, used only when the settings call has not answered, so a page never shows a
// different number from the one the API will apply.

export const DEFAULT_DEPOSIT_PCT = 30;
export const DEFAULT_QUOTE_VALIDITY_DAYS = 14;
/** The API accepts a proposal validity of 7–30 days from today (bk-005 send rule). */
export const QUOTE_VALIDITY_RANGE_DAYS = { min: 7, max: 30 } as const;

export interface BookingSettingsValues {
  depositPct: number;
  quoteValidityDays: number;
}

export function settingsFromApi(values: Record<string, unknown> | undefined | null): BookingSettingsValues {
  const num = (v: unknown, fallback: number) => (typeof v === "number" && Number.isFinite(v) ? v : fallback);
  return {
    depositPct: num(values?.deposit_pct, DEFAULT_DEPOSIT_PCT),
    quoteValidityDays: num(values?.quote_validity_days, DEFAULT_QUOTE_VALIDITY_DAYS),
  };
}
