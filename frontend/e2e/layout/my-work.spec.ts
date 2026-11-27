// e2e/layout/my-work.spec.ts
// AA-1159 — the My work band of the dashboard, against the same mocked API as layout.spec.ts (fixtures.ts MY_WORK; no
// real sign-in). Checks: the band shows the two groups with the fixture's tiles; each tile links to its list with the
// API's filter as the query string; an unavailable tile reads "Not available yet"; "CIS: My work" points at the CIS
// half; Refresh reloads the summary; Vietnamese labels when VI is picked; the page never scrolls sideways at 390; the
// stop-sale tile's list (/stop-sales?review=me) leaves out what the viewer entered; AA-1177: the "Contracts ending
// soon" tile's list on Suppliers (every contract kind, the e-mail state) and a supplier's own list fit 390 px. AA-1190:
// the tiles with nothing waiting fold into one line that opens on click; the new approval tiles and the overdue trip
// checks split into yours / no owner link to their filtered lists; the price setup progress card links each step.
// AA-1190 (S296 live check): the card's count and its explanation are two separate lines; and every tile, CLICKED
// (a client-side <Link> navigation), makes the same filtered reads as a direct load of its link (the CIS Review Queue
// and Data erasure pages opened unfiltered on a click because they read window.location once; this keeps Booking's
// targets from doing the same).
// Screenshots (1440 light / dark, 390 light / dark) land in $LAYOUT_SHOTS_DIR/my-work-*.png.

import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { answer, CLOCK, MY_WORK } from "./fixtures";
import { OUT } from "./paths";

function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}

async function prepare(page: Page, baseURL: string): Promise<{ errors: string[]; summaryCalls: () => number }> {
  const errors: string[] = [];
  let calls = 0;
  const origin = new URL(baseURL).origin;
  await page.clock.setFixedTime(new Date(CLOCK));
  await page.context().addCookies([{ name: "bk_access", value: fakeAccessToken(), url: origin }]);
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text().slice(0, 300));
  });
  page.on("pageerror", (e) => errors.push(`pageerror: ${String(e).slice(0, 300)}`));
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin === origin) {
      if (!url.pathname.startsWith("/api/booking/")) return route.continue();
      const p = url.pathname.slice("/api/booking/".length);
      if (p === "my-work/summary") calls += 1;
      const a = answer(req.method(), p, url.searchParams);
      if (!a) return route.fulfill({ status: 501, json: { detail: "layout_mock_unmocked" } });
      return route.fulfill({ status: a.status, json: a.body });
    }
    return route.fulfill({ status: 204, body: "" });
  });
  return { errors, summaryCalls: () => calls };
}

const band = (page: Page) => page.getByTestId("my-work-band");
const tile = (page: Page, key: string) => page.getByTestId(`my-work-tile-${key}`);

for (const scheme of ["light", "dark"] as const) {
  test.describe(`My work band (${scheme})`, () => {
    test.use({ colorScheme: scheme });

    test("1440: two groups, tiles link to their filtered lists", async ({ page, baseURL }) => {
      await page.setViewportSize({ width: 1440, height: 900 });
      const { errors, summaryCalls } = await prepare(page, baseURL!);
      await page.goto("/", { waitUntil: "domcontentloaded" });
      await expect(tile(page, "supplier_bills_to_approve")).toContainText("5");
      await expect(page.getByTestId("my-work-group-work")).toContainText("To do");
      await expect(page.getByTestId("my-work-group-approve")).toContainText("To approve or pay");
      await expect(tile(page, "supplier_bills_due_soon")).toHaveAttribute("href", "/billing?bill_status=open&bill_due=soon");
      await expect(tile(page, "my_open_cases")).toHaveAttribute("href", "/cases?tab=mine&status=discovery%2Cbuilding");
      await expect(tile(page, "trip_checks_overdue")).toHaveAttribute("href", "/trip-checks?due=overdue&owner=me");
      await expect(tile(page, "trip_checks_unassigned")).toHaveAttribute("href", "/trip-checks?due=overdue&owner=none");
      await expect(tile(page, "price_versions_to_approve")).toHaveAttribute("href", "/pricing/workbench?pending_versions=me");
      await expect(tile(page, "season_changes_to_approve")).toHaveAttribute("href", "/pricing/seasons?approvable=me");
      // AA-1190: the three zero tiles fold into one line; a click shows them
      await expect(tile(page, "b2b_on_request")).toHaveCount(0);
      await expect(page.getByTestId("my-work-empty-toggle")).toContainText("Nothing waiting in 3 queues");
      await page.getByTestId("my-work-empty-toggle").click();
      await expect(tile(page, "b2b_on_request")).toHaveAttribute("href", "/b2b-desk");
      await expect(tile(page, "addons_to_publish")).toHaveAttribute("href", "/add-ons?approvable=me");
      await expect(tile(page, "holiday_events_to_approve")).toHaveAttribute("href", "/pricing/holiday-events?approvable=me");
      // AA-1190: the price setup progress card: only the steps with plans, each opening the workbench on that step
      const setup = page.getByTestId("my-work-price-setup");
      await expect(page.getByTestId("my-work-price-setup-ready")).toHaveText("30 of 55 tours ready to buy");
      await expect(page.getByTestId("my-work-price-setup-help")).toHaveText(/^Tours by their next price setup step\./);
      await expect(setup).not.toContainText("ready to buy Tours");
      await setup.screenshot({ path: path.join(OUT, `my-work-price-setup-1440-${scheme}.png`) });
      await expect(page.getByTestId("my-work-price-setup-add_components")).toHaveAttribute("href", "/pricing/workbench?next_step=add_components");
      await expect(page.getByTestId("my-work-price-setup-load_rates")).toContainText("7");
      await expect(page.getByTestId("my-work-price-setup-approve_fx")).toHaveCount(0);
      await expect(tile(page, "agency_payments_to_approve")).toHaveAttribute("href", "/agency-billing?tab=payments&approvable=me");
      await expect(tile(page, "supplier_bills_to_approve")).toHaveAttribute("href", "/billing?bill_status=received&approvable=me");
      await expect(tile(page, "stop_sales_to_review")).toHaveAttribute("href", "/stop-sales?review=me");
      // AA-1177: the contracts ending within the policy window open the Suppliers list of them.
      await expect(tile(page, "contracts_ending_soon")).toHaveAttribute("href", "/suppliers?contracts=ending");
      await expect(tile(page, "contracts_ending_soon")).toContainText("Within 60 days, first due");
      await expect(tile(page, "agency_invoices_overdue")).toContainText("Not available yet");
      await expect(tile(page, "b2b_on_request")).toContainText("Nothing waits for an answer");
      await expect(tile(page, "supplier_bills_due_soon")).toContainText("Within 3 days, first due");
      await expect(band(page).getByRole("link", { name: /CIS: My work/ })).toHaveAttribute("href", /\/admin\/my-work$/);
      const before = summaryCalls();
      await page.getByTestId("my-work-refresh").click();
      await expect.poll(summaryCalls).toBeGreaterThan(before);
      await band(page).screenshot({ path: path.join(OUT, `my-work-1440-${scheme}.png`) });
      expect(errors).toEqual([]);
    });

    test("390: the band stacks and the page never scrolls sideways", async ({ page, baseURL }) => {
      await page.setViewportSize({ width: 390, height: 844 });
      const { errors } = await prepare(page, baseURL!);
      await page.goto("/", { waitUntil: "domcontentloaded" });
      await expect(tile(page, "supplier_bills_to_pay")).toContainText("2");
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      await band(page).screenshot({ path: path.join(OUT, `my-work-390-${scheme}.png`) });
      expect(errors).toEqual([]);
    });
  });
}

test("Vietnamese labels when VI is picked", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await prepare(page, baseURL!);
  // The UI language cookie the EN | VI switch writes (ui/local/lang.ts).
  await page.context().addCookies([{ name: "bk_lang", value: "vi", url: new URL(baseURL!).origin }]);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(band(page)).toContainText("Việc của tôi");
  await expect(tile(page, "supplier_bills_to_approve")).toContainText("HĐ nhà cung cấp cần duyệt");
  await band(page).screenshot({ path: path.join(OUT, "my-work-1440-vi.png") });
});

test("the stop-sale tile's list: only what the viewer may decide, phone width without sideways scroll", async ({ page, baseURL }) => {
  const { errors } = await prepare(page, baseURL!);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/stop-sales?review=me", { waitUntil: "domcontentloaded" });
  await expect(page.getByText("2 waiting")).toBeVisible();
  await expect(page.getByText("Mekong Riverside Co.")).toBeVisible();
  // The fixture's first stop-sale was entered by the viewer: "Only what I can decide" leaves it out.
  await expect(page.getByText("Lotus Bay Travel")).toHaveCount(0);
  await expect(page.getByText("Lifting it")).toBeVisible();
  await page.screenshot({ path: path.join(OUT, "stop-sales-1440-light.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  expect(errors).toEqual([]);
});

test("the contracts ending soon list: every kind on Suppliers, phone width without sideways scroll", async ({ page, baseURL }) => {
  const { errors } = await prepare(page, baseURL!);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/suppliers?contracts=ending", { waitUntil: "domcontentloaded" });
  const panel = page.getByTestId("contracts-expiring");
  await expect(panel).toContainText("end within 60 days");
  await expect(page.getByTestId("contracts-expiring-row")).toHaveCount(2);
  await expect(panel).toContainText("Mekong Riverside Co.");
  await expect(panel).toContainText("E-mailed");
  await expect(panel).toContainText("Notified");
  await expect(page.getByText("Contracts ending within 60 days")).toBeVisible();
  await page.screenshot({ path: path.join(OUT, "suppliers-contracts-ending-1440-light.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: path.join(OUT, "suppliers-contracts-ending-390-light.png"), fullPage: true });
  expect(errors).toEqual([]);
});

// --- AA-1190: a tile click opens its list filtered, exactly like a direct load of its link -------------------------

const QUIET_MS = 800;
const SETTLE_CAP_MS = 8_000;

/** Records the BFF reads (/api/booking/<path>?query) and waits until none has been in flight for a while. */
function recorder(page: Page, origin: string) {
  const seen: string[] = [];
  let inflight = 0;
  let last = Date.now();
  let on = false;
  const isApi = (u: string) => u.startsWith(origin) && new URL(u).pathname.startsWith("/api/booking/");
  page.on("request", (r) => {
    if (!isApi(r.url())) return;
    inflight++;
    last = Date.now();
    if (on) { const u = new URL(r.url()); seen.push(`${u.pathname}${u.search}`); }
  });
  const done = (r: { url(): string }) => { if (isApi(r.url())) { inflight = Math.max(0, inflight - 1); last = Date.now(); } };
  page.on("requestfinished", done);
  page.on("requestfailed", done);
  return {
    // the quiet window starts now: a page whose first read only starts after hydration is still waited for
    start() { seen.length = 0; on = true; last = Date.now(); },
    async settle(): Promise<string[]> {
      const t0 = Date.now();
      while (Date.now() - t0 < SETTLE_CAP_MS && (inflight > 0 || Date.now() - last < QUIET_MS)) await page.waitForTimeout(100);
      on = false;
      return [...seen];
    },
  };
}

const pathOf = (read: string) => read.split("?")[0];

for (const t of MY_WORK.tiles.filter((x) => Object.keys(x.filter).length > 0)) {
  test(`tile ${t.key}: a click opens its list with the same filtered reads as a direct load`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await prepare(page, baseURL!);
    const rec = recorder(page, new URL(baseURL!).origin);
    // the dashboard's own reads (the shell and the band): the target's reads are the others
    rec.start();
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(tile(page, "supplier_bills_to_approve")).toContainText("5");
    const dashboard = new Set((await rec.settle()).map(pathOf));
    if (!(await tile(page, t.key).count())) await page.getByTestId("my-work-empty-toggle").click();
    const link = tile(page, t.key);
    const target = (await link.getAttribute("href")) ?? "";
    expect(target.startsWith("/") && target.includes("?"), `${t.key} links to ${target}`).toBe(true);

    rec.start();
    await link.click();
    await page.waitForURL((u) => `${u.pathname}${u.search}` === target, { timeout: 20_000 });
    const clicked = await rec.settle();

    rec.start();
    await page.goto(target, { waitUntil: "domcontentloaded" });
    const direct = (await rec.settle()).filter((r) => !dashboard.has(pathOf(r)));
    expect(direct.length, `${target}: a direct load makes no read of its own`).toBeGreaterThan(0);
    for (const r of new Set(direct)) expect(clicked, `${target}: a direct load reads ${r}; after the click it did not`).toContain(r);
  });
}
