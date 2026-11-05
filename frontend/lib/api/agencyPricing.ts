// lib/api/agencyPricing.ts
// AA-1030 (bk-045) — typed calls for Pricing → Agency tiers (backend routers/agency_pricing.py, app/b2b/agency_pricing.py).
// Root ADR 0019 b15: every B2B partner has a tier whose approved discount is taken off the agency (b2b) price; an
// optional contract rate card overrides it. Discount rows and rate cards are append-only and maker-checker (a second
// person approves); moving a partner to another tier is audited.
//
//   GET   /booking/pricing/partner-tiers                       tiers + discount in force + margin left  (pricing.read)
//   POST  /booking/pricing/partner-tiers                       new tier                                 (pricing.write)
//   GET   /booking/pricing/partner-tier-discounts?tier_code=   dated discount rows with status          (pricing.read)
//   POST  /booking/pricing/partner-tier-discounts              new pending row                          (pricing.write)
//   POST  /booking/pricing/partner-tier-discounts/{id}/approve approve (403 own_row for the creator)    (pricing.write)
//   GET   /booking/pricing/partner-rate-cards?partner_id=      contract rate cards with status          (pricing.read)
//   POST  /booking/pricing/partner-rate-cards                  new pending card                         (pricing.write)
//   POST  /booking/pricing/partner-rate-cards/{id}/approve     approve (403 own_row for the creator)    (pricing.write)
//   GET   /booking/pricing/partner-terms                       partners with tier and discount today    (pricing.read)
//   PATCH /booking/pricing/partner-terms/{partner_id}          move a partner to another tier           (pricing.write)

import { bkGet, bkSend } from "./client";
import type { PolicyPerson } from "./pricingPolicy";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

/** Rate cards add `expired` (approved, its valid_to is over). */
export type AgencyRowStatus = "pending" | "in_force" | "scheduled" | "superseded" | "expired";

interface ApprovedRowBase {
  id: string;
  status: AgencyRowStatus;
  created_by: PolicyPerson | null;
  created_at: IsoDateTime;
  approved_by: PolicyPerson | null;
  approved_at: IsoDateTime | null;
  can_approve: boolean;
}

export interface PartnerTier {
  code: string;
  label: string;
  sort_order: number;
  partners: number;
  /** Fraction taken off the agency price today ("0.0300" = 3 %). */
  discount_in_force: DecimalString;
  pending: number;
  /** AA's margin on a b2b price after this discount under the b2b rule in force; null without a rule. */
  margin_after_discount: DecimalString | null;
  below_floor: boolean | null;
}

export interface B2bRuleSummary {
  target_margin: DecimalString;
  floor_margin: DecimalString;
  card_fee_pct: DecimalString;
  valid_from: IsoDate;
  rrp_markup_pct: DecimalString | null;
}

export interface TierList {
  items: PartnerTier[];
  b2b_rule: B2bRuleSummary | null;
  today: IsoDate;
}

export interface TierDiscountRow extends ApprovedRowBase {
  tier_code: string;
  tier_label: string;
  discount_pct: DecimalString;
  valid_from: IsoDate;
  note: string | null;
}

export interface RateCardRow extends ApprovedRowBase {
  partner_id: string;
  agency_name: string;
  trip_plan_id: string | null;
  /** Catalog tour key of a one-tour card; null = the whole catalogue. */
  source_id: string | null;
  /** null = an end card: from valid_from it ends the contract rate of its scope (back to the tier). */
  discount_pct: DecimalString | null;
  ends_contract_rate: boolean;
  valid_from: IsoDate;
  valid_to: IsoDate | null;
  contract_ref: string | null;
  note: string | null;
}

export interface PartnerTerms {
  id: string;
  agency_name: string;
  status: "active" | "suspended";
  billing_currency: string;
  projected: boolean;
  tier_code: string;
  tier_label: string;
  tier_discount: DecimalString;
  /** The approved whole-catalogue rate card in force, if any (it overrides the tier). */
  card_discount: DecimalString | null;
  plan_cards: number;
  effective_discount: DecimalString;
}

export interface Listed<T> {
  items: T[];
  today: IsoDate;
}

export interface NewTierBody {
  code: string;
  label: string;
  sort_order?: number;
}

export interface NewTierDiscountBody {
  tier_code: string;
  discount_pct: string;
  valid_from: IsoDate;
  note?: string;
}

export interface NewRateCardBody {
  partner_id: string;
  source_id?: string;
  /** Always sent; null = an end card. */
  discount_pct: string | null;
  valid_from: IsoDate;
  valid_to?: IsoDate;
  contract_ref?: string;
  note?: string;
}

export const agencyPricingApi = {
  tiers: () => bkGet<TierList>("pricing/partner-tiers"),
  createTier: (body: NewTierBody) => bkSend<PartnerTier>("pricing/partner-tiers", { body }),
  discounts: (tierCode?: string) => bkGet<Listed<TierDiscountRow>>("pricing/partner-tier-discounts", { tier_code: tierCode }),
  createDiscount: (body: NewTierDiscountBody) => bkSend<TierDiscountRow>("pricing/partner-tier-discounts", { body }),
  approveDiscount: (id: string) => bkSend<TierDiscountRow>(`pricing/partner-tier-discounts/${enc(id)}/approve`),
  rateCards: (partnerId?: string) => bkGet<Listed<RateCardRow>>("pricing/partner-rate-cards", { partner_id: partnerId }),
  createRateCard: (body: NewRateCardBody) => bkSend<RateCardRow>("pricing/partner-rate-cards", { body }),
  approveRateCard: (id: string) => bkSend<RateCardRow>(`pricing/partner-rate-cards/${enc(id)}/approve`),
  partnerTerms: () => bkGet<Listed<PartnerTerms>>("pricing/partner-terms"),
  setPartnerTier: (partnerId: string, tierCode: string) =>
    bkSend<PartnerTerms>(`pricing/partner-terms/${enc(partnerId)}`, { method: "PATCH", body: { tier_code: tierCode } }),
};
