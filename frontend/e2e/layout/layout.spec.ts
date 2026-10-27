// e2e/layout/layout.spec.ts
// AA-910 — the staff admin tables fit a 1400-1440 px desktop: a repeatable check that needs no staff sign-in.
//
// For every staff page that has a table (app/(staff)/**, kit DataTable), in light and dark:
//   1440 x 900 — no horizontal page scroll (document and <main>); every table fits its own scroll box with the last
//                column in view; tab strips have no scrollbar (the old 1 px one); on the FX tab the currency, rate,
//                buffer, as-of and source cells are one line each; Poppins really loaded (widths depend on it);
//                no console error; every API call answered by a fixture. A named screenshot per page and theme.
//   1400 x 900 — the same checks, light only (S250: the live Dev check was made at 1400).
//   1280 x 800 — the squeeze: the page still never scrolls sideways; a table may scroll inside its own box (that is
//                the design below 1440, AA-910 decision), and where the last column is the row menu it stays pinned
//                (`bk-dt-sticky-end`, position: sticky) and in view.
//   390 x 844  — phones (cards instead of tables): no horizontal page scroll, console clean.
//   960 / 1100 — AA-996 compact density (768-1199 px: a laptop at 125-150 % browser zoom; ui/booking/DataTable.tsx and
//                app/globals.css): the widest lists (cases, billing invoices / payments, suppliers, trip plans,
//                destinations) fit their frame with no clipped column, in light and dark at 960; the sidebar is the
//                68 px rail; the folded FX / audit columns open in the row's details drawer. Every page at 960: the page
//                never scrolls sideways. At 1280 the sidebar is still the stored (expanded) width: nothing changed there.
//                Review fixes: a CSV exported at 960 equals the one at 1400 (folded columns included); the details drawer
//                closes for good when the window leaves 768-1199 px; the `[` key is ignored at 960.
// The browser clock is frozen at fixtures.ts CLOCK and every fixture timestamp is older than 7 days, so relative dates
// render as the (widest) absolute date on any day the check runs.
// The API is mocked (see playwright.config.ts for why the fake session is safe). Measurements go to
// $LAYOUT_SHOTS_DIR/measurements.{json,md}.

import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { answer, CLOCK } from "./fixtures";
import { MEASURE_DIR, OUT } from "./paths";

type PageCase = {
  name: string;
  path: string;
  /** Wait for rows inside this container (e.g. the hotel drawer), not just the first table on the page. */
  within?: string;
  /** Check the FX columns for single-line cells. */
  fx?: boolean;
  /** The element that shows the page has its data (default: a kit DataTable row). */
  ready?: string;
  /** Click this once the page is ready (e.g. open an expandable row) before measuring. */
  click?: string;
  /** On a phone the page shows its own cards instead of a kit DataTable: wait for this instead. */
  phoneReady?: string;
  /** The page lists its rows without a kit DataTable (only the page-level checks apply). */
  noTable?: boolean;
};

// Every route under app/(staff) whose view renders a kit DataTable (grep DataTable app/(staff)), with the tab or
// drawer that holds the table. Pages without a table (billing, trip checks, notifications, settings, calculator, forms)
// are out of scope; the Countries card grid (AA-996) is measured with `noTable`.
const PAGES: PageCase[] = [
  { name: "dashboard", path: "/", phoneReady: "text=TC-000001" },
  { name: "cases", path: "/cases" },
  { name: "case-proposals", path: "/cases/TC-000001?tab=proposals" },
  { name: "proposals", path: "/proposals" },
  { name: "customers", path: "/customers" },
  { name: "customer-cases", path: "/customers/CUS-000001?tab=cases" },
  { name: "trip-plans", path: "/catalog/trip-plans" },
  { name: "trip-plans-orphaned", path: "/catalog/trip-plans?show=orphaned" },
  { name: "destinations", path: "/catalog/destinations" },
  { name: "destination-drawer", path: "/catalog/destinations?place=dddd-0000-4000-8000-000000000005", ready: '[role="dialog"]', noTable: true },
  { name: "countries", path: "/catalog/countries", ready: "text=Area: Indochina", noTable: true },
  { name: "country-destinations", path: "/catalog/countries/LA?tab=destinations" },
  // AA-996 slice 3: cover hero + "Photos · From CIS" gallery (24 tiles) on the country overview.
  { name: "country-overview", path: "/catalog/countries/LA", ready: '[data-testid="country-photos"]', noTable: true },
  { name: "settings-security", path: "/settings?tab=security", ready: "text=Company e-mail, one-time code", noTable: true },
  { name: "suppliers", path: "/suppliers" },
  { name: "supplier-contracts", path: "/suppliers/SUP-000001?tab=contracts" },
  { name: "supplier-rates", path: "/suppliers/SUP-000001?tab=rates" },
  { name: "hotels", path: "/hotels" },
  { name: "hotel-drawer", path: "/hotels?hotel=HTL-000001", within: '[role="dialog"]' },
  { name: "services", path: "/services" },
  { name: "service-rates", path: "/services/SVC-000001?tab=rates" },
  { name: "pricing-rules", path: "/pricing/rules" },
  { name: "pricing-fx", path: "/pricing/rules?tab=fx", fx: true },
  { name: "b2b-desk-on-request", path: "/b2b-desk" },
  { name: "b2b-desk-departures", path: "/b2b-desk?tab=departures" },
  { name: "staff", path: "/staff" },
  { name: "staff-cases", path: "/staff/u-001?tab=cases" },
  // Not a kit DataTable: a feed of entries with an expandable before / after grid (opened on the first entry).
  { name: "audit-log", path: "/audit-log", ready: 'button:has-text("Show changes")', click: 'button:has-text("Show changes")', noTable: true },
];

const SLACK_PX = 20;
const FX_HEADERS = ["Currency", "Units per 1 USD", "Buffer", "As of", "Source"];
// 1x1 transparent PNG for CIS photos (the trip plan covers point at the CIS media host).
const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64");

/** An unsigned JWT shaped like a Cognito access token, far from expiry: proxy.ts lets it through (it only decodes). */
function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}

type Collected = { unmocked: string[]; consoleErrors: string[]; blockedExternal: string[] };

// Google Fonts answers, fetched once per worker and replayed: 88 tests then make a handful of font requests, not
// hundreds, so a network hiccup is far less likely to fail a test.
const fontCache = new Map<string, { status: number; headers: Record<string, string>; body: Buffer }>();

async function prepare(page: Page, baseURL: string): Promise<Collected> {
  const c: Collected = { unmocked: [], consoleErrors: [], blockedExternal: [] };
  const origin = new URL(baseURL).origin;
  // The pixels must not depend on the day the check runs: relative dates ("3 d ago" vs "28 Sept 2026") change widths.
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
      const a = answer(req.method(), url.pathname.slice("/api/booking/".length), url.searchParams);
      if (!a) {
        c.unmocked.push(`${req.method()} ${url.pathname}${url.search}`);
        return route.fulfill({ status: 501, json: { detail: "layout_mock_unmocked", message: "No fixture for this call." } });
      }
      return route.fulfill({ status: a.status, json: a.body });
    }
    // The real brand fonts: table widths depend on them, so the check loads them like a browser does.
    if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
      let hit = fontCache.get(req.url());
      if (!hit) {
        const res = await route.fetch();
        hit = { status: res.status(), headers: res.headers(), body: await res.body() };
        if (res.ok()) fontCache.set(req.url(), hit);
      }
      return route.fulfill(hit);
    }
    c.blockedExternal.push(`${req.resourceType()} ${url.hostname}${url.pathname}`);
    if (req.resourceType() === "image") return route.fulfill({ status: 200, contentType: "image/png", body: PIXEL });
    return route.fulfill({ status: 204, body: "" });
  });
  return c;
}

type TableM = {
  head: string;
  inDialog: boolean;
  boxIsMain: boolean;
  boxClient: number;
  boxScroll: number;
  tableWidth: number;
  cols: number;
  lastHeader: string;
  lastHeaderVisible: boolean | null;
  stickyEnd: boolean;
  lastHeaderPosition: string | null;
  /** Header text and rendered width of every column (to find the column that pushes a table wider). */
  colWidths: string[];
  /** The narrowest the table can get (min-content, ignoring the kit's tableMinWidth): frame - this = the slack. */
  minContent: number;
  /** Each column's width in that min-content layout (which column to trim when the slack is short). */
  colMin: string[];
};

type Measure = {
  innerWidth: number;
  docScrollWidth: number;
  docClientWidth: number;
  mainScrollWidth: number | null;
  mainClientWidth: number | null;
  mainScrollbar: number | null;
  theme: string | null;
  poppinsLoaded: boolean;
  tables: TableM[];
  tabstrips: { overflowY: number; hScrollbar: number }[];
  fxCells: { header: string; text: string; lines: number }[];
  /** Rendered width of the shell sidebar (68 = icon rail, 248 = expanded). */
  sidebarWidth: number | null;
};

async function measure(page: Page): Promise<Measure> {
  return page.evaluate((fxHeaders) => {
    const se = document.scrollingElement as HTMLElement;
    const main = document.querySelector("main");
    const scrollBox = (t: Element): HTMLElement | null => {
      let el = t.parentElement;
      while (el && el !== document.body) {
        const s = getComputedStyle(el);
        if (s.overflowX === "auto" || s.overflowX === "scroll") return el;
        el = el.parentElement;
      }
      return null;
    };
    // Distinct line boxes of the text inside `el` (text nodes whose rects share a vertical band are one line).
    const lines = (el: Element): number => {
      const mids: number[] = [];
      const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let n = walk.nextNode(); n; n = walk.nextNode()) {
        if (!n.textContent?.trim()) continue;
        const r = document.createRange();
        r.selectNodeContents(n);
        for (const rect of Array.from(r.getClientRects())) {
          if (rect.width < 1) continue;
          const mid = rect.top + rect.height / 2;
          if (!mids.some((m) => Math.abs(m - mid) < 6)) mids.push(mid);
        }
      }
      return mids.length;
    };
    const tables = Array.from(document.querySelectorAll("table"))
      .filter((t) => (t as HTMLElement).offsetParent !== null)
      .map((t) => {
        const box = scrollBox(t);
        const ths = Array.from(t.querySelectorAll("thead th"));
        const last = ths[ths.length - 1] as HTMLElement | undefined;
        const br = (box ?? t).getBoundingClientRect();
        const lr = last?.getBoundingClientRect();
        // min-content: the table squeezed to width 0 (a table never gets narrower than its content), then restored.
        const te = t as HTMLElement;
        const saved = [te.style.width, te.style.minWidth];
        te.style.width = "0px";
        te.style.minWidth = "0px";
        const minContent = te.offsetWidth;
        const colMin = ths.map((th, i) => `${(th.textContent ?? "").trim().slice(0, 16) || `#${i + 1}`}:${(th as HTMLElement).offsetWidth}`);
        [te.style.width, te.style.minWidth] = saved;
        return {
          head: ths
            .map((th) => th.textContent?.trim())
            .filter(Boolean)
            .slice(0, 3)
            .join(" | "),
          inDialog: !!t.closest('[role="dialog"]'),
          boxIsMain: box === main,
          boxClient: box?.clientWidth ?? 0,
          boxScroll: box?.scrollWidth ?? 0,
          tableWidth: (t as HTMLElement).offsetWidth,
          cols: ths.length,
          lastHeader: (last?.textContent ?? "").trim().slice(0, 24),
          lastHeaderVisible: lr ? lr.right <= br.right + 1 && lr.left >= br.left - 1 : null,
          stickyEnd: !!t.closest(".bk-dt-sticky-end"),
          lastHeaderPosition: last ? getComputedStyle(last).position : null,
          minContent,
          colMin,
          colWidths: ths.map((th, i) => `${(th.textContent ?? "").trim().slice(0, 16) || `#${i + 1}`}:${(th as HTMLElement).offsetWidth}`),
        };
      });
    const tabstrips = Array.from(document.querySelectorAll('[role="tablist"]')).map((el) => {
      const e = el as HTMLElement;
      const s = getComputedStyle(e);
      const borders = parseFloat(s.borderTopWidth) + parseFloat(s.borderBottomWidth);
      return { overflowY: e.scrollHeight - e.clientHeight, hScrollbar: Math.round(e.offsetHeight - e.clientHeight - borders) };
    });
    const fxCells: { header: string; text: string; lines: number }[] = [];
    for (const t of Array.from(document.querySelectorAll("table"))) {
      const heads = Array.from(t.querySelectorAll("thead th")).map((th) => th.textContent?.trim() ?? "");
      if (!heads.includes("Units per 1 USD")) continue;
      const idx = heads.map((h, i) => (fxHeaders.includes(h) ? i : -1)).filter((i) => i >= 0);
      for (const tr of Array.from(t.querySelectorAll("tbody tr"))) {
        const tds = Array.from(tr.children);
        for (const i of idx) {
          const td = tds[i];
          if (td) fxCells.push({ header: heads[i], text: (td.textContent ?? "").trim().slice(0, 40), lines: lines(td) });
        }
      }
    }
    return {
      innerWidth: window.innerWidth,
      docScrollWidth: se.scrollWidth,
      docClientWidth: se.clientWidth,
      mainScrollWidth: main ? main.scrollWidth : null,
      mainClientWidth: main ? main.clientWidth : null,
      mainScrollbar: main ? main.offsetWidth - main.clientWidth : null,
      theme: document.documentElement.getAttribute("data-theme"),
      poppinsLoaded: Array.from(document.fonts).some((f) => f.family.replace(/["']/g, "") === "Poppins" && f.status === "loaded"),
      tables,
      tabstrips,
      fxCells,
      sidebarWidth: (document.querySelector('[data-testid="admin-sidebar"]') as HTMLElement | null)?.offsetWidth ?? null,
    };
  }, FX_HEADERS);
}

async function open(page: Page, pc: PageCase, c: Collected) {
  await page.goto(pc.path, { waitUntil: "domcontentloaded" });
  const scope = pc.within ? page.locator(pc.within) : page.locator("main");
  const ready = scope.locator(pc.ready ?? '[data-testid="kit-datatable-row"]').first();
  try {
    await expect(ready).toBeVisible({ timeout: 15_000 });
  } catch (e) {
    const shown = (await page.locator("main").innerText({ timeout: 2_000 }).catch(() => "")).slice(0, 400);
    throw new Error(`${pc.name}: no rows. unmocked=${JSON.stringify(c.unmocked)} console=${JSON.stringify(c.consoleErrors)} main="${shown}"\n${e}`);
  }
  if (pc.click) await scope.locator(pc.click).first().click();
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  // Queries settle in one tick (fulfilled locally); give late renders (pager, sticky column) a short, bounded moment.
  await page.waitForTimeout(400);
}

/** For the screenshot only (after measuring): a list that starts below the fold (detail tabs, the hotel drawer's rates,
 *  the phone cards under the KPI tiles) is scrolled up so the evidence shows it. */
async function bringListIntoView(page: Page, pc: PageCase, selector: string, which: "first" | "last") {
  const scope = pc.within ? page.locator(pc.within) : page.locator("main");
  const all = scope.locator(selector);
  const list = which === "first" ? all.first() : all.last();
  if (!(await list.count())) return;
  await list.evaluate((t) => {
    if (t.getBoundingClientRect().top <= window.innerHeight * 0.55) return;
    (t as HTMLElement).style.scrollMarginTop = "80px"; // clear the sticky top bar, so the list's top edge shows
    t.scrollIntoView({ block: "start" });
  });
  await page.waitForTimeout(150);
}

function save(testInfo: TestInfo, pc: PageCase, width: number, scheme: string, m: Measure, c: Collected) {
  fs.mkdirSync(MEASURE_DIR, { recursive: true });
  const row = { name: pc.name, path: pc.path, width, scheme, ...m, ...c };
  fs.writeFileSync(path.join(MEASURE_DIR, `${pc.name}-${width}-${scheme}.json`), JSON.stringify(row, null, 1));
  void testInfo.attach("measure", { body: JSON.stringify(row, null, 1), contentType: "application/json" });
}

function commonChecks(pc: PageCase, m: Measure, c: Collected, scheme: string) {
  expect(c.unmocked, "every Booking API call has a fixture").toEqual([]);
  expect(c.consoleErrors, "console is clean").toEqual([]);
  expect(m.theme, "the page renders in the scheme under test").toBe(scheme);
  expect(m.poppinsLoaded, "Poppins loaded (widths are measured with the real brand font)").toBe(true);
  if (!pc.noTable) expect(m.tables.length, "the page shows at least one table").toBeGreaterThan(0);
  expect(m.docScrollWidth, "no horizontal page scroll (document)").toBeLessThanOrEqual(m.docClientWidth);
  expect(m.docScrollWidth, "no horizontal page scroll (viewport)").toBeLessThanOrEqual(m.innerWidth);
  if (m.mainScrollWidth !== null && m.mainClientWidth !== null) {
    expect(m.mainScrollWidth, "no horizontal scroll in <main>").toBeLessThanOrEqual(m.mainClientWidth);
  }
  for (const s of m.tabstrips) {
    expect(s.overflowY, "tab strip has no vertical overflow (the old 1 px scrollbar)").toBeLessThanOrEqual(0);
    expect(s.hScrollbar, "tab strip shows no scrollbar").toBeLessThanOrEqual(0);
  }
}

// The "fits" passes: 1440 in both themes, and 1400 in light (S250: Nghiệp's live check on Dev at 1400 px; geometry does
// not depend on the theme). Each table fits its frame with SLACK_PX to spare at both widths.
const FIT_PASSES = [
  { width: 1440, scheme: "light" },
  { width: 1440, scheme: "dark" },
  { width: 1400, scheme: "light" },
] as const;

for (const { width, scheme } of FIT_PASSES) {
  test.describe(`${width} x 900, ${scheme}`, () => {
    test.use({ viewport: { width, height: 900 }, colorScheme: scheme });
    for (const pc of PAGES) {
      test(`${pc.name} fits ${width}`, async ({ page, baseURL }, testInfo) => {
        const c = await prepare(page, baseURL!);
        await open(page, pc, c);
        const m = await measure(page);
        fs.mkdirSync(OUT, { recursive: true });
        await bringListIntoView(page, pc, "table", "last");
        await page.screenshot({ path: path.join(OUT, `${pc.name}-${width}-${scheme}.png`) });
        save(testInfo, pc, width, scheme, m, c);

        commonChecks(pc, m, c, scheme);
        for (const t of m.tables) {
          expect(t.boxScroll, `table "${t.head}" fits its frame at ${width} (no sideways scroll inside it)`).toBeLessThanOrEqual(t.boxClient);
          expect(t.lastHeaderVisible, `table "${t.head}": last column "${t.lastHeader}" in view`).not.toBe(false);
          expect(t.boxIsMain, `table "${t.head}" scrolls in its own box, not in <main>`).toBe(false);
          // Lesson S239 (AA-683): a table that fits "by 2 px" scrolls on a browser with a wider scrollbar (Firefox on
          // Windows ~17 px vs Chromium's styled 6 px), so the content must leave room to spare.
          expect(t.boxClient - t.minContent, `table "${t.head}": at least ${SLACK_PX} px to spare at ${width}`).toBeGreaterThanOrEqual(SLACK_PX);
        }
        if (pc.fx) {
          expect(m.fxCells.length, "FX cells found").toBeGreaterThan(0);
          const wrapped = m.fxCells.filter((f) => f.lines !== 1);
          expect(wrapped, "FX currency / rate / buffer / as-of / source cells are one line").toEqual([]);
        }
      });
    }
  });
}

// S250: /cases scored CLS 0.12 on Dev (Chrome Local metrics, 1400 px): the filter options (staff names) and the tab
// counts arrive after the list, and a native select grows to its longest option, so the filter bar widened, wrapped and
// pushed the table down. Here those two answers come 800 ms late and the page must not shift (layout-shift entries
// without recent input, summed like CLS). 1400 px, and 960 px (1400 px at 150 % browser zoom, Nghiệp's real setup).
for (const width of [1400, 960]) {
  test.describe(`late filter data at ${width}`, () => {
    test.use({ viewport: { width, height: 900 }, colorScheme: "light" });
    test(`cases at ${width}: no layout shift when the filter options and tab counts arrive late`, async ({ page, baseURL }, testInfo) => {
      await page.addInitScript(() => {
        const w = window as unknown as { __cls: number };
        w.__cls = 0;
        new PerformanceObserver((list) => {
          for (const e of list.getEntries() as unknown as { value: number; hadRecentInput: boolean }[]) {
            if (!e.hadRecentInput) w.__cls += e.value;
          }
        }).observe({ type: "layout-shift", buffered: true });
      });
      const c = await prepare(page, baseURL!);
      // Registered after prepare(), so it runs first; fallback() hands the request to the fixture route.
      await page.route(/\/api\/booking\/(staff\/directory|trip-cases\/summary)(\?|$)/, async (route) => {
        await new Promise((r) => setTimeout(r, 800));
        await route.fallback();
      });
      await open(page, { name: "cases-late", path: "/cases" }, c);
      await expect(page.locator('select:has(option:text("Unassigned")) option')).not.toHaveCount(2, { timeout: 5_000 });
      await page.waitForTimeout(1_200);
      const cls = await page.evaluate(() => (window as unknown as { __cls: number }).__cls);
      void testInfo.attach("cls", { body: String(cls), contentType: "text/plain" });
      expect(c.unmocked, "every Booking API call has a fixture").toEqual([]);
      expect(cls, "layout shift on /cases once the late data arrives").toBeLessThan(0.02);
    });
  });
}

test.describe("1280 x 800 squeeze, light", () => {
  test.use({ viewport: { width: 1280, height: 800 }, colorScheme: "light" });
  for (const pc of PAGES) {
    test(`${pc.name} at 1280: page never scrolls sideways`, async ({ page, baseURL }, testInfo) => {
      const c = await prepare(page, baseURL!);
      await open(page, pc, c);
      const m = await measure(page);
      save(testInfo, pc, 1280, "light", m, c);

      commonChecks(pc, m, c, "light");
      expect(m.sidebarWidth, "1280 is above the compact window: the sidebar keeps its stored (expanded) width").toBe(248);
      for (const t of m.tables) {
        expect(t.boxIsMain, `table "${t.head}" scrolls in its own box, not in <main>`).toBe(false);
        if (t.stickyEnd) {
          expect(t.lastHeaderPosition, `table "${t.head}": row-menu column pinned`).toBe("sticky");
          expect(t.lastHeaderVisible, `table "${t.head}": pinned row-menu column in view`).toBe(true);
        }
      }
    });
  }
});

// AA-996 — compact density (768-1199 px). Nghiệp's laptop at 150 % zoom is about 960 px CSS wide, where the wide lists
// used to cut their last columns (the frame scrolled sideways). The widest lists must now fit their frame with nothing
// clipped and SLACK_PX to spare (the FX / audit columns are folded into the row's details drawer), at 960 in light and
// dark and at 1100 in light; the sidebar is the 68 px rail.
const COMPACT_PAGES: PageCase[] = [
  { name: "cases", path: "/cases" },
  { name: "billing-invoices", path: "/billing" },
  { name: "billing-payments", path: "/billing?tab=payments" },
  { name: "suppliers", path: "/suppliers" },
  { name: "trip-plans", path: "/catalog/trip-plans" },
  { name: "destinations", path: "/catalog/destinations" },
];
const COMPACT_PASSES = [
  { width: 960, scheme: "light" },
  { width: 960, scheme: "dark" },
  { width: 1100, scheme: "light" },
] as const;

for (const { width, scheme } of COMPACT_PASSES) {
  test.describe(`compact ${width} x 900, ${scheme}`, () => {
    test.use({ viewport: { width, height: 900 }, colorScheme: scheme });
    for (const pc of COMPACT_PAGES) {
      test(`${pc.name} fits ${width} (compact density)`, async ({ page, baseURL }, testInfo) => {
        const c = await prepare(page, baseURL!);
        await open(page, pc, c);
        const m = await measure(page);
        fs.mkdirSync(OUT, { recursive: true });
        await bringListIntoView(page, pc, "table", "last");
        await page.screenshot({ path: path.join(OUT, `${pc.name}-${width}-${scheme}.png`) });
        save(testInfo, { ...pc, name: `compact-${pc.name}` }, width, scheme, m, c);

        commonChecks(pc, m, c, scheme);
        expect(m.sidebarWidth, "compact density: the sidebar is the 68 px icon rail").toBe(68);
        for (const t of m.tables) {
          expect(t.boxScroll, `table "${t.head}" fits its frame at ${width}: no column cut off`).toBeLessThanOrEqual(t.boxClient);
          expect(t.lastHeaderVisible, `table "${t.head}": last column "${t.lastHeader}" in view`).toBe(true);
          expect(t.boxIsMain, `table "${t.head}" scrolls in its own box, not in <main>`).toBe(false);
          expect(t.boxClient - t.minContent, `table "${t.head}": at least ${SLACK_PX} px to spare at ${width}`).toBeGreaterThanOrEqual(SLACK_PX);
        }
      });
    }
  });
}

test.describe("compact 960: the folded columns open in the row's details drawer", () => {
  test.use({ viewport: { width: 960, height: 900 }, colorScheme: "light" });
  test("billing invoices: Issued, Total and Paid are in the details drawer", async ({ page, baseURL }) => {
    const c = await prepare(page, baseURL!);
    await open(page, { name: "billing-invoices", path: "/billing" }, c);
    const heads = await page.locator("main table thead th").allInnerTexts();
    for (const folded of ["ISSUED", "TOTAL", "PAID"]) expect(heads.map((h) => h.trim().toUpperCase()), `${folded} left the table`).not.toContain(folded);
    await page.locator('[data-testid="bk-dt-details"]').first().click();
    const drawer = page.locator('[data-testid="bk-dt-details-drawer"]');
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("INV-000001");
    for (const label of ["Issued", "Total", "Paid"]) await expect(drawer.locator("dt", { hasText: new RegExp(`^${label}$`, "i") })).toHaveCount(1);
    await page.waitForTimeout(300); // the kit drawer slides in (0.18 s): shoot it settled
    await page.screenshot({ path: path.join(OUT, "billing-invoices-details-960-light.png") });
    // Opening the details does not follow the row link (the button stops the row click).
    expect(new URL(page.url()).pathname).toBe("/billing");
    expect(c.unmocked, "every Booking API call has a fixture").toEqual([]);
    expect(c.consoleErrors, "console is clean").toEqual([]);
  });
});

// AA-996 review fixes: the CSV does not depend on the density, the details drawer does not outlive the compact window,
// and the `[` key does not flip the hidden sidebar preference there.
async function csvText(page: Page): Promise<string> {
  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "CSV", exact: true }).click()]);
  return fs.readFileSync((await download.path())!, "utf8");
}
const sidebarWidth = (page: Page) =>
  page.evaluate(() => (document.querySelector('[data-testid="admin-sidebar"]') as HTMLElement | null)?.offsetWidth ?? null);

test.describe("compact 960: the CSV export keeps every column", () => {
  test.use({ viewport: { width: 960, height: 900 }, colorScheme: "light" });
  for (const pc of [
    { name: "cases", path: "/cases", folded: ["Sales"] },
    { name: "trip-plans", path: "/catalog/trip-plans", folded: ["Trip style", "Price hint"] },
    { name: "destinations", path: "/catalog/destinations", folded: ["Coordinates", "Media"] },
  ]) {
    test(`${pc.name}: the file at 960 equals the file at 1400`, async ({ page, baseURL }) => {
      const c = await prepare(page, baseURL!);
      await open(page, pc, c);
      const compactCsv = await csvText(page);
      const header = compactCsv.replace(/^\uFEFF/, "").split("\n")[0].split(",");
      for (const h of pc.folded) expect(header, `${h} is in the CSV at 960`).toContain(h);
      expect(header, "the details button column is not exported").not.toContain("__details");
      await page.setViewportSize({ width: 1400, height: 900 });
      await page.waitForTimeout(300);
      expect(await csvText(page), "same file at 960 and 1400").toBe(compactCsv);
      expect(c.consoleErrors, "console is clean").toEqual([]);
    });
  }
});

test.describe("compact 960: the details drawer and the sidebar key", () => {
  test.use({ viewport: { width: 960, height: 900 }, colorScheme: "light" });
  test("the drawer closes when the window leaves the compact range and does not come back", async ({ page, baseURL }) => {
    const c = await prepare(page, baseURL!);
    await open(page, { name: "billing-invoices", path: "/billing" }, c);
    await page.locator('[data-testid="bk-dt-details"]').first().click();
    const drawer = page.locator('[data-testid="bk-dt-details-drawer"]');
    await expect(drawer).toBeVisible();
    await page.setViewportSize({ width: 1280, height: 900 });
    await expect(drawer).toHaveCount(0);
    await page.setViewportSize({ width: 960, height: 900 });
    await page.waitForTimeout(300);
    await expect(drawer, "back in the compact window the drawer stays closed").toHaveCount(0);
    expect(c.consoleErrors, "console is clean").toEqual([]);
  });
  test("the [ key does nothing at 960 (the stored sidebar width applies again at 1280)", async ({ page, baseURL }) => {
    const c = await prepare(page, baseURL!);
    await open(page, { name: "cases", path: "/cases" }, c);
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("[");
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForTimeout(300);
    expect(await sidebarWidth(page), "the preference was not flipped at 960: expanded at 1280").toBe(248);
    // Control: the key still works from 1200 px.
    await page.keyboard.press("[");
    await expect.poll(() => sidebarWidth(page)).toBe(68);
    expect(c.consoleErrors, "console is clean").toEqual([]);
  });
});

// Every staff page at 960 (compact density): the page itself never scrolls sideways and tables keep to their own box.
test.describe("960 x 900 compact, every page, light", () => {
  test.use({ viewport: { width: 960, height: 900 }, colorScheme: "light" });
  for (const pc of PAGES) {
    test(`${pc.name} at 960: page never scrolls sideways`, async ({ page, baseURL }, testInfo) => {
      const c = await prepare(page, baseURL!);
      await open(page, pc, c);
      const m = await measure(page);
      save(testInfo, pc, 960, "light", m, c);
      commonChecks(pc, m, c, "light");
      expect(m.sidebarWidth, "compact density: the sidebar is the 68 px icon rail").toBe(68);
      for (const t of m.tables) {
        expect(t.boxIsMain, `table "${t.head}" scrolls in its own box, not in <main>`).toBe(false);
        if (t.stickyEnd) expect(t.lastHeaderVisible, `table "${t.head}": pinned row-menu column in view`).toBe(true);
      }
    });
  }
});

// Phones: the kit DataTable switches to cards below 768 px, so only the page-level checks apply (Linear Done-when
// asks for 390 px too). One screenshot per page in light.
test.describe("390 x 844 phone, light", () => {
  test.use({ viewport: { width: 390, height: 844 }, colorScheme: "light", isMobile: true, hasTouch: true });
  for (const pc of PAGES) {
    test(`${pc.name} at 390: page never scrolls sideways`, async ({ page, baseURL }, testInfo) => {
      const c = await prepare(page, baseURL!);
      const phonePc = { ...pc, ready: pc.phoneReady ?? pc.ready };
      await open(page, phonePc, c);
      const m = await measure(page);
      fs.mkdirSync(OUT, { recursive: true });
      // The first card (or row) of the list, not the page header, so the phone evidence shows the list itself.
      await bringListIntoView(page, phonePc, phonePc.ready ?? '[data-testid="kit-datatable-row"]', "first");
      await page.screenshot({ path: path.join(OUT, `${pc.name}-390-light.png`) });
      save(testInfo, pc, 390, "light", m, c);
      commonChecks({ ...pc, noTable: true }, m, c, "light");
    });
  }
});

test.afterAll(() => {
  if (!fs.existsSync(MEASURE_DIR)) return;
  const rows = fs
    .readdirSync(MEASURE_DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => JSON.parse(fs.readFileSync(path.join(MEASURE_DIR, f), "utf8")) as { name: string; path: string; width: number; scheme: string } & Measure & Collected);
  const order = new Map(PAGES.map((p, i) => [p.name, i]));
  rows.sort((a, b) => b.width - a.width || a.scheme.localeCompare(b.scheme) || (order.get(a.name) ?? 99) - (order.get(b.name) ?? 99));
  fs.writeFileSync(path.join(OUT, "measurements.json"), JSON.stringify(rows, null, 1));
  const md = [
    "| Page | Path | Width | Theme | Page scroll (doc / main) | Tables: min-content / frame px (cols) | Last column in view | Tab strip overflow | FX cells > 1 line | Console errors |",
    "|---|---|---|---|---|---|---|---|---|---|",
    ...rows.map((r) => {
      const tables = !r.tables.length ? "no kit table (entry feed)" : r.tables.map((t) => `${t.minContent} / ${t.boxClient}${t.boxScroll > t.boxClient ? ` scrolls ${t.boxScroll}` : ""} (${t.cols}${t.stickyEnd ? ", pinned ⋯" : ""}${t.inDialog ? ", drawer" : ""})`).join("; ");
      const lastOk = !r.tables.length
        ? "–"
        : r.tables.every((t) => t.lastHeaderVisible !== false)
          ? "yes"
          : r.tables.every((t) => t.lastHeaderVisible !== false || (t.boxScroll > t.boxClient && !t.stickyEnd))
            ? "scrolls in its box (last column is data)"
            : "NO";
      const tab = r.tabstrips.length ? r.tabstrips.map((s) => Math.max(s.overflowY, s.hScrollbar)).join(", ") : "–";
      const fx = r.fxCells.length ? `${r.fxCells.filter((f) => f.lines !== 1).length} of ${r.fxCells.length}` : "–";
      return `| ${r.name} | \`${r.path}\` | ${r.width} | ${r.scheme} | ${r.docScrollWidth - r.docClientWidth} / ${(r.mainScrollWidth ?? 0) - (r.mainClientWidth ?? 0)} | ${tables} | ${lastOk} | ${tab} | ${fx} | ${r.consoleErrors.length} |`;
    }),
  ].join("\n");
  fs.writeFileSync(path.join(OUT, "measurements.md"), md + "\n");
});
