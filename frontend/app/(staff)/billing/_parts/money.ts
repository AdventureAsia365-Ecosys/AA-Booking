// app/(staff)/billing/_parts/money.ts
// AA-682 S3 — exact money arithmetic for the invoice forms: amounts as integer cents (BigInt), never floats. The API is
// the authority (it recomputes every line and refuses a schedule that does not add up); these helpers only preview.

export const AMOUNT_RE = /^-?\d{1,14}(\.\d{1,2})?$/;

export function toCents(value: string | null | undefined): bigint | null {
  const s = (value ?? "").trim();
  if (!AMOUNT_RE.test(s)) return null;
  const neg = s.startsWith("-");
  const [whole, frac = ""] = s.replace("-", "").split(".");
  const cents = BigInt(whole) * BigInt(100) + BigInt((frac + "00").slice(0, 2));
  return neg ? -cents : cents;
}

export function fromCents(cents: bigint): string {
  const neg = cents < BigInt(0);
  const abs = neg ? -cents : cents;
  const whole = abs / BigInt(100);
  const frac = abs % BigInt(100);
  return `${neg ? "-" : ""}${whole.toString()}.${frac.toString().padStart(2, "0")}`;
}

/** round(quantity x unit price, 2), half away from zero (the bk-028 line rule). */
export function lineCents(quantity: string, unitPrice: string): bigint | null {
  const q = toCents(quantity);
  const u = toCents(unitPrice);
  if (q === null || u === null || q <= BigInt(0)) return null;
  const product = q * u; // in 1/10000
  const neg = product < BigInt(0);
  const abs = neg ? -product : product;
  const rounded = (abs + BigInt(50)) / BigInt(100);
  return neg ? -rounded : rounded;
}

export function sumCents(values: (bigint | null)[]): bigint | null {
  let total = BigInt(0);
  for (const v of values) {
    if (v === null) return null;
    total += v;
  }
  return total;
}
