// e2e/signed-in/finance.spec.ts
// AA-1074 signed-in smoke (b): Operations → Costs & profit on Dev. The three tabs (Profit, Departure costs, Purchase
// orders) render their table with live rows or the kit empty state (never the load error), and a trip case with
// supplier bookings shows its costs in USD on the Suppliers tab (the CostVsBilled panel, `case-costs-usd`).
// S277 UAT fixes: the Tour column of Departure costs and Profit → Per departure names the tour (never the raw catalog
// id), and the case panel prints the USD totals once (the per-currency line keeps only other currencies).
// Read-only: no button that writes is clicked (the E2E user gets 403 e2e_read_only on writes anyway).
import { expect, test, type Page } from "@playwright/test";
import { assertClean, bffGet, capture, firstTableItem, pageSettled, tableSettled, watch } from "./helpers/page";

type Page_<T> = { items: T[]; total: number };
type CaseProfit = { code: string; planned_cost_usd: string; actual_cost_usd: string; cancelled: boolean };
type CaseListItem = { code: string };
type CaseCosts = { lines: unknown[]; usd?: { cost: string } };

// A raw catalog key (a UUID) in a Tour cell: the tour title is missing from the API or the page (S277 UAT finding 1).
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/** The rows of the settled table in <main> name their tour, not the raw catalog key. */
async function expectNoRawTourKey(page: Page, where: string): Promise<void> {
  const rows = await page.locator("main [data-kit-row]").allInnerTexts();
  expect(rows.join("\n"), `${where}: a Tour cell shows the raw catalog id instead of the tour name`).not.toMatch(UUID);
}

const TABS = [
  { key: "profit", label: "Profit", query: "?period=all" },
  { key: "departures", label: "Departure costs", query: "?tab=departures" },
  { key: "orders", label: "Purchase orders", query: "?tab=orders" },
] as const;

/** A trip case whose Suppliers tab has supplier bookings (so the USD line shows), or null when Dev has none. */
async function caseWithCosts(page: Page): Promise<string | null> {
  const profit = await bffGet<Page_<CaseProfit>>(page, "finance/profit/cases?limit=200");
  const fromProfit = (profit.body?.items ?? []).filter((c) => !c.cancelled && (Number(c.planned_cost_usd) > 0 || Number(c.actual_cost_usd) > 0));
  const candidates = fromProfit.map((c) => c.code);
  if (candidates.length === 0) {
    const list = await bffGet<Page_<CaseListItem>>(page, "trip-cases?limit=25&sort=-updated_at");
    candidates.push(...(list.body?.items ?? []).map((c) => c.code));
  }
  for (const code of candidates.slice(0, 25)) {
    const costs = await bffGet<CaseCosts>(page, `trip-cases/${encodeURIComponent(code)}/supplier-costs`);
    if (costs.status === 200 && (costs.body?.lines.length ?? 0) > 0) return code;
  }
  return null;
}

test("Costs & profit: three tabs render with data or the empty state", async ({ page, baseURL }) => {
  const w = watch(page, new URL(baseURL!).origin);
  await page.goto("/finance", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  await expect(page.getByRole("heading", { name: "Costs & profit" }).first()).toBeVisible({ timeout: 15_000 });
  const tablist = page.getByRole("tablist", { name: "Costs and profit" });
  for (const t of TABS) await expect(tablist.getByRole("tab", { name: t.label }), `tab ${t.label}`).toBeVisible({ timeout: 10_000 });

  const results: string[] = [];
  for (const t of TABS) {
    await tablist.getByRole("tab", { name: t.label }).click({ timeout: 10_000 });
    await expect(tablist.getByRole("tab", { name: t.label })).toHaveAttribute("aria-selected", "true", { timeout: 10_000 });
    const main = page.locator("main").first();
    const state = await tableSettled(main);
    results.push(`${t.label}: ${state}`);
    if (state === "rows" && t.key === "departures") await expectNoRawTourKey(page, t.label);
    await page.goto(`/finance${t.query}`, { waitUntil: "domcontentloaded", timeout: 30_000 });
    await capture(page, `finance-${t.key}`, async () => {
      await pageSettled(page);
      await tableSettled(page.locator("main").first());
    }, () => firstTableItem(page));
  }
  // Profit, per departure: the same Tour column (GET /finance/profit/departures carries tour_title).
  await page.goto("/finance?view=departures&period=all", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  const perDeparture = await tableSettled(page.locator("main").first());
  results.push(`Profit per departure: ${perDeparture}`);
  if (perDeparture === "rows") await expectNoRawTourKey(page, "Profit per departure");
  test.info().annotations.push({ type: "tables", description: results.join("; ") });
  assertClean(w, "finance");
});

test("trip case Suppliers tab shows the costs in USD", async ({ page, baseURL }) => {
  const w = watch(page, new URL(baseURL!).origin);
  await page.goto("/finance", { waitUntil: "domcontentloaded", timeout: 30_000 });
  const code = await caseWithCosts(page);
  test.skip(code === null, "Dev has no trip case with a supplier booking: the USD cost panel has nothing to show");
  test.info().annotations.push({ type: "case", description: "a trip case with supplier bookings was found" });
  await page.goto(`/cases/${encodeURIComponent(code!)}?tab=suppliers`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  const usd = page.getByTestId("case-costs-usd");
  await expect(usd, "the cost panel shows the USD line").toBeVisible({ timeout: 30_000 });
  await expect(usd).toContainText("USD");
  await expect(usd).toContainText(/Best cost/);
  // S277 UAT finding 2: next to the USD summary the per-currency line keeps only other currencies, so no second
  // "USD: net … · billed … · variance …" line repeats it.
  expect(await page.locator("main").first().innerText(), "the USD totals are shown once").not.toMatch(/USD:\s*net\s/);
  await capture(page, "case-costs-usd", async () => {
    await pageSettled(page);
    await expect(page.getByTestId("case-costs-usd")).toBeVisible({ timeout: 30_000 });
  }, () => page.getByTestId("case-costs-usd"));
  assertClean(w, "case-costs-usd");
});
