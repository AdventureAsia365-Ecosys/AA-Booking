// ui/booking/plural.ts — AA-1128 B4 (S283 UI audit): English counts with the right plural ("1 day", "3 days"), so a
// one-day tour never reads "1 days". Pages that switch language take their plural from their own messages.

/** `count` and its noun, singular for exactly 1 ("1 day"), plural otherwise ("0 days", "3 days"). */
export function countText(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

/** "N day(s)" for a trip length, or `fallback` when there is none. */
export function daysText(days: number | null | undefined, fallback = ""): string {
  return typeof days === "number" && Number.isFinite(days) ? countText(days, "day") : fallback;
}
