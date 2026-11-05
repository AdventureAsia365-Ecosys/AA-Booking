// e2e/visual/visual.spec.ts
// AA-1008 (S1d/S2c, root ADR 0016 decision 9) — the visual run of the Booking staff admin. For every selected page of
// pages.ts, in the project's viewport (1440 x 900 or 390 x 844) and both themes:
//   - no page-level horizontal overflow: the document (`scrollWidth` of html / body vs the viewport) and the kit frame
//     (PageFrame `[data-kit-frame]`: it scrolls on its own, so a wide child there never shows in the document's
//     scrollWidth; lesson S254); the widest offenders are named;
//   - no console error and no uncaught page error (allowlist.json for known noise);
//   - no failed same-origin request (status >= 400 or a network failure), except the document's own expected status;
//     on a mocked run, no API call without a fixture;
//   - the kit frame is on the page (`[data-kit-frame]`) unless the page says frame: false; the theme applied;
//   - a PNG of the page (the inner scroll region grown into the viewport first: the shell's document never scrolls).
// Each result goes to e2e/visual/results/report/<page>__<viewport>__<theme>.json; summary.mjs merges them.
// Read-only: the run only navigates.
//
// Two targets (env):
//   VISUAL_BASE_URL unset: the local `next start` of playwright.config.ts with the layout-mock API: staff pages get the
//     layout check's unsigned session cookie, their /api/booking/* calls are answered from e2e/layout/fixtures.ts and the
//     clock is frozen at its CLOCK (the AA-910 harness; production auth is unchanged, see playwright.config.ts).
//   VISUAL_BASE_URL set: a deployed host, public pages only (no Booking E2E sign-in from CI yet).
// Selection: VISUAL_AUTH public | staff | all (default all on the mock, public on a host); VISUAL_SLUGS a comma list or
// "all" (default all), from changed-pages.mjs; VISUAL_GALLERY=1 adds the gated gallery (a KIT_GALLERY=1 server);
// VISUAL_SELFTEST=1 runs only the planted-overflow pages (expected to fail: the workflow checks they did).
import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { answer, CLOCK } from "../layout/fixtures";
import { PAGES, selectPages } from "./pages";

const THEMES = ["light", "dark"] as const;
const OUT = path.join(__dirname, "results");
const REPORT_DIR = path.join(OUT, "report");
const SHOT_DIR = path.join(OUT, "shots");
const ALLOW = JSON.parse(fs.readFileSync(path.join(__dirname, "allowlist.json"), "utf8")) as {
  console: { match: string; why: string }[];
  requests: { match: string; why: string }[];
};
const allowed = (list: { match: string }[], text: string) => list.some((a) => text.includes(a.match));

const HOST = process.env.VISUAL_BASE_URL?.replace(/\/+$/, "") || null;
const AUTH = (process.env.VISUAL_AUTH as "public" | "staff" | "all" | undefined) ?? (HOST ? "public" : "all");
const SLUGS = (process.env.VISUAL_SLUGS ?? "all").trim();
const GALLERY = process.env.VISUAL_GALLERY === "1";
const SELFTEST = process.env.VISUAL_SELFTEST === "1";

/** An unsigned JWT shaped like a Cognito access token (the layout check's: proxy.ts only decodes it). */
function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}
const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64");

/** The mocked target: the session cookie, the frozen clock, fixtures for the BFF, a pixel for external images. */
async function mock(page: Page, origin: string, unmocked: string[]) {
  await page.clock.setFixedTime(new Date(CLOCK));
  await page.context().addCookies([{ name: "bk_access", value: fakeAccessToken(), url: origin }]);
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin === origin) {
      if (!url.pathname.startsWith("/api/booking/")) return route.continue();
      const a = answer(req.method(), url.pathname.slice("/api/booking/".length), url.searchParams);
      if (!a) {
        unmocked.push(`${req.method()} ${url.pathname}${url.search}`);
        return route.fulfill({ status: 501, json: { detail: "visual_unmocked" } });
      }
      return route.fulfill({ status: a.status, json: a.body });
    }
    if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") return route.continue();
    if (req.resourceType() === "image") return route.fulfill({ status: 200, contentType: "image/png", body: PIXEL });
    return route.fulfill({ status: 204, body: "" });
  });
}

/** The overflow probe: the document and every kit frame, with the five widest offenders. */
async function probeOverflow(page: Page) {
  // The configured viewport width, not window.innerWidth: a mobile (isMobile) page that overflows widens its layout
  // viewport to the content, so innerWidth grows with the overflow and the document never looks wider than it.
  const width = page.viewportSize()?.width ?? 0;
  return page.evaluate((configured) => {
    const vw = configured || window.innerWidth;
    const doc = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
    const name = (el: Element) =>
      el.tagName.toLowerCase() + (el.id ? `#${el.id}` : "") +
      (el.getAttribute("data-testid") ? `[data-testid=${el.getAttribute("data-testid")}]` : "") +
      (typeof el.className === "string" && el.className ? `.${el.className.split(" ").slice(0, 2).join(".")}` : "");
    const frames = [...document.querySelectorAll<HTMLElement>('[data-kit-frame], [data-kit-shell="main"]')]
      .filter((el) => el.scrollWidth > el.clientWidth + 1)
      .map((el) => ({ frame: name(el), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }));
    const offenders = [...document.querySelectorAll<HTMLElement>("body *")]
      .filter((el) => {
        if (el.closest('[data-kit-scroll], [data-kit-shell="sidebar"], [aria-hidden="true"]')) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.right > vw + 1;
      })
      .sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right)
      .slice(0, 5)
      .map((el) => `${name(el)} (right ${Math.round(el.getBoundingClientRect().right)})`);
    return { doc, vw, frames, offenders };
  }, width);
}

/** Grow the viewport height to the tallest inner scroll region (bounded), so a full-page PNG shows the whole page. */
async function growToContent(page: Page) {
  const vp = page.viewportSize() ?? { width: 1440, height: 900 };
  const hidden = await page.evaluate(() => {
    let h = 0;
    for (const el of Array.from(document.body.querySelectorAll<HTMLElement>("*"))) {
      if (el.clientHeight < window.innerHeight * 0.5) continue;
      const oy = getComputedStyle(el).overflowY;
      if (oy === "auto" || oy === "scroll") h = Math.max(h, el.scrollHeight - el.clientHeight);
    }
    return Math.ceil(h);
  });
  if (hidden > 1) await page.setViewportSize({ width: vp.width, height: Math.min(vp.height + hidden, 16000) });
  return vp;
}

for (const p of selectPages(PAGES, { auth: AUTH, slugs: SLUGS, gallery: GALLERY, selftest: SELFTEST })) {
  for (const theme of THEMES) {
    test(`${p.slug} ${theme}`, async ({ page, baseURL }, info) => {
      const viewport = info.project.name.replace(/^visual-/, "");
      const origin = new URL(HOST ?? baseURL ?? "http://127.0.0.1").origin;
      const consoleErrors: string[] = [];
      const failed: string[] = [];
      const unmocked: string[] = [];
      if (!HOST) await mock(page, origin, unmocked);
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript((t) => {
        try {
          window.localStorage.setItem("bk_theme", t);
        } catch {
          /* storage blocked: the emulated colour scheme gives the same theme */
        }
      }, theme);
      const docUrl = new URL(p.path, origin).toString();
      page.on("console", (m) => {
        if (m.type() !== "error" || allowed(ALLOW.console, m.text())) return;
        // Chromium logs the document's own non-200 answer ("Failed to load resource: … 404"): expected on the 404 page.
        if (p.status && m.location().url === docUrl && m.text().startsWith("Failed to load resource")) return;
        consoleErrors.push(m.text().slice(0, 300));
      });
      page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message.slice(0, 300)}`));
      page.on("requestfailed", (r) => {
        if (r.failure()?.errorText === "net::ERR_ABORTED") return; // cancelled by the page itself (a prefetch)
        if (r.url().startsWith(origin) && !allowed(ALLOW.requests, r.url())) failed.push(`${r.failure()?.errorText ?? "failed"} ${r.url()}`);
      });
      page.on("response", (r) => {
        const url = r.url();
        if (!url.startsWith(origin) || allowed(ALLOW.requests, url)) return;
        if (url.includes("/api/booking/") && r.status() === 501 && !HOST) return; // counted as unmocked
        const expected = url === docUrl ? (p.status ?? 200) : null;
        if (expected !== null ? r.status() !== expected : r.status() >= 400) failed.push(`${r.status()} ${url}`);
      });

      const res = await page.goto(HOST ? docUrl : p.path, { waitUntil: "load" });
      // Every wait has its own ceiling (lesson S220: a polling page never reaches networkidle).
      await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
      await page.evaluate(() => document.fonts.ready.then(() => true));

      const appliedTheme = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
      const hasFrame = (await page.locator("[data-kit-frame]").count()) > 0;
      const overflow = await probeOverflow(page);

      const name = `${p.slug}__${viewport}__${theme}`;
      fs.mkdirSync(SHOT_DIR, { recursive: true });
      const vp = await growToContent(page);
      await page.screenshot({ path: path.join(SHOT_DIR, `${name}.png`), fullPage: true });
      await page.setViewportSize(vp);

      const result = {
        page: p.slug, path: p.path, auth: p.auth, viewport, theme, selftest: p.selftest ?? null,
        status: res?.status() ?? null, appliedTheme, hasFrame, overflow, consoleErrors, failedRequests: failed, unmocked,
      };
      fs.mkdirSync(REPORT_DIR, { recursive: true });
      fs.writeFileSync(path.join(REPORT_DIR, `${name}.json`), JSON.stringify(result, null, 1));

      expect.soft(res?.status(), "document status").toBe(p.status ?? 200);
      expect.soft(appliedTheme, "data-theme on <html>").toBe(theme);
      if (p.frame !== false) expect.soft(hasFrame, "the kit frame [data-kit-frame] is on the page").toBe(true);
      expect.soft(overflow.doc, `page scrolls sideways: ${overflow.offenders.join(", ")}`).toBeLessThanOrEqual(overflow.vw + 1);
      expect.soft(overflow.frames, `the page frame scrolls sideways: ${overflow.offenders.join(", ")}`).toEqual([]);
      expect.soft(consoleErrors, "console errors").toEqual([]);
      expect.soft(failed, "failed same-origin requests").toEqual([]);
      expect.soft(unmocked, "API calls without a layout-mock fixture (e2e/layout/fixtures.ts)").toEqual([]);
    });
  }
}
