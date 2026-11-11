// e2e/signed-in/departures.spec.ts
// AA-1073 signed-in smoke (c): a trip plan's Departures tab lists its fixed departures (minimum, sales close, decision),
// a row opens the departure drawer with its rules (seats, minimum, sales cutoff, decision days) and the agency
// allotments section, and the B2B desk shows the "Departures at risk" tile and its Departures & incidents tab.
// Read-only: the drawer's Save is never clicked (the E2E user gets 403 e2e_read_only on writes anyway).
import { expect, test, type Page } from "@playwright/test";
import { assertClean, bffGet, capture, firstTableItem, pageSettled, tableSettled, watch } from "./helpers/page";

type DeskDeparture = { id: string; source_id: string; start_date: string; at_risk: boolean; allotted: number; status: string };
type PlanItem = { source_id: string };
type DepartureList = { items: { id: string; at_risk: boolean; allotted: number }[] };

/** The plan + departure to open: an at-risk one first, then one with allotments, then any; null when Dev has none. */
async function pickDeparture(page: Page): Promise<{ sourceId: string; departureId: string | null; atRisk: boolean } | null> {
  const desk = await bffGet<{ items: DeskDeparture[] }>(page, "b2b-desk/departures");
  const items = (desk.body?.items ?? []).filter((d) => d.status !== "cancelled");
  const best = items.find((d) => d.at_risk) ?? items.find((d) => d.allotted > 0) ?? items[0];
  if (best) return { sourceId: best.source_id, departureId: best.id, atRisk: best.at_risk };
  // Nothing in the desk window: look for any plan with departures, else the first plan (the empty state is checked).
  const plans = await bffGet<{ items: PlanItem[] }>(page, "catalog/trip-plans?limit=10");
  for (const p of plans.body?.items ?? []) {
    const deps = await bffGet<DepartureList>(page, `catalog/trip-plans/${encodeURIComponent(p.source_id)}/departures`);
    const d = deps.body?.items[0];
    if (d) return { sourceId: p.source_id, departureId: d.id, atRisk: d.at_risk };
  }
  const first = plans.body?.items[0];
  return first ? { sourceId: first.source_id, departureId: null, atRisk: false } : null;
}

test("trip plan Departures tab and the departure drawer (rules, allotments)", async ({ page, baseURL }) => {
  const w = watch(page, new URL(baseURL!).origin);
  await page.goto("/catalog/trip-plans", { waitUntil: "domcontentloaded", timeout: 30_000 });
  const pick = await pickDeparture(page);
  test.skip(pick === null, "Dev has no trip plan in the Booking catalog");
  const planPath = `/catalog/trip-plans/${encodeURIComponent(pick!.sourceId)}`;

  await page.goto(`${planPath}?tab=departures`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  await expect(page.getByRole("tab", { name: "Departures" }).first()).toHaveAttribute("aria-selected", "true", { timeout: 15_000 });
  const state = await tableSettled(page.locator("main").first());
  test.info().annotations.push({ type: "departures", description: `plan departures: ${state}; at risk picked: ${pick!.atRisk}` });
  await capture(page, "trip-plan-departures", async () => {
    await pageSettled(page);
    await tableSettled(page.locator("main").first());
  }, () => firstTableItem(page));

  if (state === "empty" || pick!.departureId === null) {
    test.info().annotations.push({ type: "skip-step", description: "no departure on Dev: the drawer was not opened" });
  } else {
    // A row click opens the drawer and puts the departure in the URL.
    await page.locator("main [data-kit-row]").first().click({ timeout: 10_000 });
    const drawer = page.getByRole("dialog");
    await expect(drawer, "the departure drawer opens on a row click").toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(/[?&]departure=/, { timeout: 10_000 });

    // The picked departure (at risk when Dev has one), by URL.
    await page.goto(`${planPath}?tab=departures&departure=${encodeURIComponent(pick!.departureId)}`, {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
    const openDrawer = async () => {
      await pageSettled(page);
      await expect(page.getByRole("dialog")).toBeVisible({ timeout: 30_000 });
      await expect(page.getByRole("dialog").getByRole("region", { name: "Agency allotments" })).toBeVisible({ timeout: 15_000 });
      // The allotments list loads on its own: wait for its answer (a list or "No allotment"), never the load error.
      const allotments = page.getByRole("dialog").getByRole("region", { name: "Agency allotments" });
      await expect(allotments.getByTestId("allotments").or(allotments.getByText("No allotment on this departure."))).toBeVisible({
        timeout: 30_000,
      });
    };
    await openDrawer();
    const d = page.getByRole("dialog");
    await expect(d.getByText(/^Departure \S/).first(), "the drawer title").toBeVisible({ timeout: 10_000 });
    for (const label of ["Seats", "Minimum to operate", "Sales cutoff (days)", "Decision (days before)"]) {
      await expect(d.getByText(label, { exact: true }).first(), `rule field ${label}`).toBeVisible({ timeout: 10_000 });
    }
    await expect(d.getByRole("heading", { name: "Agency allotments" })).toBeVisible({ timeout: 10_000 });
    if (pick!.atRisk) await expect(d.getByRole("status").filter({ hasText: "Short of its minimum" })).toBeVisible({ timeout: 10_000 });
    await capture(page, "departure-drawer", openDrawer, () => page.getByRole("dialog").getByRole("region", { name: "Agency allotments" }));
  }
  assertClean(w, "trip-plan-departures");
});

test("B2B desk: Departures at risk tile and the departures tab", async ({ page, baseURL }) => {
  const w = watch(page, new URL(baseURL!).origin);
  await page.goto("/b2b-desk", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  const tile = page.getByText("Departures at risk", { exact: true }).first();
  await expect(tile, "the at-risk tile").toBeVisible({ timeout: 15_000 });
  const summary = await bffGet<{ departures_at_risk?: number }>(page, "b2b-desk/summary");
  expect(summary.status, "GET b2b-desk/summary").toBe(200);
  expect(typeof summary.body!.departures_at_risk, "the summary carries departures_at_risk").toBe("number");
  test.info().annotations.push({ type: "at-risk", description: `departures at risk: ${summary.body!.departures_at_risk}` });
  await capture(page, "b2b-desk", async () => {
    await pageSettled(page);
    await expect(page.getByText("Departures at risk", { exact: true }).first()).toBeVisible({ timeout: 15_000 });
  });

  await page.goto("/b2b-desk?tab=departures", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  await tableSettled(page.locator("main").first());
  await capture(page, "b2b-desk-departures", async () => {
    await pageSettled(page);
    await tableSettled(page.locator("main").first());
  }, () => firstTableItem(page));
  assertClean(w, "b2b-desk");
});
