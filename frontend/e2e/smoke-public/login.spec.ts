// e2e/smoke-public/login.spec.ts
// AA-993 V5 — public smoke of the DEPLOYED staff admin (project `smoke-public` in playwright.config.ts). Runs after
// every production deploy (.github/workflows/vercel-deploy.yml, job `smoke`) against the production host. Public
// page only: no sign-in, no cookie, no secret, nothing written. /login is the one page that answers 200 without a
// session (proxy.ts PUBLIC_PATHS); every other page redirects to it.
//
// What it checks on GET /login:
//   - HTTP 200 on the same origin and path (a Vercel Authentication wall or a redirect elsewhere fails);
//   - the sign-in card renders, with "Sign in with company email" (the production build has the booking-staff
//     client secret; without it the button disappears and staff would only see the legacy form);
//   - no console error, no uncaught page error, no failed or >= 400 same-origin request while the page loads;
//   - the response carries every header next.config.ts sets, with the same value, and at least the six AA-879
//     security headers (CSP frame-ancestors, HSTS, X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy);
//     no X-Powered-By.
// A full-page screenshot lands in SMOKE_SHOTS_DIR (default e2e/smoke-public/results), uploaded by the workflow.
import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import nextConfig from "../../next.config";

const SHOTS_DIR = process.env.SMOKE_SHOTS_DIR || path.join(__dirname, "results");

// The AA-879 set: the smoke fails if next.config.ts ever drops one of these, not only if the deployment does.
const REQUIRED_HEADERS = [
  "content-security-policy",
  "strict-transport-security",
  "x-frame-options",
  "x-content-type-options",
  "referrer-policy",
  "permissions-policy",
];

async function configuredHeaders(): Promise<Record<string, string>> {
  const rules = (await nextConfig.headers?.()) ?? [];
  const all = rules.find((r) => r.source === "/:path*");
  expect(all, "next.config.ts headers() has a /:path* rule").toBeTruthy();
  // VERCEL_ENV is unset here, so this is the production set (the preview build adds only X-Robots-Tag).
  return Object.fromEntries(all!.headers.map((h) => [h.key.toLowerCase(), h.value]));
}

test("GET /login renders with the security headers and no errors", async ({ page, baseURL }) => {
  expect(baseURL, "SMOKE_BASE_URL is set").toBeTruthy();
  const origin = new URL(baseURL!).origin;

  const consoleErrors: string[] = [];
  const failed: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${err.message}`));
  page.on("requestfailed", (req) => {
    if (new URL(req.url()).origin === origin) failed.push(`${req.method()} ${req.url()} -> ${req.failure()?.errorText}`);
  });
  page.on("response", (res) => {
    if (new URL(res.url()).origin === origin && res.status() >= 400) {
      failed.push(`${res.request().method()} ${res.url()} -> HTTP ${res.status()}`);
    }
  });

  const res = await page.goto("/login", { waitUntil: "networkidle" });
  expect(res, "a response for /login").not.toBeNull();
  expect(res!.status(), "GET /login status").toBe(200);
  const landed = new URL(page.url());
  expect(landed.origin, "stayed on the admin host (no auth wall / redirect)").toBe(origin);
  expect(landed.pathname).toBe("/login");

  await expect(page.getByRole("heading", { name: "Sign in to Booking" })).toBeVisible();
  const company = page.getByRole("link", { name: "Sign in with company email" });
  await expect(company).toBeVisible();
  await expect(company).toHaveAttribute("href", /^\/api\/auth\/signin\/cognito/);

  const got = await res!.allHeaders();
  const want = await configuredHeaders();
  for (const name of REQUIRED_HEADERS) {
    expect(Object.keys(want), `next.config.ts still sets ${name}`).toContain(name);
  }
  for (const [name, value] of Object.entries(want)) {
    expect(got[name], `response header ${name}`).toBe(value);
  }
  expect(want["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(got["x-powered-by"], "no X-Powered-By").toBeUndefined();

  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS_DIR, "login-desktop.png"), fullPage: true });

  expect(failed, "failed same-origin requests").toEqual([]);
  expect(consoleErrors, "console errors").toEqual([]);
});
