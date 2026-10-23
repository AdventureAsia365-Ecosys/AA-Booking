// e2e/layout/layout.spec.ts
// AA-910 — the staff admin tables fit a 1440 px desktop: a repeatable check that needs no staff sign-in.
//
// For every staff page that has a table (app/(staff)/**, kit DataTable), in light and dark:
//   1440 x 900 — no horizontal page scroll (document and <main>); every table fits its own scroll box with the last
//                column in view; tab strips have no scrollbar (the old 1 px one); on the FX tab the currency, rate,
//                buffer, as-of and source cells are one line each; Poppins really loaded (widths depend on it);
//                no console error; every API call answered by a fixture. A named screenshot per page and theme.
//   1280 x 800 — the squeeze: the page still never scrolls sideways; a table may scroll inside its own box (that is
//                the design below 1440, AA-910 decision), and where the last column is the row menu it stays pinned
//                (`bk-dt-sticky-end`, position: sticky) and in view.
//   390 x 844  — phones (cards instead of tables): no horizontal page scroll, console clean.
// The API is mocked (see playwright.config.ts for why the fake session is safe). Measurements go to
// $LAYOUT_SHOTS_DIR/measurements.{json,md}.

import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { answer } from "./fixtures";

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
// drawer that holds the table. Pages without a table (billing, trip checks, notifications, settings, countries,
// destinations, calculator, forms) are out of scope.
const PAGES: PageCase[] = [
  { name: "dashboard", path: "/", phoneReady: "text=TC-000001" },
  { name: "cases", path: "/cases" },
  { name: "case-proposals", path: "/cases/TC-000001?tab=proposals" },
  { name: "proposals", path: "/proposals" },
  { name: "customers", path: "/customers" },
  { name: "customer-cases", path: "/customers/CUS-000001?tab=cases" },
  { name: "trip-plans", path: "/catalog/trip-plans" },
  { name: "trip-plans-orphaned", path: "/catalog/trip-plans?show=orphaned" },
  { name: "suppliers", path: "/suppliers" },
  { name: "supplier-contracts", path: "/suppliers/SUP-000001?tab=contracts" },
  { name: "supplier-rates", path: "/suppliers/SUP-000001?tab=rates" },
  { name: "hotels", path: "/hotels" },
  { name: "hotel-drawer", path: "/hotels?hotel=HTL-000001", within: '[role="dialog"]' },
  { name: "services", path: "/services" },
  { name: "service-rates", path: "/services/SVC-000001?tab=rates" },
  { name: "pricing-rules", path: "/pricing/rules" },
  { name: "pricing-fx", path: "/pricing/rules?tab=fx", fx: true },
  { name: "staff", path: "/staff" },
  { name: "staff-cases", path: "/staff/u-001?tab=cases" },
  // Not a kit DataTable: a feed of entries with an expandable before / after grid (opened on the first entry).
  { name: "audit-log", path: "/audit-log", ready: 'button:has-text("Show changes")', click: 'button:has-text("Show changes")', noTable: true },
];

const OUT = process.env.LAYOUT_SHOTS_DIR ? path.resolve(process.env.LAYOUT_SHOTS_DIR) : path.join(__dirname, "results");
const MEASURE_DIR = path.join(OUT, ".measurements");
const SLACK_PX = 20;
const FX_HEADERS = ["Currency", "Units per 1 USD", "Selling buffer", "As of", "Source"];
// 1x1 transparent PNG for CIS photos (the trip plan covers point at the CIS media host).
const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64");

/** An unsigned JWT shaped like a Cognito access token, far from expiry: proxy.ts lets it through (it only decodes). */
function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}

type Collected = { unmocked: string[]; consoleErrors: string[]; blockedExternal: string[] };

async function prepare(page: Page, baseURL: string): Promise<Collected> {
  const c: Collected = { unmocked: [], consoleErrors: [], blockedExternal: [] };
  const origin = new URL(baseURL).origin;
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
    if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") return route.continue();
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

/** For the screenshot only (after measuring): a table that starts below the fold (detail tabs, the hotel drawer's rates)
 *  is scrolled up so the evidence shows it. */
async function bringTableIntoView(page: Page, pc: PageCase) {
  const scope = pc.within ? page.locator(pc.within) : page.locator("main");
  const table = scope.locator("table").last();
  if (!(await table.count())) return;
  await table.evaluate((t) => {
    if (t.getBoundingClientRect().top > window.innerHeight * 0.55) t.scrollIntoView({ block: "start" });
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

for (const scheme of ["light", "dark"] as const) {
  test.describe(`1440 x 900, ${scheme}`, () => {
    test.use({ viewport: { width: 1440, height: 900 }, colorScheme: scheme });
    for (const pc of PAGES) {
      test(`${pc.name} fits 1440`, async ({ page, baseURL }, testInfo) => {
        const c = await prepare(page, baseURL!);
        await open(page, pc, c);
        const m = await measure(page);
        fs.mkdirSync(OUT, { recursive: true });
        await bringTableIntoView(page, pc);
        await page.screenshot({ path: path.join(OUT, `${pc.name}-1440-${scheme}.png`) });
        save(testInfo, pc, 1440, scheme, m, c);

        commonChecks(pc, m, c, scheme);
        for (const t of m.tables) {
          expect(t.boxScroll, `table "${t.head}" fits its frame at 1440 (no sideways scroll inside it)`).toBeLessThanOrEqual(t.boxClient);
          expect(t.lastHeaderVisible, `table "${t.head}": last column "${t.lastHeader}" in view`).not.toBe(false);
          expect(t.boxIsMain, `table "${t.head}" scrolls in its own box, not in <main>`).toBe(false);
          // Lesson S239 (AA-683): a table that fits "by 2 px" scrolls on a browser with a wider scrollbar (Firefox on
          // Windows ~17 px vs Chromium's styled 6 px), so the content must leave room to spare.
          expect(t.boxClient - t.minContent, `table "${t.head}": at least ${SLACK_PX} px to spare at 1440`).toBeGreaterThanOrEqual(SLACK_PX);
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

test.describe("1280 x 800 squeeze, light", () => {
  test.use({ viewport: { width: 1280, height: 800 }, colorScheme: "light" });
  for (const pc of PAGES) {
    test(`${pc.name} at 1280: page never scrolls sideways`, async ({ page, baseURL }, testInfo) => {
      const c = await prepare(page, baseURL!);
      await open(page, pc, c);
      const m = await measure(page);
      save(testInfo, pc, 1280, "light", m, c);

      commonChecks(pc, m, c, "light");
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

// Phones: the kit DataTable switches to cards below 768 px, so only the page-level checks apply (Linear Done-when
// asks for 390 px too). One screenshot per page in light.
test.describe("390 x 844 phone, light", () => {
  test.use({ viewport: { width: 390, height: 844 }, colorScheme: "light", isMobile: true, hasTouch: true });
  for (const pc of PAGES) {
    test(`${pc.name} at 390: page never scrolls sideways`, async ({ page, baseURL }, testInfo) => {
      const c = await prepare(page, baseURL!);
      await open(page, { ...pc, ready: pc.phoneReady ?? pc.ready }, c);
      const m = await measure(page);
      fs.mkdirSync(OUT, { recursive: true });
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
