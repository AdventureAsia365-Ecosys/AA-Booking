// e2e/layout/case-workspace.spec.ts
// AA-996 (Figma audit slice 4) — the case workspace, the proposal builder and the trip-check board, at 1440 x 900 and
// 390 x 844 (light), against the production build with the Booking API mocked (same harness as layout.spec.ts; see
// playwright.config.ts for why the fake session is safe). Fixtures: case-fixtures.ts first, then fixtures.ts.
//
// Layout: no horizontal page scroll (document and <main>), tables fit their frame at 1440 with the last column in
// view, console clean, every API call answered. Behaviour the audit asked for:
//   - Overview: trip facts are the main column (left of People), the empty legs card is one line, the header names the
//     customer and the agent, Countries falls back to the legs, the inquiry shows the client's message and budget;
//   - Proposals tab: Preview / Send / Discard per row (Send off for an unpriced draft, Discard only on the draft);
//   - builder: the live quote's 403 e2e_read_only is explained (not "No access to pricing"); a locked version shows its
//     saved price and never asks for a live quote;
//   - trip checks: status tabs with counts, per-trip progress, Overdue = the open list with due=overdue, the drawer's
//     trip context;
//   - Convert / Decline dialogs keep their buttons in the Modal footer band.
// Shots: $LAYOUT_SHOTS_DIR (default e2e/layout/results) as slice4-<name>-<width>.png.

import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { CUSTOM_CASE, INQUIRY_CASE, caseAnswer } from "./case-fixtures";
import { answer, CLOCK } from "./fixtures";
import { OUT } from "./paths";

type Collected = { unmocked: string[]; consoleErrors: string[]; posts: string[] };

const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64");
const fontCache = new Map<string, { status: number; headers: Record<string, string>; body: Buffer }>();

function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}

async function prepare(page: Page, baseURL: string): Promise<Collected> {
  const c: Collected = { unmocked: [], consoleErrors: [], posts: [] };
  const origin = new URL(baseURL).origin;
  await page.clock.setFixedTime(new Date(CLOCK));
  await page.context().addCookies([{ name: "bk_access", value: fakeAccessToken(), url: origin }]);
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // The builder's live quote is answered 403 on purpose (the audit's read-only session); Chromium logs that load.
    if (/status of 403/.test(m.text()) && /quote-preview|Failed to load resource/.test(m.text())) return;
    c.consoleErrors.push(m.text().slice(0, 300));
  });
  page.on("pageerror", (e) => c.consoleErrors.push(`pageerror: ${String(e).slice(0, 300)}`));
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin === origin) {
      if (!url.pathname.startsWith("/api/booking/")) return route.continue();
      const p = url.pathname.slice("/api/booking/".length);
      if (req.method() !== "GET") c.posts.push(`${req.method()} ${p}`);
      const a = caseAnswer(req.method(), p) ?? answer(req.method(), p, url.searchParams);
      if (!a) {
        c.unmocked.push(`${req.method()} ${url.pathname}${url.search}`);
        return route.fulfill({ status: 501, json: { detail: "layout_mock_unmocked", message: "No fixture for this call." } });
      }
      return route.fulfill({ status: a.status, json: a.body });
    }
    if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
      let hit = fontCache.get(req.url());
      if (!hit) {
        const res = await route.fetch();
        hit = { status: res.status(), headers: res.headers(), body: await res.body() };
        if (res.ok()) fontCache.set(req.url(), hit);
      }
      return route.fulfill(hit);
    }
    if (req.resourceType() === "image") return route.fulfill({ status: 200, contentType: "image/png", body: PIXEL });
    return route.fulfill({ status: 204, body: "" });
  });
  return c;
}

type Measure = {
  innerWidth: number;
  docScrollWidth: number;
  docClientWidth: number;
  mainScrollWidth: number | null;
  mainClientWidth: number | null;
  tables: { head: string; boxClient: number; boxScroll: number; lastHeaderVisible: boolean | null }[];
};

async function measure(page: Page): Promise<Measure> {
  return page.evaluate(() => {
    const se = document.scrollingElement as HTMLElement;
    const main = document.querySelector("main");
    const box = (t: Element): HTMLElement | null => {
      let el = t.parentElement;
      while (el && el !== document.body) {
        const s = getComputedStyle(el);
        if (s.overflowX === "auto" || s.overflowX === "scroll") return el;
        el = el.parentElement;
      }
      return null;
    };
    const tables = Array.from(document.querySelectorAll("main table"))
      .filter((t) => (t as HTMLElement).offsetParent !== null)
      .map((t) => {
        const b = box(t) ?? (t.parentElement as HTMLElement);
        const heads = Array.from(t.querySelectorAll("thead th"));
        const last = heads[heads.length - 1] as HTMLElement | undefined;
        let visible: boolean | null = null;
        if (last) {
          const r = last.getBoundingClientRect();
          const f = b.getBoundingClientRect();
          visible = r.right <= f.right + 1 && r.left >= f.left - 1;
        }
        return { head: heads.map((h) => (h as HTMLElement).innerText.trim()).join("|").slice(0, 80), boxClient: b.clientWidth, boxScroll: b.scrollWidth, lastHeaderVisible: visible };
      });
    return {
      innerWidth: window.innerWidth,
      docScrollWidth: se.scrollWidth,
      docClientWidth: se.clientWidth,
      mainScrollWidth: main ? main.scrollWidth : null,
      mainClientWidth: main ? main.clientWidth : null,
      tables,
    };
  });
}

function pageChecks(m: Measure, c: Collected) {
  expect(c.unmocked, "every Booking API call has a fixture").toEqual([]);
  expect(c.consoleErrors, "console is clean").toEqual([]);
  expect(m.docScrollWidth, "no horizontal page scroll (document)").toBeLessThanOrEqual(m.docClientWidth);
  expect(m.docScrollWidth, "no horizontal page scroll (viewport)").toBeLessThanOrEqual(m.innerWidth);
  if (m.mainScrollWidth !== null && m.mainClientWidth !== null) {
    expect(m.mainScrollWidth, "no horizontal scroll in <main>").toBeLessThanOrEqual(m.mainClientWidth);
  }
}

async function open(page: Page, url: string, ready: string, c: Collected) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  try {
    await expect(page.locator(ready).first()).toBeVisible({ timeout: 15_000 });
  } catch (e) {
    const shown = (await page.locator("main").innerText({ timeout: 2_000 }).catch(() => "")).slice(0, 400);
    throw new Error(`${url}: not ready. unmocked=${JSON.stringify(c.unmocked)} console=${JSON.stringify(c.consoleErrors)} main="${shown}"\n${e}`);
  }
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  await page.waitForTimeout(400);
}

async function shot(page: Page, name: string, width: number) {
  fs.mkdirSync(OUT, { recursive: true });
  await page.screenshot({ path: path.join(OUT, `slice4-${name}-${width}.png`) });
}

const ROW = '[data-testid="kit-datatable-row"]';
const PAGES = [
  { name: "case-overview", url: `/cases/${CUSTOM_CASE}`, ready: '[data-testid="trip-facts"]', table: false },
  { name: "case-proposals", url: `/cases/${CUSTOM_CASE}?tab=proposals`, ready: ROW, table: true },
  { name: "builder-draft", url: `/cases/${CUSTOM_CASE}/proposals/3`, ready: '[data-testid="quote-read-only"]', table: false },
  { name: "builder-locked", url: `/cases/${CUSTOM_CASE}/proposals/2`, ready: '[data-testid="locked-price"]', table: false },
  // The trip-checks table width at 1440 (Status cut at the card edge) is audit slice 1 (PR #126: truncated Check /
  // Customer / Owner cells); here the progress chip must not make the Trip case column wider (behaviour test below).
  { name: "trip-checks", url: "/trip-checks", ready: '[data-testid="trip-progress"]', table: false },
] as const;

for (const width of [1440, 390] as const) {
  test.describe(`slice 4 at ${width}`, () => {
    test.use(
      width === 1440
        ? { viewport: { width, height: 900 }, colorScheme: "light" }
        : { viewport: { width, height: 844 }, colorScheme: "light", isMobile: true, hasTouch: true },
    );
    for (const pc of PAGES) {
      test(`${pc.name} at ${width}: fits, console clean`, async ({ page, baseURL }) => {
        const c = await prepare(page, baseURL!);
        // On a phone the kit DataTable shows cards; the progress chip is in the same cell, so the ready marker holds.
        await open(page, pc.url, pc.ready, c);
        const m = await measure(page);
        await shot(page, pc.name, width);
        pageChecks(m, c);
        if (width === 1440 && pc.table) {
          expect(m.tables.length, "the page shows its table").toBeGreaterThan(0);
          for (const t of m.tables) {
            expect(t.boxScroll, `table "${t.head}" fits its frame at 1440`).toBeLessThanOrEqual(t.boxClient);
            expect(t.lastHeaderVisible, `table "${t.head}": last column in view`).not.toBe(false);
          }
        }
      });
    }
  });
}

test.describe("slice 4 behaviour at 1440", () => {
  test.use({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });

  test("overview: trip facts lead the main column, header names customer and agent", async ({ page, baseURL }) => {
    const c = await prepare(page, baseURL!);
    await open(page, `/cases/${CUSTOM_CASE}`, '[data-testid="trip-facts"]', c);
    const meta = page.getByTestId("case-meta");
    await expect(meta).toContainText("Alexandra Catherine Montgomery-Whitfield");
    await expect(meta).toContainText("Agent: Pham Quoc Bao-Long Sales Lead");
    await expect(meta).toContainText("12 Nov 2026 – 26 Nov 2026 (15 days)");
    const facts = page.getByTestId("trip-facts");
    await expect(facts).toContainText("From the trip legs");
    await expect(facts).toContainText("Laos, Vietnam");
    await expect(facts).toContainText("2 / 0"); // children null reads 0, not "—"
    await expect(facts).toContainText("V2");
    await expect(page.getByTestId("legs-empty")).toBeVisible();
    await expect(page.getByTestId("inquiry-prefs")).toContainText("Estimated budget");
    await expect(page.getByTestId("inquiry-prefs")).toContainText("4,000 – 5,500 EUR");
    const factsBox = (await facts.boundingBox())!;
    const peopleBox = (await page.getByText("Assigned sales", { exact: true }).boundingBox())!;
    expect(factsBox.x + factsBox.width, "trip facts are the main column, left of the People rail").toBeLessThan(peopleBox.x);
    expect(factsBox.width, "trip facts are the wide column").toBeGreaterThan(600);
    const inquiryBox = (await page.getByTestId("inquiry-prefs").boundingBox())!;
    expect(factsBox.y, "trip facts sit above the TripPlanner inquiry").toBeLessThan(inquiryBox.y);
    expect(c.unmocked).toEqual([]);
    expect(c.consoleErrors).toEqual([]);
  });

  test("proposals tab: row actions follow the version state", async ({ page, baseURL }) => {
    const c = await prepare(page, baseURL!);
    await open(page, `/cases/${CUSTOM_CASE}?tab=proposals`, ROW, c);
    await expect(page.getByTestId("version-actions")).toHaveCount(3);
    await expect(page.getByRole("button", { name: `Preview ${CUSTOM_CASE} V2` })).toBeEnabled();
    await expect(page.getByRole("button", { name: `Send ${CUSTOM_CASE} V3 to the customer` })).toBeDisabled(); // unpriced draft
    await expect(page.getByRole("button", { name: `Send ${CUSTOM_CASE} V2 to the customer` })).toBeDisabled(); // approved
    await expect(page.getByRole("button", { name: `Discard draft ${CUSTOM_CASE} V2` })).toBeDisabled();
    await page.getByRole("button", { name: `Discard draft ${CUSTOM_CASE} V3` }).click();
    const dialog = page.getByTestId("confirm-dialog");
    await expect(dialog).toContainText(`Discard draft ${CUSTOM_CASE} V3?`);
    expect(new URL(page.url()).pathname, "an action never opens the builder").toBe(`/cases/${CUSTOM_CASE}`);
    await shot(page, "case-proposals-discard", 1440);
    expect(c.posts, "nothing was written").toEqual([]);
    expect(c.consoleErrors).toEqual([]);
  });

  test("builder: read-only quote explained, locked version shows the saved price", async ({ page, baseURL }) => {
    const c = await prepare(page, baseURL!);
    await open(page, `/cases/${CUSTOM_CASE}/proposals/3`, '[data-testid="quote-read-only"]', c);
    await expect(page.getByTestId("quote-read-only")).toContainText("Live quote not available in this session");
    await expect(page.getByTestId("quote-read-only")).toContainText("The saved version total below is still correct."); // builder only
    await expect(page.getByText("No access to pricing")).toHaveCount(0);
    expect(c.posts).toContain("POST pricing/quote-preview");

    const locked = await prepare(page, baseURL!);
    await open(page, `/cases/${CUSTOM_CASE}/proposals/2`, '[data-testid="locked-price"]', locked);
    const panel = page.getByTestId("locked-price");
    await expect(panel).toContainText("4,385.00");
    await expect(panel).toContainText("1 USD = 0.920000 EUR");
    await expect(panel).toContainText("Approved");
    await expect(page.getByText("No quote yet")).toHaveCount(0);
    await page.waitForTimeout(800); // past the 500 ms quote debounce
    expect(locked.posts.filter((p) => p.includes("quote-preview")), "a locked version never asks for a live quote").toEqual([]);
    await shot(page, "builder-locked-panel", 1440);
  });

  test("trip checks: tabs with counts, progress, Overdue tab, drawer context", async ({ page, baseURL }) => {
    const c = await prepare(page, baseURL!);
    await open(page, "/trip-checks", '[data-testid="trip-progress"]', c);
    const tabs = page.locator('[role="tablist"]').first();
    await expect(tabs.getByRole("tab", { name: /Open\s*42/ })).toHaveAttribute("aria-selected", "true");
    await expect(tabs.getByRole("tab", { name: /Overdue\s*18/ })).toBeVisible();
    await expect(tabs.getByRole("tab", { name: /Done\s*102/ })).toBeVisible();
    await expect(page.getByTestId("trip-progress").first()).toHaveAttribute("title", /of \d+ checks done/);
    // The chip is narrower than the case status badge above it, so it never sets the Trip case column's width.
    const wider = await page.getByTestId("trip-progress").evaluateAll((chips) =>
      chips
        .map((chip) => {
          const cell = chip.parentElement as HTMLElement;
          const others = Array.from(cell.children).filter((el) => el !== chip);
          return { chip: chip.getBoundingClientRect().width, widest: Math.max(...others.map((el) => el.getBoundingClientRect().width)) };
        })
        .filter((w) => w.chip > w.widest),
    );
    expect(wider, "progress chip wider than the code / status badge").toEqual([]);
    await tabs.getByRole("tab", { name: /Overdue/ }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("due")).toBe("overdue");
    await expect(tabs.getByRole("tab", { name: /Overdue/ })).toHaveAttribute("aria-selected", "true");
    await tabs.getByRole("tab", { name: /Done/ }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("status")).toBe("done");
    expect(new URL(page.url()).searchParams.get("due"), "leaving Overdue clears its due filter").toBeNull();
    await expect(tabs.getByRole("tab", { name: /Done\s*102/ })).toBeVisible();
    // A due window narrows the rows but not the counts (the API counts ignore it): the badges are hidden meanwhile.
    await page.goto(`${baseURL}/trip-checks?status=done&due=7d`);
    await expect(tabs.getByRole("tab", { name: "Done" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("trip-progress").first()).toBeVisible(); // the list (and its counts) arrived
    await expect(tabs.getByRole("tab", { name: /\d/ })).toHaveCount(0);
    await page.goto(`${baseURL}/trip-checks?status=done`);
    await page.locator(ROW).first().click();
    const context = page.getByTestId("check-trip-context");
    await expect(context).toContainText("Sales");
    await expect(context).toContainText(/\d+ of \d+ checks done/);
    await expect(context.getByRole("link", { name: "Open Checks tab" })).toHaveAttribute("href", /\/cases\/TC-000200\?tab=checks/);
    // The drawer's due-date box has the kit control look, like the Select beside it (AA-994 S6d review: form date
    // boxes rendered bare, no border and auto height, once the shared form classes became layout only).
    const dueBox = await page
      .getByRole("dialog")
      .locator('[data-kit-field="date"] input[type="text"]')
      .first()
      .evaluate((el) => {
        const s = getComputedStyle(el);
        return { border: s.borderTopWidth, style: s.borderTopStyle, height: Math.round(el.getBoundingClientRect().height), transparent: s.backgroundColor === "rgba(0, 0, 0, 0)" };
      });
    expect(dueBox, "a form DateField is a 40 px box with a 1 px border and a background").toEqual({ border: "1px", style: "solid", height: 40, transparent: false });
    await page.waitForTimeout(300);
    await shot(page, "trip-check-drawer", 1440);
    expect(c.unmocked).toEqual([]);
    expect(c.consoleErrors).toEqual([]);
  });

  test("convert and decline dialogs keep their buttons in the footer band", async ({ page, baseURL }) => {
    const c = await prepare(page, baseURL!);
    await open(page, `/cases/${INQUIRY_CASE}`, '[data-testid="trip-facts"]', c);
    for (const [open, action] of [
      ["Convert to discovery", "Convert"],
      ["Decline", "Decline inquiry"],
    ] as const) {
      await page.getByRole("button", { name: open, exact: true }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      const footer = dialog.locator(":scope > div").last();
      await expect(footer.getByRole("button", { name: action, exact: true }), `${action} sits in the footer band`).toBeVisible();
      await expect(footer.getByRole("button", { name: /^(Cancel|Keep case)$/ })).toBeVisible();
      await shot(page, `dialog-${action.toLowerCase().replace(/\s+/g, "-")}`, 1440);
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
    }
    expect(c.posts, "nothing was written").toEqual([]);
    expect(c.consoleErrors).toEqual([]);
  });
});
