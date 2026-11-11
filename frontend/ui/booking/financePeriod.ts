// ui/booking/financePeriod.ts
// AA-1074 — the travel-date windows of the Costs & profit page: a period key from the URL → { from, to } as ISO days
// (local calendar). "all" has no bound. Pure, tested in financePeriod.test.mjs.

export const PERIOD_OPTIONS = [
  { value: "this_month", label: "This month" },
  { value: "last_3", label: "Last 3 months" },
  { value: "this_year", label: "This year" },
  { value: "all", label: "Any date" },
];

function iso(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

/** The window of a period key on `today`; an unknown key is the default, the next 3 months from the 1st. */
export function periodWindow(period: string, today: Date): { from?: string; to?: string } {
  const y = today.getFullYear();
  const m = today.getMonth();
  switch (period) {
    case "all":
      return {};
    case "this_month":
      return { from: iso(new Date(y, m, 1)), to: iso(new Date(y, m + 1, 0)) };
    case "last_3":
      return { from: iso(new Date(y, m - 3, 1)), to: iso(new Date(y, m, 0)) };
    case "this_year":
      return { from: iso(new Date(y, 0, 1)), to: iso(new Date(y, 11, 31)) };
    default:
      return { from: iso(new Date(y, m, 1)), to: iso(new Date(y, m + 3, 0)) };
  }
}
