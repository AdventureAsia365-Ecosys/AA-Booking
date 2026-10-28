// e2e/layout/dialogs.spec.ts
// AA-996 slice 5 — two confirm dialogs the Figma audit asked for, against the same mocked API as layout.spec.ts
// (fixtures.ts; nothing is sent: every write would be an unmocked call and fail the test):
//   - Remind on an invoice opens a confirm (Figma SEND REMAINDER) instead of posting at once; Cancel closes it.
//   - Deactivate a staff member requires a reason (Figma BLOCK ADMIN "Blockage Reason"; stored in the audit row).
// At 1440 x 900 and 390 x 844: the dialog fits, the page never scrolls sideways, the console stays clean.

import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { answer, CLOCK } from "./fixtures";
import { OUT } from "./paths";

function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}

type Seen = { unmocked: string[]; writes: string[]; consoleErrors: string[] };

async function prepare(page: Page, baseURL: string): Promise<Seen> {
  const seen: Seen = { unmocked: [], writes: [], consoleErrors: [] };
  const origin = new URL(baseURL).origin;
  await page.clock.setFixedTime(new Date(CLOCK));
  await page.context().addCookies([{ name: "bk_access", value: fakeAccessToken(), url: origin }]);
  page.on("console", (m) => {
    if (m.type() === "error") seen.consoleErrors.push(m.text().slice(0, 300));
  });
  page.on("pageerror", (e) => seen.consoleErrors.push(`pageerror: ${String(e).slice(0, 300)}`));
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin === origin) {
      if (!url.pathname.startsWith("/api/booking/")) return route.continue();
      if (req.method() !== "GET") seen.writes.push(`${req.method()} ${url.pathname}`);
      const a = answer(req.method(), url.pathname.slice("/api/booking/".length), url.searchParams);
      if (!a) {
        seen.unmocked.push(`${req.method()} ${url.pathname}${url.search}`);
        return route.fulfill({ status: 501, json: { detail: "layout_mock_unmocked", message: "No fixture for this call." } });
      }
      return route.fulfill({ status: a.status, json: a.body });
    }
    // Fonts and CIS images are not needed to check the dialogs: answer them locally (no network in the check).
    return route.fulfill({ status: 204, body: "" });
  });
  return seen;
}

async function pageFits(page: Page) {
  const m = await page.evaluate(() => {
    const se = document.scrollingElement as HTMLElement;
    const main = document.querySelector("main");
    return { doc: se.scrollWidth - se.clientWidth, main: main ? main.scrollWidth - main.clientWidth : 0 };
  });
  expect(m.doc, "the page never scrolls sideways").toBeLessThanOrEqual(0);
  expect(m.main, "<main> never scrolls sideways").toBeLessThanOrEqual(0);
}

async function dialogFits(page: Page) {
  const box = await page.getByTestId("confirm-dialog").boundingBox();
  const vw = page.viewportSize()!.width;
  expect(box, "the dialog is on screen").not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(vw);
}

const VIEWPORTS = [
  { name: "1440", viewport: { width: 1440, height: 900 }, mobile: false },
  { name: "390", viewport: { width: 390, height: 844 }, mobile: true },
];

for (const v of VIEWPORTS) {
  test.describe(`AA-996 dialogs at ${v.name}`, () => {
    test.use({ viewport: v.viewport, colorScheme: "light", isMobile: v.mobile, hasTouch: v.mobile });

    test(`invoice Remind asks first and sends nothing until confirmed (${v.name})`, async ({ page, baseURL }) => {
      const seen = await prepare(page, baseURL!);
      await page.goto("/billing/invoices/INV-000004", { waitUntil: "domcontentloaded" });
      await expect(page.getByTestId("invoice-totals")).toBeVisible({ timeout: 15_000 });
      await expect(page.getByTestId("invoice-dates")).toContainText("Due date");
      await expect(page.getByTestId("invoice-pdf-note")).toContainText("PDF: coming later");
      await page.getByRole("button", { name: "Remind" }).click();
      const dialog = page.getByTestId("confirm-dialog");
      await expect(dialog).toContainText("Send a reminder for INV-000004");
      await expect(dialog).toContainText("No e-mail is sent");
      await expect(dialog.getByRole("button", { name: "Send reminder" })).toBeEnabled();
      await dialogFits(page);
      await pageFits(page);
      fs.mkdirSync(OUT, { recursive: true });
      await page.screenshot({ path: path.join(OUT, `invoice-remind-confirm-${v.name}-light.png`) });
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await expect(dialog).toHaveCount(0);
      expect(seen.writes, "opening and cancelling the dialog sends nothing").toEqual([]);
      expect(seen.unmocked).toEqual([]);
      expect(seen.consoleErrors).toEqual([]);
    });

    test(`staff Deactivate needs a reason (${v.name})`, async ({ page, baseURL }) => {
      const seen = await prepare(page, baseURL!);
      await page.goto("/staff/u-001", { waitUntil: "domcontentloaded" });
      const deactivate = page.locator("main").getByRole("button", { name: "Deactivate" });
      await expect(deactivate).toBeVisible({ timeout: 15_000 });
      await deactivate.click();
      const dialog = page.getByTestId("confirm-dialog");
      await expect(dialog).toContainText("Deactivate staff member");
      const confirm = dialog.getByRole("button", { name: "Deactivate" });
      await expect(confirm, "no reason, no deactivation").toBeDisabled();
      await dialog.getByRole("textbox").fill("   ");
      await expect(confirm, "blank is not a reason").toBeDisabled();
      await dialog.getByRole("textbox").fill("Left the company at the end of October");
      await expect(confirm).toBeEnabled();
      await dialogFits(page);
      await pageFits(page);
      fs.mkdirSync(OUT, { recursive: true });
      await page.screenshot({ path: path.join(OUT, `staff-deactivate-reason-${v.name}-light.png`) });
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await expect(dialog).toHaveCount(0);
      expect(seen.writes).toEqual([]);
      expect(seen.unmocked).toEqual([]);
      expect(seen.consoleErrors).toEqual([]);
    });
  });
}
