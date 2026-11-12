// ui/booking/priceVersions.ts
// AA-1091 (bk-052 / bk-053) — pure helpers of the price versions drawer, Pricing → Seasons and Pricing → Holiday
// events: which buttons a person sees on a version, a season range or a holiday event (the API decides; this only
// hides what it would refuse: the maker never approves their own change), which of them needs a reason, the tone of
// each state, the short text of a diff row, and who made or decided a change (a person, never a raw id).
//
// Import-free on purpose: priceVersions.test.mjs loads this file with Node's type stripping (`npm run test:unit`).

export type ChangeState = "pending" | "approved" | "rejected" | "withdrawn" | "ending" | "ended";
export type ChangeAction = "approve" | "reject" | "withdraw" | "end" | "end_approve" | "end_reject";
export type VersionAction = "edit" | "submit" | "withdraw" | "approve" | "reject";
export type Tone = "neutral" | "warning" | "success" | "info" | "danger";

/** A maker or checker as the API returns it: a staff member's id and directory name, or a system marker
 *  (`migration:bk-052`, `system:bk_trip_plan_sync`, `seed:…`) with no name. */
export interface Person {
  id: string;
  name: string | null;
}

/** What to show for a person: the name; a label for a system marker; the id only as a last resort. */
export function personText(
  p: Person | null | undefined,
  labels: { migration: string; system: string; seed: string; nobody: string },
): string {
  if (!p) return labels.nobody;
  if (p.name) return p.name;
  if (p.id.startsWith("migration:")) return labels.migration;
  if (p.id.startsWith("system:")) return labels.system;
  if (p.id.startsWith("seed:")) return labels.seed;
  return p.id;
}

/** Actions that carry a reason (ADR 0005b d1): every one but the two approvals. */
export function needsReason(action: ChangeAction | "approve_plan_calendar"): boolean {
  return action !== "approve" && action !== "end_approve" && action !== "approve_plan_calendar";
}

/** The actions on a season range or a holiday event in its state. */
export function changeActions(
  item: { state: ChangeState; created_by: Person; end_requested_by: Person | null },
  me: string | undefined,
  canWrite: boolean,
  canApprove: boolean,
): ChangeAction[] {
  const out: ChangeAction[] = [];
  const mine = item.created_by.id === me;
  if (item.state === "pending") {
    if (canApprove && !mine) out.push("approve", "reject");
    if (canWrite && mine) out.push("withdraw");
  } else if (item.state === "approved") {
    if (canWrite) out.push("end");
  } else if (item.state === "ending") {
    if (canApprove && item.end_requested_by?.id !== me) out.push("end_approve", "end_reject");
  }
  return out;
}

/** The actions on a price list version: the maker edits, submits and withdraws a draft (and withdraws a pending one);
 *  a second person approves or rejects a pending one and withdraws a published one. */
export function versionActions(
  v: { status: string; created_by: Person },
  me: string | undefined,
  canWrite: boolean,
  canApprove: boolean,
): VersionAction[] {
  const mine = v.created_by.id === me;
  if (v.status === "draft") return canWrite && mine ? ["edit", "submit", "withdraw"] : [];
  if (v.status === "pending") {
    const out: VersionAction[] = [];
    if (canApprove && !mine) out.push("approve", "reject");
    if (canWrite && mine) out.push("withdraw");
    return out;
  }
  if (v.status === "published") return canApprove && !mine ? ["withdraw"] : [];
  return [];
}

export const VERSION_TONE: Record<string, Tone> = {
  draft: "neutral",
  pending: "warning",
  published: "success",
  superseded: "info",
  rejected: "danger",
  withdrawn: "neutral",
};

export const CHANGE_TONE: Record<ChangeState, Tone> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
  withdrawn: "neutral",
  ending: "warning",
  ended: "neutral",
};

export const SEASON_TONE: Record<string, Tone> = { low: "neutral", high: "info", peak: "warning", festive: "danger" };

/** "+12.0 %", "-3.5 %", "new" or "same" for one diff row. */
export function changeText(row: { kind: string; change_pct: string | null }): string {
  if (row.kind === "added" || row.change_pct === null) return "new";
  const pct = Number(row.change_pct);
  if (pct === 0) return "same";
  return `${pct > 0 ? "+" : ""}${row.change_pct} %`;
}

/** A holiday event code from its name ("Lunar New Year" -> "lunar_new_year"), the same every year. */
export function eventCode(name: string): string {
  const code = name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40);
  return /^[a-z]/.test(code) ? code : `e_${code}`.slice(0, 40);
}
