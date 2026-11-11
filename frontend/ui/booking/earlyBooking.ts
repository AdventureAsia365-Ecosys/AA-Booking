// ui/booking/earlyBooking.ts
// AA-1073 rest (bk-048, bk-17) — the early-booking discount of a pricing rule, for the "New pricing rule" drawer and the
// rules table: which channels carry it, the request fields from what staff typed, the field a 422 code belongs to and
// the short label of a rule's discount. The API owns every check (routers/pricing.py RuleCreate, policy.create_rule);
// this only stops the obvious mistakes before a round trip and puts the API's answer under the right input.
//
// Import-free on purpose: earlyBooking.test.mjs loads this file with Node's type stripping (`npm run test:unit`).

/** The channels whose rule may carry an early-booking discount (policy.create_rule: early_booking_channel). */
export const EARLY_BOOKING_CHANNELS: readonly string[] = ["b2b", "fixed_departure"];

export function earlyBookingAllowed(channel: string): boolean {
  return EARLY_BOOKING_CHANNELS.includes(channel);
}

export type EarlyField = "early_days" | "early_discount_pts";

export type EarlyBookingInput =
  | { ok: true; body: { early_days?: number; early_discount_pts?: string } }
  | { ok: false; field: EarlyField; message: string };

/** The request fields for the typed "days before start" and "margin points off" (points as a percentage, "5" = 5 pts):
 *  both empty or a channel without the discount = no fields; both filled = both fields; one of them = an error on the
 *  empty one. Days are a whole number 1..365; points are above 0 and below 100 with at most 2 decimals. */
export function earlyBookingBody(channel: string, daysText: string, ptsText: string): EarlyBookingInput {
  const days = daysText.trim();
  const pts = ptsText.trim();
  if (!earlyBookingAllowed(channel) || (!days && !pts)) return { ok: true, body: {} };
  if (!days) return { ok: false, field: "early_days", message: "Enter the days before start, or clear the margin points." };
  if (!pts) return { ok: false, field: "early_discount_pts", message: "Enter the margin points off, or clear the days." };
  if (!/^\d{1,3}$/.test(days) || Number(days) < 1 || Number(days) > 365) {
    return { ok: false, field: "early_days", message: "Days before start is a whole number from 1 to 365." };
  }
  if (!/^\d{1,2}(\.\d{1,2})?$/.test(pts) || Number(pts) <= 0) {
    return { ok: false, field: "early_discount_pts", message: "Margin points off is a number above 0 and below 100, for example 5." };
  }
  return { ok: true, body: { early_days: Number(days), early_discount_pts: (Number(pts) / 100).toFixed(4) } };
}

/** The input a 422 code of POST /pricing/rules belongs to, with the text to show under it (null = not an early-booking
 *  code). invalid_early_booking: one of the pair is missing, or the points are above the target margin. */
export function earlyBookingError(code: string): { field: EarlyField; message: string } | null {
  if (code === "early_booking_channel") {
    return { field: "early_days", message: "Only the B2B and fixed departure rules carry an early-booking discount." };
  }
  if (code === "invalid_early_booking") {
    return {
      field: "early_discount_pts",
      message: "Enter both values, with no more margin points off than the target margin.",
    };
  }
  return null;
}

/** ["early −5 pts", "from 60 d"] for a rule with the discount (two short lines, so the rules table's Margin column
 *  stays narrow), null without it (points are a fraction of 1). */
export function earlyBookingLabel(rule: {
  early_days?: number | null;
  early_discount_pts?: string | null;
}): [string, string] | null {
  if (rule.early_days == null || rule.early_discount_pts == null) return null;
  const pts = Math.round(Number(rule.early_discount_pts) * 10000) / 100;
  return [`early −${pts} pts`, `from ${rule.early_days} d`];
}
