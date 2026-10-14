// lib/api/pricingPolicy.ts
// AA-810 (S237) — typed calls for Pricing rules & FX (backend routers/pricing.py + app/pricing/policy.py, bk-007).
// Both tables are append-only: a change is a new row, pending until a SECOND staff member approves it
// (maker-checker, ADR 0005 decision 8, UI spec Q19); only approved rows price anything.
//
//   GET  /booking/pricing/rules?channel=            rules, newest first, with status       (pricing.read)
//   POST /booking/pricing/rules                     new pending rule                       (pricing.write)
//   POST /booking/pricing/rules/{id}/approve        approve (403 own_row for the creator)  (pricing.write)
//   GET  /booking/pricing/fx-rates?currency=        FX rows, newest first, with status     (pricing.read)
//   POST /booking/pricing/fx-rates                  new pending FX row                     (pricing.write)
//   POST /booking/pricing/fx-rates/{id}/approve     approve (403 own_row for the creator)  (pricing.write)

import { bkGet, bkSend } from "./client";
import type { Channel } from "./proposals";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

/** pending = waiting for a second person; in_force = used for prices today; scheduled = approved, dated later;
 *  superseded = approved, replaced by a newer approved row. */
export type PolicyStatus = "pending" | "in_force" | "scheduled" | "superseded";

export interface PolicyPerson {
  id: string;
  name: string | null;
}

interface PolicyRowBase {
  id: string;
  status: PolicyStatus;
  created_by: PolicyPerson | null;
  created_at: IsoDateTime;
  approved_by: PolicyPerson | null;
  approved_at: IsoDateTime | null;
  /** The API's maker-checker answer for the caller: pending, pricing.write, and not the creator. */
  can_approve: boolean;
}

export interface PricingRuleRow extends PolicyRowBase {
  channel: Channel;
  target_margin: DecimalString;
  floor_margin: DecimalString;
  peak_uplift_pts: DecimalString;
  card_fee_pct: DecimalString;
  valid_from: IsoDate;
  note: string | null;
}

export interface FxRateRow extends PolicyRowBase {
  currency: string;
  rate_per_usd: DecimalString;
  buffer_pct: DecimalString;
  as_of: IsoDate;
  source: string;
}

export interface PolicyList<T> {
  items: T[];
  /** The pricing day (UTC) the statuses were computed for. */
  today: IsoDate;
}

/** Ratios are fractions as strings: "0.30" = 30%. */
export interface NewRuleBody {
  channel: Channel;
  target_margin: string;
  floor_margin: string;
  peak_uplift_pts: string;
  card_fee_pct: string;
  valid_from: IsoDate;
  note?: string;
}

export interface NewFxRateBody {
  currency: string;
  rate_per_usd: string;
  buffer_pct: string;
  as_of: IsoDate;
  source: string;
}

export const pricingPolicyApi = {
  rules: (channel?: string) => bkGet<PolicyList<PricingRuleRow>>("pricing/rules", { channel }),
  createRule: (body: NewRuleBody) => bkSend<PricingRuleRow>("pricing/rules", { body }),
  approveRule: (id: string) => bkSend<PricingRuleRow>(`pricing/rules/${enc(id)}/approve`),
  fxRates: (currency?: string) => bkGet<PolicyList<FxRateRow>>("pricing/fx-rates", { currency }),
  createFxRate: (body: NewFxRateBody) => bkSend<FxRateRow>("pricing/fx-rates", { body }),
  approveFxRate: (id: string) => bkSend<FxRateRow>(`pricing/fx-rates/${enc(id)}/approve`),
};
