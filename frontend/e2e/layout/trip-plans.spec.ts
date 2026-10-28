// e2e/layout/trip-plans.spec.ts
// AA-996 (Figma audit slice 2) — trip plan commercial and list, at 1440 x 900 and 390 x 844 (light), against the
// production build with the Booking API mocked (same harness as layout.spec.ts; see playwright.config.ts for why the
// fake session is safe). Nothing is written: a non-GET call fails the test.
//
// Layout: no horizontal page scroll (document and <main>), the drawer never scrolls sideways, console clean, every
// API call answered. Behaviour the audit asked for:
//   - list: duration "N days / N-1 nights", the tour's places as the subtitle (never "No SKU"), the price matrix's
//     "from" price, row menu "Edit commercial" and "Edit in CIS ↗";
//   - commercial drawer: Supplier select, the price matrix rows (priced, hand and override rows), read-only Included /
//     Not included from CIS;
//   - detail: supplier, price from and the price matrix panel;
//   - /cases/new: "Start from a trip plan" lists sellable plans and keeps the picked one.
// Shots: $LAYOUT_SHOTS_DIR (default e2e/layout/results) as slice2-<name>-<width>.png.

import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { answer, CLOCK } from "./fixtures";
import { OUT } from "./paths";

type Collected = { unmocked: string[]; consoleErrors: string[]; writes: string[] };

const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64");

function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}

async function prepare(page: Page, baseURL: string): Promise<Collected> {
  const c: Collected = { unmocked: [], consoleErrors: [], writes: [] };
  const origin = new URL(baseURL).origin;
  await page.clock.setFixedTime(new Date(CLOCK));
  await page.context().addCookies([{ name: "bk_access", value: fakeAccessToken(), url: origin }]);
  page.on("console", (m) => {
    if (m.type() === "error") c.consoleErrors.push(m.text().slice(0, 300));
  });
  page.on("pageerror", (e) => c.consoleErrors.push(`pageerror: ${String(e).slice(0, 300)}`));
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin === origin) {
      if (!url.pathname.startsWith("/api/booking/")) return route.continue();
      if (req.method() !== "GET") c.writes.push(`${req.method()} ${url.pathname}`);
      const a = answer(req.method(), url.pathname.slice("/api/booking/".length), url.searchParams);
      if (!a) {
        c.unmocked.push(`${req.method()} ${url.pathname}${url.search}`);
        return route.fulfill({ status: 501, json: { detail: "layout_mock_unmocked", message: "No fixture for this call." } });
      }
      return route.fulfill({ status: a.status, json: a.body });
    }
    if (req.resourceType() === "image") return route.fulfill({ status: 200, contentType: "image/png", body: PIXEL });
    return route.fulfill({ status: 204, body: "" });
  });
  return c;
}

async function open(page: Page, url: string, ready: string, c: Collected) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  try {
    await expect(page.locator(ready).first()).toBeVisible({ timeout: 15_000 });
  } catch (e) {
    const shown = (await page.locator("main").innerText({ timeout: 2_000 }).catch(() => "")).slice(0, 400);
    throw new Error(`${url}: not ready. unmocked=${JSON.stringify(c.unmocked)} console=${JSON.stringify(c.consoleErrors)} main="${shown}"\n${e}`);
  }
  await page.waitForTimeout(400);
}

async function fits(page: Page, c: Collected) {
  const m = await page.evaluate(() => {
    const se = document.scrollingElement as HTMLElement;
    const main = document.querySelector("main");
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement | null;
    const scroller = dialog ? (Array.from(dialog.querySelectorAll("*")) as HTMLElement[]).find((el) => getComputedStyle(el).overflowY === "auto") : null;
    return {
      doc: se.scrollWidth - se.clientWidth,
      main: main ? main.scrollWidth - main.clientWidth : 0,
      dialogRight: dialog ? dialog.getBoundingClientRect().right - window.innerWidth : 0,
      dialogInner: scroller ? scroller.scrollWidth - scroller.clientWidth : 0,
    };
  });
  expect(c.unmocked, "every Booking API call has a fixture").toEqual([]);
  expect(c.consoleErrors, "console is clean").toEqual([]);
  expect(c.writes, "nothing is written").toEqual([]);
  expect(m.doc, "the page never scrolls sideways").toBeLessThanOrEqual(0);
  expect(m.main, "<main> never scrolls sideways").toBeLessThanOrEqual(0);
  expect(m.dialogRight, "the drawer stays inside the window").toBeLessThanOrEqual(1);
  expect(m.dialogInner, "the drawer body never scrolls sideways").toBeLessThanOrEqual(0);
}

async function shot(page: Page, name: string, width: number) {
  fs.mkdirSync(OUT, { recursive: true });
  await page.screenshot({ path: path.join(OUT, `slice2-${name}-${width}.png`) });
}

const PLAN = "tour-00002";

for (const width of [1440, 390] as const) {
  test.describe(`slice 2 at ${width}`, () => {
    test.use(
      width === 1440
        ? { viewport: { width, height: 900 }, colorScheme: "light" }
        : { viewport: { width, height: 844 }, colorScheme: "light", isMobile: true, hasTouch: true },
    );

    test(`trip plan list at ${width}: duration, places, from price`, async ({ page, baseURL }) => {
      const c = await prepare(page, baseURL!);
      await open(page, "/catalog/trip-plans", '[data-testid="kit-datatable-row"]', c);
      const text = await page.locator("main").innerText();
      expect(text).toMatch(/15 days\s+14 nights/); // two lines in the Duration cell
      expect(text).toContain("Hanoi · Ha Long Bay");
      expect(text).not.toContain("No SKU");
      await shot(page, "list", width);
      await fits(page, c);
      if (width === 1440) {
        expect(text).toContain("Sellable first");
        expect(text).toContain("12,485");
        await page.locator('button[aria-label^="Actions for"]').first().click();
        const menu = page.getByRole("menu");
        await expect(menu.getByRole("menuitem", { name: "Edit commercial" })).toBeVisible();
        await expect(menu.getByRole("menuitem", { name: "Edit in CIS ↗" })).toBeVisible();
        await shot(page, "list-menu", width);
        await page.keyboard.press("Escape");
      }
    });

    test(`commercial drawer at ${width}: supplier, price matrix, inclusions`, async ({ page, baseURL }) => {
      const c = await prepare(page, baseURL!);
      await open(page, `/catalog/trip-plans/${PLAN}/commercial`, '[data-testid="price-row"]', c);
      const drawer = page.getByRole("dialog");
      await expect(drawer.getByText("Supplier", { exact: true })).toBeVisible();
      await expect(drawer.locator("select").filter({ hasText: "No supplier" })).toHaveCount(1);
      await expect(drawer.locator('[data-testid="price-row"]')).toHaveCount(3);
      await expect(drawer.getByText("Not included", { exact: true })).toBeVisible();
      await expect(drawer.getByText(/margin 30\.0 %/)).toBeVisible();
      await expect(drawer.getByText("(override)")).toBeVisible();
      await shot(page, "drawer", width);
      await drawer.getByRole("button", { name: "Add price row" }).click();
      await expect(drawer.locator('[data-testid="price-row"]')).toHaveCount(4);
      await drawer.locator('[data-testid="price-row"]').last().scrollIntoViewIfNeeded();
      await shot(page, "drawer-new-row", width);
      await fits(page, c);
    });

    test(`trip plan detail at ${width}: supplier and price matrix panel`, async ({ page, baseURL }) => {
      const c = await prepare(page, baseURL!);
      await open(page, `/catalog/trip-plans/${PLAN}`, '[data-testid="price-matrix"]', c);
      const text = await page.locator("main").innerText();
      expect(text).toContain("Red River Destination Management Company");
      expect(text).toContain("15 days / 14 nights");
      expect(text).toContain("11+ pax");
      await shot(page, "detail", width);
      await fits(page, c);
    });

    test(`new trip case at ${width}: start from a trip plan`, async ({ page, baseURL }) => {
      const c = await prepare(page, baseURL!);
      await open(page, "/cases/new", 'text="Start from a trip plan"', c);
      await page.getByPlaceholder("Search sellable trip plans by title, SKU or CIS id").click();
      const options = page.getByRole("option");
      await expect(options.first()).toBeVisible();
      await shot(page, "new-case-picker", width);
      await options.first().click();
      await expect(page.locator('[data-testid="trip-plan-picked"]')).toBeVisible();
      await shot(page, "new-case-picked", width);
      await fits(page, c);
    });
  });
}
