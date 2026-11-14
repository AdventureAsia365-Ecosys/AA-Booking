// lib/policyEdit.ts — AA-1096 (S280 settings UI audit): the pure helpers of Pricing > Policy that decide what the page
// shows before a change is sent. No imports but types, so lib/policyEdit.test.mjs runs them under node.
//
// - searchText / matchesSearch: one page-wide search over every parameter (label in English and Vietnamese, key,
//   the whole guide in both languages, help, approver name), so a parameter is found by its key too.
// - valueOn / scheduledAfter: the value a parameter has on a given day (the version in force or a scheduled one
//   starting on or before it), and the scheduled changes after it; the API compares a proposal against that value.
// - boundProblem / crossProblem: the allowed range and the bound against another parameter (the API's cross check),
//   so the drawer says it in the reader's language instead of the API's English message with raw keys.

import type { ParamValue, ParamVersion, PolicyParam } from "./api/policyParams";

/** Lower case, accents dropped, so "quan tri" finds "Quản trị viên". */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d").replace(/Đ/g, "d").toLowerCase();
}

type Searchable = Pick<PolicyParam, "key" | "label" | "help" | "guide">;

/** Everything a page-wide search looks in for one parameter; `extra` = the page's own words (translated label,
 *  approver name in both languages). */
export function searchText(p: Searchable, extra: string[] = []): string {
  const guide = p.guide ?? {};
  const guides = [guide.en, guide.vi].flatMap((g) => (g ? Object.values(g) : []));
  const parts = [p.key, p.key.replace(/_/g, " "), p.label, p.help, ...guides, ...extra];
  return fold(parts.join(" \u0001 "));
}

/** Every word of the query must appear (in any order). An empty query matches everything. */
export function matchesSearch(haystack: string, query: string): boolean {
  const words = fold(query).split(/\s+/).filter(Boolean);
  return words.every((w) => haystack.includes(w));
}

/** The version that gives the value on `day`: the newest of current + scheduled starting on or before it. */
export function valueOn(p: Pick<PolicyParam, "current" | "scheduled">, day: string): ParamVersion | null {
  const candidates = [p.current, ...p.scheduled].filter((v): v is ParamVersion => !!v && v.effective_from <= day);
  candidates.sort((a, b) => (a.effective_from === b.effective_from ? a.version_no - b.version_no : a.effective_from < b.effective_from ? -1 : 1));
  return candidates.length ? candidates[candidates.length - 1] : null;
}

/** The scheduled changes that start after `day`, first one first (a change on `day` is replaced by them later). */
export function scheduledAfter(p: Pick<PolicyParam, "scheduled">, day: string): ParamVersion[] {
  return [...p.scheduled].filter((v) => v.effective_from > day).sort((a, b) => (a.effective_from < b.effective_from ? -1 : 1));
}

function numbers(value: ParamValue): number[] {
  if (typeof value === "number") return [value];
  if (typeof value === "string") return value.trim() === "" ? [] : [Number(value)];
  if (Array.isArray(value)) return value.map(Number);
  if (value && typeof value === "object") return Object.values(value).map(Number);
  return [];
}

const NUMERIC: ReadonlySet<PolicyParam["type"]> = new Set(["integer", "decimal", "integers", "amount_map"]);

/** "min" / "max" when a number of the value is outside the parameter's range; null when inside (or not numeric). */
export function boundProblem(p: Pick<PolicyParam, "type" | "minimum" | "maximum">, value: ParamValue): "min" | "max" | null {
  if (!NUMERIC.has(p.type)) return null;
  const all = numbers(value).filter((n) => Number.isFinite(n));
  if (p.minimum !== null && all.some((n) => n < Number(p.minimum))) return "min";
  if (p.maximum !== null && all.some((n) => n > Number(p.maximum))) return "max";
  return null;
}

export type Relation = "lt" | "lte" | "gt" | "gte" | "ne";
export interface CrossBound {
  relation: Relation;
  key: string;
}

const HOLDS: Record<Relation, (a: number | string, b: number | string) => boolean> = {
  lt: (a, b) => Number(a) < Number(b),
  lte: (a, b) => Number(a) <= Number(b),
  gt: (a, b) => Number(a) > Number(b),
  gte: (a, b) => Number(a) >= Number(b),
  ne: (a, b) => a !== b,
};

/** True when `value` keeps its bound against `other` (the other parameter's value on the same day); true when either
 *  side is missing (the API then decides). */
export function crossHolds(relation: Relation, value: ParamValue | null | undefined, other: ParamValue | null | undefined): boolean {
  if (value === null || value === undefined || other === null || other === undefined) return true;
  if (typeof value === "object" || typeof other === "object" || typeof value === "boolean" || typeof other === "boolean") return true;
  return HOLDS[relation](value, other);
}
