// ui/booking/Money.tsx
// AA-683 — money is always an amount plus its ISO code, never a bare "$" (spec §4, ADR 0005: USD base).
// Amounts arrive from the API as decimal strings; they are formatted, never re-computed here, and always with the
// currency's minor units (S236 review finding 10: "1,845 USD" next to "2,390.50 EUR" read as two formats).

const minorUnitsCache = new Map<string, number>();

/** The currency's minor units (USD 2, VND 0, KWD 3), so amounts always show them: "1,845.00 USD". */
export function minorUnits(code: string): number {
  if (!/^[A-Z]{3}$/.test(code)) return 2;
  let digits = minorUnitsCache.get(code);
  if (digits === undefined) {
    try {
      digits = new Intl.NumberFormat("en", { style: "currency", currency: code }).resolvedOptions().maximumFractionDigits ?? 2;
    } catch {
      digits = 2;
    }
    minorUnitsCache.set(code, digits);
  }
  return digits;
}

export function formatMoney(amount: string | number | null | undefined, currency: string | null | undefined): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const n = typeof amount === "number" ? amount : Number(amount);
  if (!Number.isFinite(n)) return "—";
  const code = (currency || "").toUpperCase().trim();
  const digits = code ? minorUnits(code) : 2;
  const formatted = new Intl.NumberFormat("en-GB", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(n);
  return code ? `${formatted} ${code}` : formatted;
}

export function Money({
  amount,
  currency,
  className = "",
  strong = false,
}: {
  amount: string | number | null | undefined;
  currency: string | null | undefined;
  className?: string;
  strong?: boolean;
}) {
  return (
    <span className={`bk-tabular whitespace-nowrap ${strong ? "font-semibold text-ink" : ""} ${className}`}>
      {formatMoney(amount, currency)}
    </span>
  );
}
