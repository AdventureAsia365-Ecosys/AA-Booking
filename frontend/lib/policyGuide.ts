// lib/policyGuide.ts — AA-1096 l23 (tech lead S279: "each setting needs an explanation or definition so I know when to
// change it"): the pure helpers of the parameter guides on Pricing > Policy (backend app/pricing/policy_guides.py) and
// the decimal parse of the change drawer. No imports but types, so lib/policyGuide.test.mjs runs them under node.

import type { ParamType, PolicyParam } from "./api/policyParams";

/** The operator's guide of a parameter, one per language. */
export const GUIDE_PARTS = ["what", "raise", "lower", "when", "example"] as const;
export type GuidePart = (typeof GUIDE_PARTS)[number];
export type GuideText = Record<GuidePart, string>;
export type PolicyGuide = Partial<Record<"en" | "vi", GuideText>>;

/** A decimal typed with a point or a comma ("2.5" or "2,5", the staff's own habit), as the API's decimal string
 *  ("2.5"); null when it is not a decimal of at most 2 places. */
export function decimalText(text: string): string | null {
  const t = text.trim().replace(",", ".");
  return /^\d+(\.\d{1,2})?$/.test(t) ? t : null;
}

/** The guide in the page's language, else English, else null. */
export function guideFor(p: Pick<PolicyParam, "guide">, lang: string): GuideText | null {
  const g = p.guide;
  if (!g) return null;
  return (lang === "vi" ? g.vi : undefined) ?? g.en ?? g.vi ?? null;
}

/** How the page names the two directions of a value: a number goes up or down, a yes/no is switched on or off, a
 *  day moves later or earlier, a list or map is made stricter or looser. */
export type DirectionWords = "number" | "switch" | "date" | "choice";
export function directionWords(type: ParamType): DirectionWords {
  if (type === "integer" || type === "decimal") return "number";
  if (type === "boolean") return "switch";
  return type === "month_day" ? "date" : "choice";
}
