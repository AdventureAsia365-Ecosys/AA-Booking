// lib/api/policyParams.ts
// AA-1091 (bk-050, root ADR 0005b decision 15) — typed calls for Pricing > Policy: the pricing policy parameters
// (quote validity, surcharge rules, auto-publish threshold, provisional uplift, group size, deposits, ...). A change is
// a new version, never an edit: a draft or pending version takes effect only after a second person of the parameter's
// approver group approves it (backend app/pricing/policy_admin.py).
//
//   GET  /booking/pricing/policy                 every parameter: definition, value in force, open change, history
//   POST /booking/pricing/policy                 new version {key, value, effective_from, reason, submit}
//   POST /booking/pricing/policy/{id}/submit     the maker submits a draft
//   POST /booking/pricing/policy/{id}/withdraw   the maker withdraws a draft or pending change
//   POST /booking/pricing/policy/{id}/approve    a second person approves (403 own_row / approver_group)
//   POST /booking/pricing/policy/{id}/reject     a second person rejects, with a reason

import { bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type ParamType = "integer" | "decimal" | "boolean" | "month_day" | "month_days" | "codes" | "currency_map";
/** pricing = the product / pricing lead approves; finance = finance approves (FX, tax, surcharges, payment terms). */
export type ApproverGroup = "pricing" | "finance";
/** draft / pending: not used yet; in_force: used today; scheduled: approved, from a later day; superseded: replaced. */
export type VersionStatus = "draft" | "pending" | "in_force" | "scheduled" | "superseded" | "rejected" | "withdrawn";
/** integer: number; decimal: "5" (a percentage); boolean; "MM-DD"; a list of those or of codes; country -> currency. */
export type ParamValue = number | string | boolean | string[] | Record<string, string>;

export interface Person {
  id: string;
  name: string | null;
}

export interface ParamVersion {
  id: string;
  key: string;
  version_no: number;
  value: ParamValue;
  unit: string | null;
  effective_from: IsoDate;
  reason: string;
  replaces_id: string | null;
  diff: { from: ParamValue | null; to: ParamValue } | null;
  status: VersionStatus;
  created_by: Person | null;
  created_at: IsoDateTime;
  submitted_at: IsoDateTime | null;
  approved_by: Person | null;
  approved_at: IsoDateTime | null;
  rejected_by: Person | null;
  rejected_at: IsoDateTime | null;
  reject_reason: string | null;
  withdrawn_at: IsoDateTime | null;
  /** What the API allows this caller (the page only shows the buttons). */
  can_submit: boolean;
  can_withdraw: boolean;
  can_approve: boolean;
}

export interface PolicyParam {
  key: string;
  type: ParamType;
  unit: string | null;
  question: string;
  approver: ApproverGroup;
  label: string;
  help: string;
  /** Read by a code path today; false = stored for a later step (shown as such). */
  in_use: boolean;
  minimum: string | null;
  maximum: string | null;
  choices: string[];
  current: ParamVersion | null;
  scheduled: ParamVersion[];
  open: ParamVersion | null;
  history: ParamVersion[];
  can_approve_group: boolean;
}

export interface PolicyListing {
  items: PolicyParam[];
  today: IsoDate;
  pending: number;
}

export interface ProposeBody {
  key: string;
  value: ParamValue;
  effective_from: IsoDate;
  reason: string;
  submit: boolean;
}

export const policyParamsApi = {
  list: () => bkGet<PolicyListing>("pricing/policy"),
  propose: (body: ProposeBody) => bkSend<ParamVersion>("pricing/policy", { body }),
  submit: (id: string) => bkSend<ParamVersion>(`pricing/policy/${enc(id)}/submit`),
  withdraw: (id: string) => bkSend<ParamVersion>(`pricing/policy/${enc(id)}/withdraw`),
  approve: (id: string) => bkSend<ParamVersion>(`pricing/policy/${enc(id)}/approve`),
  reject: (id: string, reason: string) => bkSend<ParamVersion>(`pricing/policy/${enc(id)}/reject`, { body: { reason } }),
};

export interface ValueWords {
  yes: string;
  no: string;
  none: string;
  others: string;
}
const EN_WORDS: ValueWords = { yes: "Yes", no: "No", none: "None", others: "others" };

/** The value as staff read it ("14 days", "5 %", "Yes", "12-24, 12-31", "fx, tax", "VN: VND, others: USD"). */
export function formatValue(
  value: ParamValue | null | undefined,
  type: ParamType,
  unit: string | null,
  words: ValueWords = EN_WORDS,
): string {
  if (value === null || value === undefined) return "—";
  if (type === "boolean") return value ? words.yes : words.no;
  if (type === "month_days" || type === "codes") {
    const list = value as string[];
    return list.length ? list.join(", ") : words.none;
  }
  if (type === "currency_map") {
    const map = value as Record<string, string>;
    const named = Object.entries(map).filter(([k]) => k !== "*").map(([k, v]) => `${k}: ${v}`);
    return [...named, `${words.others}: ${map["*"] ?? "—"}`].join(", ");
  }
  if (type === "month_day") return String(value);
  return unit === "%" ? `${value} %` : unit ? `${value} ${unit}` : String(value);
}

/** The editor text of a value (lists and maps one item per comma) and back; null when the text is not valid. */
export function valueToText(value: ParamValue | null | undefined, type: ParamType): string {
  if (value === null || value === undefined) return "";
  if (type === "month_days" || type === "codes") return (value as string[]).join(", ");
  if (type === "currency_map") return Object.entries(value as Record<string, string>).map(([k, v]) => `${k}=${v}`).join(", ");
  return String(value);
}

export function textToValue(text: string, type: ParamType): ParamValue | null {
  const t = text.trim();
  if (type === "boolean") return t === "true" ? true : t === "false" ? false : null;
  if (type === "integer") return /^-?\d+$/.test(t) ? Number(t) : null;
  if (type === "decimal") return /^\d+(\.\d{1,2})?$/.test(t) ? t : null;
  if (type === "month_day") return /^\d{2}-\d{2}$/.test(t) ? t : null;
  const parts = t ? t.split(",").map((p) => p.trim()).filter(Boolean) : [];
  if (type === "month_days") return parts.every((p) => /^\d{2}-\d{2}$/.test(p)) ? parts : null;
  if (type === "codes") return parts;
  if (type === "currency_map") {
    const map: Record<string, string> = {};
    for (const p of parts) {
      const [k, v] = p.split("=").map((x) => x.trim().toUpperCase());
      if (!k || !v) return null;
      map[k] = v;
    }
    return map;
  }
  return null;
}
