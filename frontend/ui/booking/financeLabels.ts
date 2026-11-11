// ui/booking/financeLabels.ts — AA-1074 (S277 signed-in UAT fixes): labels of the Costs & profit pages and of a trip
// case's Cost vs billed panel. Pure functions (no imports), unit-tested by financeLabels.test.mjs.

/** The tour of a departure: its catalog title, else a short form of the catalog key (never the raw key). */
export function tourLabel(title: string | null | undefined, sourceId: string | null | undefined): string {
  if (title && title.trim()) return title;
  return sourceId ? `Tour ${sourceId.slice(0, 8)}` : "—";
}

/**
 * The per-currency totals to show under the USD summary of a case's costs. Without the USD summary: all of them.
 * With it, a USD line only repeats the summary (planned / billed / variance in USD), so only the other currencies stay.
 */
export function nativeTotals<T extends { currency: string }>(totals: T[], hasUsdSummary: boolean): T[] {
  return hasUsdSummary ? totals.filter((t) => t.currency !== "USD") : totals;
}
