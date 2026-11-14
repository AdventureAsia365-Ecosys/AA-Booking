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
//   GET  /booking/pricing/cancellation-policies     B2B cancellation fee schedules (AA-974) (pricing.read)
//   POST /booking/pricing/cancellation-policies     new pending schedule                   (pricing.write)
//   POST /booking/pricing/cancellation-policies/{id}/approve   approve (403 own_row)       (pricing.write)
//   POST /booking/pricing/{rules|fx-rates|cancellation-policies}/{id}/reject {reason}   a second person ends a
//        pending row (403 own_row, 409 already_approved / already_decided)          (pricing.write, S280 bk-058)
//   POST /booking/pricing/{rules|fx-rates|cancellation-policies}/{id}/withdraw          its maker ends a pending row
//        (403 not_maker)                                                               (pricing.write, S280 bk-058)

import { bkGet, bkSend } from "./client";
import type { Channel } from "./proposals";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

/** pending = waiting for a second person; in_force = used for prices today; scheduled = approved, dated later;
 *  superseded = approved, replaced by a newer approved row; rejected / withdrawn (S280, bk-058) = a pending row a
 *  second person rejected or its maker withdrew: it never prices anything (its day stays taken). */
export type PolicyStatus = "pending" | "in_force" | "scheduled" | "superseded" | "rejected" | "withdrawn";

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
  /** S280: pending and its day has passed: it can no longer be approved (409 effective_date_passed); enter it again
   * with a later day. */
  day_passed: boolean;
}

/** S280 (bk-058): the reject / withdraw answer of a pricing rule, FX rate or cancellation schedule row. */
export interface ClosableRow extends PolicyRowBase {
  /** Pending, pricing.write, and not the creator (a reason is required). */
  can_reject: boolean;
  /** Pending, pricing.write, and the creator. */
  can_withdraw: boolean;
  closed_by: PolicyPerson | null;
  closed_at: IsoDateTime | null;
  close_reason: string | null;
}

export interface PricingRuleRow extends ClosableRow {
  channel: Channel;
  target_margin: DecimalString;
  floor_margin: DecimalString;
  peak_uplift_pts: DecimalString;
  card_fee_pct: DecimalString;
  valid_from: IsoDate;
  note: string | null;
  /** b2b rules only (AA-1029): the suggested retail price markup on the agency net price ("0.2500" = net + 25 %). */
  rrp_markup_pct?: DecimalString | null;
  /** AA-1075 (bk-15): a child pays this fraction of the adult price of a tour ("0.5000"); null = like an adult. */
  child_pct?: DecimalString | null;
  /** AA-1075 (bk-15): net cost per infant per trip in USD, priced by the rule; null = infants free. */
  infant_flat_usd?: DecimalString | null;
  /** AA-1073 rest (bk-048, b2b and fixed_departure rules only): a quote made at least this many days before the
   *  departure starts takes early_discount_pts off the margin; both null = no early-booking discount. */
  early_days?: number | null;
  /** Margin points off as a fraction ("0.0500" = 5 points), never more than the target margin. */
  early_discount_pts?: DecimalString | null;
}

export interface FxRateRow extends ClosableRow {
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

/** One step of a cancellation fee schedule: cancelling `min_days` or more days before the start costs `fee_pct` %
 *  of the booking (the step with the largest min_days at or below the days left applies). */
export interface CancellationTier {
  min_days: number;
  fee_pct: DecimalString;
}

/** A B2B cancellation fee schedule (AA-974, bk-021): the B2B default (no source_id) or one tour's own. */
export interface CancellationPolicyRow extends ClosableRow {
  channel: string;
  trip_plan_id: string | null;
  source_id: string | null;
  valid_from: IsoDate;
  title: string;
  /** min_days descending. */
  tiers: CancellationTier[];
  note: string | null;
}

export interface NewCancellationPolicyBody {
  title: string;
  /** Omit for the B2B default schedule. */
  source_id?: string;
  valid_from: IsoDate;
  tiers: { min_days: number; fee_pct: string }[];
  note?: string;
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
  rrp_markup_pct?: string;
  child_pct?: string;
  infant_flat_usd?: string;
  /** Both or neither; b2b and fixed_departure only (422 invalid_early_booking / early_booking_channel). */
  early_days?: number;
  early_discount_pts?: string;
}

export interface NewFxRateBody {
  currency: string;
  rate_per_usd: string;
  buffer_pct: string;
  as_of: IsoDate;
  source: string;
}

/** The three policy row kinds that a second person rejects or their maker withdraws (S280, bk-058). */
export type ClosableKind = "rules" | "fx-rates" | "cancellation-policies";

export const pricingPolicyApi = {
  rules: (channel?: string) => bkGet<PolicyList<PricingRuleRow>>("pricing/rules", { channel }),
  createRule: (body: NewRuleBody) => bkSend<PricingRuleRow>("pricing/rules", { body }),
  approveRule: (id: string) => bkSend<PricingRuleRow>(`pricing/rules/${enc(id)}/approve`),
  fxRates: (currency?: string) => bkGet<PolicyList<FxRateRow>>("pricing/fx-rates", { currency }),
  createFxRate: (body: NewFxRateBody) => bkSend<FxRateRow>("pricing/fx-rates", { body }),
  approveFxRate: (id: string) => bkSend<FxRateRow>(`pricing/fx-rates/${enc(id)}/approve`),
  cancellationPolicies: (sourceId?: string) =>
    bkGet<PolicyList<CancellationPolicyRow>>("pricing/cancellation-policies", { source_id: sourceId }),
  createCancellationPolicy: (body: NewCancellationPolicyBody) =>
    bkSend<CancellationPolicyRow>("pricing/cancellation-policies", { body }),
  approveCancellationPolicy: (id: string) =>
    bkSend<CancellationPolicyRow>(`pricing/cancellation-policies/${enc(id)}/approve`),
  rejectRow: (kind: ClosableKind, id: string, reason: string) =>
    bkSend<ClosableRow>(`pricing/${kind}/${enc(id)}/reject`, { body: { reason } }),
  withdrawRow: (kind: ClosableKind, id: string) => bkSend<ClosableRow>(`pricing/${kind}/${enc(id)}/withdraw`),
};
