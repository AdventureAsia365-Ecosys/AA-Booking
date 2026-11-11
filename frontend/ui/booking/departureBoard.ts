// ui/booking/departureBoard.ts
// AA-1073 rest (bk-048) — pure helpers of the departure board page: the weeks of a month calendar (Monday first), month
// moves and ranges, the label of a price adjustment and the occupancy of a departure for a bar.
//
// Import-free on purpose: departureBoard.test.mjs loads this file with Node's type stripping (`npm run test:unit`).

/** "-10 %" / "+25 EUR per traveller" of an adjustment (pct is a fraction). */
export function adjustmentLabel(a: { adjust_pct: string | null; amount: string | null; currency: string | null }): string {
  if (a.adjust_pct !== null) {
    const pct = Math.round(Number(a.adjust_pct) * 10000) / 100;
    return `${pct > 0 ? "+" : ""}${pct} %`;
  }
  const amount = Number(a.amount ?? 0);
  return `${amount > 0 ? "+" : ""}${amount} ${a.currency ?? ""} per traveller`.trim();
}

/** Occupancy as a whole number 0..100 for a bar (null without capacity). */
export function occupancy(d: { occupancy_pct: string | null }): number | null {
  if (d.occupancy_pct === null) return null;
  const n = Number(d.occupancy_pct);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : null;
}

/** The weeks of a month calendar (Monday first): each week 7 ISO days or null outside the month. */
export function monthWeeks(month: string): (string | null)[][] {
  const [y, m] = month.split("-").map((x) => Number.parseInt(x, 10));
  const first = new Date(Date.UTC(y, m - 1, 1));
  const days = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const lead = (first.getUTCDay() + 6) % 7; // Monday = 0
  const cells: (string | null)[] = Array.from({ length: lead }, () => null);
  for (let d = 1; d <= days; d++) cells.push(`${month}-${String(d).padStart(2, "0")}`);
  while (cells.length % 7) cells.push(null);
  const weeks: (string | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return weeks;
}

/** "YYYY-MM" moved by `delta` months. */
export function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map((x) => Number.parseInt(x, 10));
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** First and last ISO day of a "YYYY-MM" month. */
export function monthRange(month: string): { from: string; to: string } {
  const [y, m] = month.split("-").map((x) => Number.parseInt(x, 10));
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return { from: `${month}-01`, to: `${month}-${String(last).padStart(2, "0")}` };
}
