// e2e/signed-in/helpers/page.ts
// AA-1073 / AA-1074 — shared steps of the signed-in smoke: a watcher for console / page / HTTP errors, a "settled"
// wait for the kit DataTable (rows or the kit empty state, never the load error), and capture(), which re-renders the
// page at 1440 and 390 in light and dark and saves one PNG each under RESULTS_DIR, with a JSON line per shot
// (report/) that the run summary is built from.
//
// Rules (lessons log, Frontend): every wait has its own cap; never `networkidle` (the shell polls notifications);
// never read a response body the page does not read itself (it may never resolve) — the watcher keeps status codes only.
import fs from "node:fs";
import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { RESULTS_DIR } from "./env";

export type Theme = "light" | "dark";
export const VIEWPORTS = [
  { name: "1440", width: 1440, height: 900 },
  { name: "390", width: 390, height: 844 },
] as const;
export const THEMES: Theme[] = ["light", "dark"];

/** Console errors, uncaught page errors and same-origin HTTP >= 400 seen since `watch()`. */
export type Watch = { consoleErrors: string[]; pageErrors: string[]; httpErrors: { status: number; url: string }[] };

export function watch(page: Page, origin: string): Watch {
  const w: Watch = { consoleErrors: [], pageErrors: [], httpErrors: [] };
  page.on("console", (msg) => {
    if (msg.type() === "error") w.consoleErrors.push(msg.text().slice(0, 300));
  });
  page.on("pageerror", (err) => w.pageErrors.push(err.message.slice(0, 300)));
  page.on("response", (res) => {
    let u: URL;
    try {
      u = new URL(res.url());
    } catch {
      return;
    }
    // Path only: no query string (it may carry ids or filters) in the report.
    if (u.origin === origin && res.status() >= 400) w.httpErrors.push({ status: res.status(), url: `${res.request().method()} ${u.pathname}` });
  });
  return w;
}

/** The page frame is up and no kit skeleton (aria-busy) is left inside it. */
export async function pageSettled(page: Page): Promise<void> {
  await expect(page.locator("[data-kit-frame]").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('main [aria-busy="true"]')).toHaveCount(0, { timeout: 30_000 });
}

/**
 * A kit DataTable inside `scope` has finished: it shows rows (`[data-kit-row]`) or the kit empty state, not the load
 * error ("Could not load"). Returns which one.
 */
export async function tableSettled(scope: Locator, timeout = 30_000): Promise<"rows" | "empty"> {
  let state = "loading";
  await expect
    .poll(
      async () => {
        const errorState = scope.locator('[data-testid="kit-empty-state"]').filter({ hasText: /could not load/i });
        if (await errorState.first().isVisible().catch(() => false)) {
          state = `error: ${((await errorState.first().textContent().catch(() => "")) ?? "").slice(0, 200)}`;
          return state;
        }
        if ((await scope.locator("[data-kit-row]").count()) > 0) return (state = "rows");
        if (await scope.locator('[data-testid="kit-empty-state"]').first().isVisible().catch(() => false)) return (state = "empty");
        return (state = "loading");
      },
      { timeout, intervals: [250, 500, 1000] },
    )
    .toMatch(/^(rows|empty|error)/);
  expect(state, "the table loaded (rows or the kit empty state, not the load error)").toMatch(/^(rows|empty)$/);
  return state as "rows" | "empty";
}

async function applyTheme(page: Page, theme: Theme): Promise<void> {
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.evaluate((t) => {
    try {
      window.localStorage.setItem("bk_theme", t);
    } catch {
      /* storage blocked: the emulated colour scheme gives the same theme */
    }
  }, theme);
}

/**
 * Re-render the current URL at every viewport x theme and save `<slug>__<viewport>__<theme>.png`. `ready` waits for
 * the content that makes the shot worth having (a table settled, a drawer open). Checks per shot (soft): the theme is
 * applied on <html>, the document has no horizontal overflow. `focus` names the part of the page the shot is about.
 */
export async function capture(
  page: Page,
  slug: string,
  ready: () => Promise<void>,
  /** Scrolled into view before the shot: the staff page scrolls inside <main>, so a full-page shot stops at the fold. */
  focus?: () => Locator,
): Promise<void> {
  fs.mkdirSync(path.join(RESULTS_DIR, "report"), { recursive: true });
  for (const vp of VIEWPORTS) {
    for (const theme of THEMES) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await applyTheme(page, theme);
      await page.reload({ waitUntil: "domcontentloaded", timeout: 30_000 });
      await ready();
      if (focus) await focus().scrollIntoViewIfNeeded({ timeout: 10_000 });
      const applied = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect.soft(applied, `${slug} ${vp.name} ${theme}: data-theme on <html>`).toBe(theme);
      expect.soft(overflow, `${slug} ${vp.name} ${theme}: no horizontal page overflow`).toBeLessThanOrEqual(1);
      const file = `${slug}__${vp.name}__${theme}.png`;
      await page.screenshot({ path: path.join(RESULTS_DIR, file), fullPage: true, animations: "disabled", timeout: 30_000 });
      const u = new URL(page.url());
      fs.writeFileSync(
        path.join(RESULTS_DIR, "report", `${slug}__${vp.name}__${theme}.json`),
        JSON.stringify({ slug, path: u.pathname, tab: u.searchParams.get("tab"), viewport: vp.name, theme, applied, overflow, file }),
      );
    }
  }
  // Back to the default for the next steps of the test.
  await page.setViewportSize({ width: 1440, height: 900 });
  await applyTheme(page, "light");
}

/** End of a test: no uncaught page error, no 5xx; console errors and 4xx are attached to the report for review. */
export function assertClean(w: Watch, slug: string): void {
  fs.mkdirSync(path.join(RESULTS_DIR, "report"), { recursive: true });
  fs.writeFileSync(path.join(RESULTS_DIR, "report", `${slug}__errors.json`), JSON.stringify(w, null, 2));
  test.info().annotations.push({
    type: "errors",
    description: `${slug}: ${w.consoleErrors.length} console error(s), ${w.httpErrors.length} HTTP >= 400`,
  });
  expect.soft(w.pageErrors, `${slug}: uncaught page errors`).toEqual([]);
  expect.soft(w.httpErrors.filter((e) => e.status >= 500), `${slug}: same-origin 5xx`).toEqual([]);
  // The E2E user is read-only (403 e2e_read_only on writes); a read page must not send writes at all.
  expect.soft(w.httpErrors.filter((e) => !e.url.startsWith("GET ")), `${slug}: a write was sent from a read-only walk`).toEqual([]);
}

/** The first row (or the kit empty state) of the table in <main>: the focus of a list shot. */
export function firstTableItem(page: Page): Locator {
  return page.locator('main [data-kit-row], main [data-testid="kit-empty-state"]').first();
}

/** GET a BFF path with the page's session cookie (never through a navigation; lesson S243). */
export async function bffGet<T>(page: Page, apiPath: string): Promise<{ status: number; body: T | null }> {
  const res = await page.request.get(`/api/booking/${apiPath.replace(/^\/+/, "")}`, { timeout: 20_000, headers: { Accept: "application/json" } });
  const status = res.status();
  return { status, body: status < 300 ? ((await res.json()) as T) : null };
}
