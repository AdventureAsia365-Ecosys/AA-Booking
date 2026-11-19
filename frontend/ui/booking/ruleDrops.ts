// ui/booking/ruleDrops.ts
// AA-1128 (B0 review of the S283 UI audit): a SCHEDULED pricing rule that lowers the target margin, the floor margin
// or the suggested retail price markup (RRP) of its channel, compared with the approved rule it takes over from (the
// rule in force, or the scheduled rule dated just before it). /pricing/rules shows these as a warning banner, so a
// test or mistyped row approved for a future day (the "ZZ" b2b row dated 01/01/2027) is seen before it prices quotes.
// Pending rows are not compared: they price nothing until a second person approves them.
//
// Import-free on purpose: ruleDrops.test.mjs loads this file with Node's type stripping (`npm run test:unit`).

export interface RuleForDrops {
  id: string;
  channel: string;
  status: string;
  valid_from: string;
  target_margin: string;
  floor_margin: string;
  rrp_markup_pct?: string | null;
}

export type DropField = "target_margin" | "floor_margin" | "rrp_markup_pct";

export interface RuleDrop {
  /** The scheduled rule that lowers a value. */
  id: string;
  channel: string;
  valid_from: string;
  field: DropField;
  /** The value of the rule it takes over from. */
  from: string;
  /** Its new value; null = the scheduled rule publishes no RRP any more. */
  to: string | null;
}

const APPROVED = new Set(["in_force", "scheduled"]);
const FIELDS: readonly DropField[] = ["target_margin", "floor_margin", "rrp_markup_pct"];

function num(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function byDay(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/** The approved rows (in force or scheduled) of each channel, oldest day first. */
function approvedByChannel(rows: readonly RuleForDrops[]): RuleForDrops[][] {
  const byChannel = new Map<string, RuleForDrops[]>();
  for (const r of rows) {
    if (!APPROVED.has(r.status)) continue;
    const list = byChannel.get(r.channel) ?? [];
    list.push(r);
    byChannel.set(r.channel, list);
  }
  // One approved row per channel and day (unique on the table); ISO days sort as text.
  return [...byChannel.values()].map((list) => list.sort((a, b) => byDay(a.valid_from, b.valid_from)));
}

/** The values `row` lowers against `prev`, the approved rule it takes over from. */
function rowDrops(prev: RuleForDrops, row: RuleForDrops): RuleDrop[] {
  const drops: RuleDrop[] = [];
  for (const field of FIELDS) {
    const before = num(prev[field]);
    if (before === null) continue; // nothing to lower (no RRP before)
    const after = num(row[field]);
    const lowered = after === null ? field === "rrp_markup_pct" : after < before;
    if (!lowered) continue;
    drops.push({ id: row.id, channel: row.channel, valid_from: row.valid_from, field, from: String(prev[field]), to: after === null ? null : String(row[field]) });
  }
  return drops;
}

/** Every value a scheduled rule lowers against the approved rule before it on its channel, by day, then channel. */
export function scheduledDrops(rows: readonly RuleForDrops[]): RuleDrop[] {
  const drops: RuleDrop[] = [];
  for (const list of approvedByChannel(rows)) {
    for (let i = 1; i < list.length; i += 1) {
      if (list[i].status === "scheduled") drops.push(...rowDrops(list[i - 1], list[i]));
    }
  }
  return drops.sort((a, b) => byDay(a.valid_from, b.valid_from) || a.channel.localeCompare(b.channel));
}
