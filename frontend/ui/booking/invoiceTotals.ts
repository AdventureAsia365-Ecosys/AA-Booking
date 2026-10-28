// ui/booking/invoiceTotals.ts
// AA-996 (Figma View Details: Subtotal / Total due footer, Issue date / Due date pair) — the pure numbers behind the
// invoice page's totals footer and dates card. Money is exact (integer cents as BigInt), never a float; the API stays
// the authority: `total_amount`, `net_paid` and `balance` are shown as it sends them, the split of the lines is only a
// breakdown of the same total. Kept free of React and of path aliases, so Node's test runner loads it as it is.

const AMOUNT_RE = /^-?\d{1,14}(\.\d{1,2})?$/;

function cents(value: string | null | undefined): bigint | null {
  const s = (value ?? "").trim();
  if (!AMOUNT_RE.test(s)) return null;
  const neg = s.startsWith("-");
  const [whole, frac = ""] = s.replace("-", "").split(".");
  const c = BigInt(whole) * BigInt(100) + BigInt((frac + "00").slice(0, 2));
  return neg ? -c : c;
}

function decimal(c: bigint): string {
  const neg = c < BigInt(0);
  const abs = neg ? -c : c;
  return `${neg ? "-" : ""}${(abs / BigInt(100)).toString()}.${(abs % BigInt(100)).toString().padStart(2, "0")}`;
}

export interface TotalsLine {
  kind: "version_total" | "adjustment";
  amount: string;
}

export interface InvoiceTotals {
  /** Sum of the locked approved-proposal line(s). */
  approved: string;
  /** Sum of the adjustment lines (may be negative); null when the invoice has none. */
  adjustments: string | null;
  /** Number of adjustment lines. */
  adjustmentCount: number;
}

/** Breakdown of the lines; null when an amount cannot be read (then the footer shows the API total only). */
export function invoiceTotals(lines: TotalsLine[]): InvoiceTotals | null {
  let approved = BigInt(0);
  let adjustments = BigInt(0);
  let adjustmentCount = 0;
  for (const l of lines) {
    const c = cents(l.amount);
    if (c === null) return null;
    if (l.kind === "adjustment") {
      adjustments += c;
      adjustmentCount += 1;
    } else {
      approved += c;
    }
  }
  return { approved: decimal(approved), adjustments: adjustmentCount ? decimal(adjustments) : null, adjustmentCount };
}

export interface DueDates {
  /** The last schedule date: when the whole amount is due (the Figma "Due date"). */
  due: string | null;
  /** The next open schedule date, when it differs from `due`. */
  next: string | null;
}

/** Due date = the latest schedule row of a live invoice (ISO days compare as text); `next` = the API's next due date
 *  when it is earlier. A cancelled row does not count. */
export function invoiceDueDates(
  schedule: { due_date: string; state: string }[],
  nextDueDate: string | null,
): DueDates {
  const days = schedule.filter((r) => r.state !== "cancelled").map((r) => r.due_date).sort();
  const due = days.length ? days[days.length - 1] : null;
  return { due, next: nextDueDate && nextDueDate !== due ? nextDueDate : null };
}
