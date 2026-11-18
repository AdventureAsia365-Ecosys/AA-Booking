// ui/booking/fxInForce.ts
// AA-1128 (S283 UI audit, Booking batch B2): the currencies that can price today. Only USD and the currencies with an
// approved FX rate in force (GET /booking/pricing/fx-rates, status "in_force") can be quoted; the other entries of a
// fixed currency list would only answer 422 "no FX rate". Pure helpers here; the hook is in fxInForceQuery.ts.
//
// Import-free on purpose: fxInForce.test.mjs loads this file with Node's type stripping (`npm run test:unit`).

/** USD first, then every currency with an FX row in force, each once, sorted. */
export function currenciesInForce(rows: { currency: string; status: string }[]): string[] {
  const codes = new Set<string>();
  for (const r of rows) if (r.status === "in_force" && r.currency && r.currency !== "USD") codes.add(r.currency.trim());
  return ["USD", ...[...codes].sort()];
}

/** The options of a currency select: each candidate with whether it can price today. `inForce` null = unknown (the FX
 *  list could not be read, e.g. no pricing.read): every candidate stays usable, the API still answers 422 inline. */
export function currencyOptions(
  candidates: readonly string[],
  inForce: readonly string[] | null,
  current?: string,
): { code: string; usable: boolean }[] {
  const list = current && !candidates.includes(current) ? [...candidates, current] : [...candidates];
  return list.map((code) => ({ code, usable: inForce === null || code === "USD" || inForce.includes(code) }));
}
