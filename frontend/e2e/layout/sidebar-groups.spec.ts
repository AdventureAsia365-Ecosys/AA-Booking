// e2e/layout/sidebar-groups.spec.ts
// S264 — the sidebar nav groups fold (kit core NavGroup + useNavGroups, scope "booking"), against the same mocked API
// as layout.spec.ts (fixtures.ts; no real sign-in). Checks:
//   - 1440: on /cases the Sales group (current page) is open and the others are folded; unfolding Supply is kept after a
//     reload; folding Sales hides its items until the next page load, which opens it again;
//   - 960 (compact window, forced icon rail): no group header, every item of every group is shown;
//   - 390: the phone drawer shows the group headers and folds like the desktop sidebar;
//   - AA-1037 (root ADR 0019 d8): B2B leads (Partners, Settlement, B2B desk right after Overview); AA-1046: the "B2C
//     records" group is gone with the customer pages, and Operations lists Supplier bills.
// Screenshots (light and dark, 1440 expanded and folded, 390 drawer) land in $LAYOUT_SHOTS_DIR/sidebar-groups-*.png.

import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { answer, CLOCK } from "./fixtures";
import { OUT } from "./paths";

function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}

async function prepare(page: Page, baseURL: string): Promise<string[]> {
  const consoleErrors: string[] = [];
  const origin = new URL(baseURL).origin;
  await page.clock.setFixedTime(new Date(CLOCK));
  await page.context().addCookies([{ name: "bk_access", value: fakeAccessToken(), url: origin }]);
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text().slice(0, 300));
  });
  page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 300)}`));
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin === origin) {
      if (!url.pathname.startsWith("/api/booking/")) return route.continue();
      const a = answer(req.method(), url.pathname.slice("/api/booking/".length), url.searchParams);
      if (!a) return route.fulfill({ status: 501, json: { detail: "layout_mock_unmocked" } });
      return route.fulfill({ status: a.status, json: a.body });
    }
    return route.fulfill({ status: 204, body: "" });
  });
  return consoleErrors;
}

const toggle = (page: Page, id: string) => page.getByTestId(`nav-group-${id}-toggle`);
const items = (page: Page, id: string) => page.getByTestId(`nav-group-${id}`).getByRole("group");

async function openCases(page: Page) {
  await page.goto("/cases", { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("admin-sidebar")).toBeVisible();
  // Hydrated once the stored state is read (a fold below only sticks with the handlers live).
  await expect(toggle(page, "sales")).toHaveAttribute("aria-expanded", "true", { timeout: 15_000 });
}

for (const scheme of ["light", "dark"] as const) {
  test.describe(`sidebar groups at 1440 (${scheme})`, () => {
    test.use({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });

    test("the current page group is open, the others fold and remember", async ({ page, baseURL }) => {
      const errors = await prepare(page, baseURL!);
      await openCases(page);
      await expect(items(page, "sales")).toBeVisible();
      await expect(page.getByRole("button", { name: "Trip Cases" })).toBeVisible();
      for (const id of ["b2b", "supply", "operations", "catalog-from-cis", "admin"]) {
        await expect(toggle(page, id)).toHaveAttribute("aria-expanded", "false");
        await expect(items(page, id)).toBeHidden();
      }
      await page.getByTestId("admin-sidebar").screenshot({ path: path.join(OUT, `sidebar-groups-1440-folded-${scheme}.png`) });

      await toggle(page, "supply").click();
      await expect(toggle(page, "supply")).toHaveAttribute("aria-expanded", "true");
      await expect(page.getByRole("button", { name: "Hotels" })).toBeVisible();
      await page.getByTestId("admin-sidebar").screenshot({ path: path.join(OUT, `sidebar-groups-1440-open-${scheme}.png`) });

      await toggle(page, "sales").click();
      await expect(toggle(page, "sales")).toHaveAttribute("aria-expanded", "false");
      await expect(items(page, "sales")).toBeHidden();

      await page.reload({ waitUntil: "domcontentloaded" });
      await openCases(page); // Sales open again: it holds the current page
      await expect(toggle(page, "supply")).toHaveAttribute("aria-expanded", "true");
      expect(errors, "no console error").toEqual([]);
    });
  });
}

test.describe("B2B first (1440)", () => {
  test.use({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });

  test("B2B follows Overview and no B2C records group is left", async ({ page, baseURL }) => {
    const errors = await prepare(page, baseURL!);
    await openCases(page);
    const ids = await page
      .getByTestId("admin-sidebar")
      .locator("[data-nav-group]")
      .evaluateAll((els) => els.map((e) => e.getAttribute("data-nav-group")));
    expect(ids.slice(0, 3)).toEqual(["overview", "b2b", "sales"]);
    expect(ids).not.toContain("b2c-records-read-only");
    expect(ids.slice(-1)).toEqual(["admin"]);
    await toggle(page, "b2b").click();
    for (const name of ["Partners", "Settlement", "B2B desk"]) {
      await expect(items(page, "b2b").getByRole("button", { name, exact: true })).toBeVisible();
    }
    await expect(items(page, "sales").getByRole("button", { name: "Customers" })).toHaveCount(0);
    await toggle(page, "operations").click();
    await expect(items(page, "operations").getByRole("button", { name: "Supplier bills" })).toBeVisible();
    expect(errors, "no console error").toEqual([]);
  });
});

test.describe("sidebar groups in the compact window (960)", () => {
  test.use({ viewport: { width: 960, height: 900 }, colorScheme: "light" });

  test("the icon rail has no group header and shows every item", async ({ page, baseURL }) => {
    await prepare(page, baseURL!);
    await page.goto("/cases", { waitUntil: "domcontentloaded" });
    await expect(page.getByTestId("nav-group-supply")).toHaveAttribute("data-open", "true", { timeout: 15_000 });
    await expect(page.locator('[data-testid$="-toggle"][aria-expanded]')).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Hotels" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Staff" })).toBeVisible();
  });
});

for (const scheme of ["light", "dark"] as const) {
  test.describe(`sidebar groups in the phone drawer (390, ${scheme})`, () => {
    test.use({ viewport: { width: 390, height: 844 }, colorScheme: scheme });

    test("the drawer shows the group headers and folds", async ({ page, baseURL }) => {
      await prepare(page, baseURL!);
      await page.goto("/cases", { waitUntil: "domcontentloaded" });
      await page.getByRole("button", { name: "Open navigation menu" }).click();
      await expect(toggle(page, "sales")).toHaveAttribute("aria-expanded", "true", { timeout: 15_000 });
      await expect(toggle(page, "supply")).toHaveAttribute("aria-expanded", "false");
      await toggle(page, "supply").click();
      await expect(page.getByRole("button", { name: "Hotels" })).toBeVisible();
      await page.screenshot({ path: path.join(OUT, `sidebar-groups-390-drawer-${scheme}.png`) });
    });
  });
}
