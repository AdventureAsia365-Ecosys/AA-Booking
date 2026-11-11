// e2e/signed-in/agency-ops.spec.ts
// AA-1073 signed-in smoke (d): the agency booking operations pages that build on departures (B2B desk tab "Agency
// bookings" and the departure manifest). They ship separately; on a host without them this test is SKIPPED with the
// reason, so the smoke stays green before and after that release.
import { expect, test } from "@playwright/test";
import { assertClean, capture, firstTableItem, pageSettled, tableSettled, watch } from "./helpers/page";

test("B2B desk Agency bookings tab and a departure manifest (when deployed)", async ({ page, baseURL }) => {
  const w = watch(page, new URL(baseURL!).origin);
  await page.goto("/b2b-desk", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  const tab = page.getByRole("tab", { name: /^Agency bookings/ });
  const deployed = await tab.first().waitFor({ state: "visible", timeout: 10_000 }).then(() => true, () => false);
  test.skip(!deployed, "the agency booking operations pages are not deployed on this host yet (no Agency bookings tab on /b2b-desk)");

  await page.goto("/b2b-desk?tab=agency-bookings", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  await tableSettled(page.locator("main").first());
  await capture(page, "b2b-desk-agency-bookings", async () => {
    await pageSettled(page);
    await tableSettled(page.locator("main").first());
  }, () => firstTableItem(page));

  await page.goto("/b2b-desk?tab=departures", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await pageSettled(page);
  await tableSettled(page.locator("main").first());
  const manifest = page.getByRole("link", { name: "Manifest" }).first();
  if (await manifest.waitFor({ state: "visible", timeout: 5_000 }).then(() => true, () => false)) {
    await manifest.click({ timeout: 10_000 });
    await expect(page).toHaveURL(/\/b2b-desk\/manifest\//, { timeout: 15_000 });
    await capture(page, "departure-manifest", () => pageSettled(page));
  } else {
    test.info().annotations.push({ type: "skip-step", description: "no departure with sold seats on the desk: no manifest to open" });
  }
  assertClean(w, "agency-ops");
});
