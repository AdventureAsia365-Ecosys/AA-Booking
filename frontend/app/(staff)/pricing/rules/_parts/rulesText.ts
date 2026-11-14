// app/(staff)/pricing/rules/_parts/rulesText.ts — S280 (AA-1096 settings UI audit): the shared text helpers of Pricing
// rules & FX (the view and its drawers): status tones, channel labels and every API error code in the page's language.

import { ApiError } from "@/lib/api/client";
import type { PolicyStatus } from "@/lib/api/pricingPolicy";
import { useT, type BookingTone } from "@/ui";
import { R, type RKey } from "./messages";

export type RT = ReturnType<typeof useT<typeof R.en>>;

export const TONE: Record<PolicyStatus, BookingTone> = { pending: "warning", in_force: "success", scheduled: "info", superseded: "neutral" };

export function channelLabel(t: RT, channel: string): string {
  const key = `channel_${channel}` as RKey;
  return key in R.en ? (t(key) as string) : channel;
}

/** Every API error code of the rules / FX forms in the page's language (never the API's English text). */
const CODES: Record<string, RKey> = {
  own_row: "errOwnRow",
  already_approved: "errAlreadyApproved",
  rule_not_found: "errGone",
  fx_rate_not_found: "errGone",
  effective_date_passed: "errPassed",
  effective_in_past: "errInPast",
  rule_exists: "errRuleExists",
  fx_rate_exists: "errFxExists",
  invalid_rule: "errInvalidRule",
  rrp_markup_b2b_only: "errRrpB2b",
  usd_is_base: "errUsdBase",
  source_required: "errSource",
  invalid_fx_rate: "errInvalidFx",
};

export function apiErrorText(t: RT, err: unknown): string {
  if (!(err instanceof ApiError)) return err instanceof Error ? err.message : String(err);
  const key = CODES[err.code];
  if (key) return t(key) as string;
  if (err.fieldErrors.length) return t("errFields") as string;
  return t("errGeneric", err.code || String(err.status)) as string;
}
