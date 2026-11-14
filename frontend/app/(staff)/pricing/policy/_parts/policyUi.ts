"use client";
// app/(staff)/pricing/policy/_parts/policyUi.ts — the shared text hooks of Pricing → Policy (AA-1096, S280 settings UI
// audit): a value with its translated unit, the translated label, the approver name, the allowed range with its unit,
// the cross-parameter bound in words, the "not enforced yet" reason in the page's language, and every API error code
// in the page's language (never the API's English message in Vietnamese mode).

import { ApiError } from "@/lib/api/client";
import { formatValue, type ParamValue, type ParamVersion, type PolicyParam } from "@/lib/api/policyParams";
import { valueOn, type CrossBound } from "@/lib/policyEdit";
import { useLang, useT } from "@/ui";
import { M, type MessageKey } from "./messages";

export type T = ReturnType<typeof useT<typeof M.en>>;

/** The translated unit of a parameter ("days" / "ngày"), or null. */
export function unitText(t: T, unit: string | null): string | null {
  if (!unit) return null;
  const key = `unit_${unit}` as MessageKey;
  return key in M.en ? (t(key) as string) : unit;
}

/** The translated pieces a value needs: unit label, yes / no, none, others. */
export function useShow() {
  const t = useT(M);
  return (p: Pick<PolicyParam, "type" | "unit">, value: ParamValue | null | undefined) =>
    formatValue(value, p.type, unitText(t, p.unit), {
      yes: t("yes") as string,
      no: t("no") as string,
      none: t("none") as string,
      others: t("others") as string,
    });
}

export function labelOf(t: T, p: Pick<PolicyParam, "key" | "label">): string {
  const key = `label_${p.key}` as MessageKey;
  return key in M.en ? (t(key) as string) : p.label;
}

export function useLabel() {
  const t = useT(M);
  return (p: Pick<PolicyParam, "key" | "label">) => labelOf(t, p);
}

export function person(t: T, p: ParamVersion["created_by"]): string {
  if (!p) return "—";
  if (p.id.startsWith("seed:")) return t("seedMaker") as string;
  return p.name ?? p.id;
}

const APPROVER_KEY = { finance: "approverFinance", admin: "approverAdmin", pricing: "approverPricing" } as const;

export function approverLabel(t: T, p: Pick<PolicyParam, "approver">): string {
  return t(APPROVER_KEY[p.approver] ?? "approverPricing") as string;
}

/** "1–720 hours", "0–100 %", or the allowed choices; "" when the parameter has no range. */
export function rangeText(t: T, p: Pick<PolicyParam, "choices" | "minimum" | "maximum" | "unit">): string {
  if (p.choices.length) return p.choices.join(", ");
  if (p.minimum === null || p.maximum === null) return "";
  const unit = unitText(t, p.unit);
  return `${p.minimum}–${p.maximum}${unit ? ` ${unit}` : ""}`;
}

const CROSS_KEY = { lt: "crossLt", lte: "crossLte", gt: "crossGt", gte: "crossGte", ne: "crossNe" } as const;

/** "at most Quote validity (14 days)": the bound against the other parameter's value on `day`; null without one. */
export function crossText(t: T, show: ReturnType<typeof useShow>, bound: CrossBound | null | undefined,
  items: PolicyParam[], day: string): string | null {
  if (!bound) return null;
  const other = items.find((i) => i.key === bound.key);
  if (!other) return null;
  const on = valueOn(other, day);
  return t(CROSS_KEY[bound.relation], labelOf(t, other), show(other, on?.value)) as string;
}

/** The "not enforced yet" reason in the page's language. */
export function useNotEnforcedReason() {
  const lang = useLang();
  return (p: Pick<PolicyParam, "not_enforced_reason" | "not_enforced_reason_vi">) =>
    (lang === "vi" ? p.not_enforced_reason_vi : null) ?? p.not_enforced_reason;
}

/** Context of an error: the parameter it is about, and the step that failed. */
export interface ErrorContext {
  param?: PolicyParam;
  step?: "propose" | "submit" | "approve" | "reject" | "withdraw" | "discard";
}

const SIMPLE: Record<string, MessageKey> = {
  own_row: "errOwnRow",
  no_change: "errNoChange",
  effective_in_past: "errEffectiveInPast",
  no_longer_valid: "errNoLonger",
  not_maker: "errNotMaker",
  not_draft: "errNotDraft",
  already_decided: "errDecided",
  reason_required: "errReason",
  not_pending: "errNotPending",
  unknown_param: "errUnknownParam",
  policy_param_not_found: "errUnknownParam",
  policy_version_not_found: "errNotFound",
  trip_plan_not_found: "errTripPlan",
  invalid_tiers: "errTiers",
  title_required: "errTitle",
  cancellation_policy_exists: "errCxExists",
  already_approved: "errAlreadyApproved",
  cancellation_policy_not_found: "errNotFound",
};

function openVersionText(t: T, err: ApiError): string {
  const who = typeof err.body.open_by === "string" ? err.body.open_by : null;
  return err.body.open_status === "draft" && who ? (t("errOpenDraft", who) as string) : (t("errOpen") as string);
}

const CONTEXTUAL: Record<string, (t: T, err: ApiError, ctx: ErrorContext) => string | null> = {
  approver_group: (t, _e, { param }) => (param ? (t("errGroupWho", approverLabel(t, param)) as string) : (t("errGroup") as string)),
  effective_date_passed: (t, _e, { step }) => t(step === "submit" ? "errSubmitPassed" : "errPassed") as string,
  open_version: (t, err) => openVersionText(t, err),
  invalid_value: (t, _e, { param }) => (param ? (t("errInvalid", labelOf(t, param), rangeText(t, param)) as string) : null),
};

function contextual(t: T, err: ApiError, ctx: ErrorContext): string | null {
  const text = CONTEXTUAL[err.code];
  return text ? text(t, err, ctx) : null;
}

/** Every API error code of the page in its language; an unknown code names the code (never English API text). */
export function errorText(t: T, err: unknown, ctx: ErrorContext = {}): string {
  if (!(err instanceof ApiError)) return err instanceof Error ? err.message : String(err);
  const known = SIMPLE[err.code];
  if (known) return t(known) as string;
  const special = contextual(t, err, ctx);
  if (special) return special;
  if (err.fieldErrors.length) return t("errFields") as string;
  return t("errGeneric", err.code || String(err.status)) as string;
}
