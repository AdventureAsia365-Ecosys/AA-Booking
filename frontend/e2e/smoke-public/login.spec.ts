// e2e/smoke-public/login.spec.ts
// AA-993 V5 — public smoke of the DEPLOYED staff admin (project `smoke-public` in playwright.config.ts). Runs after
// every deploy (.github/workflows/deploy-admin-dev.yml, job `smoke`) against the production host. Public
// page only: no sign-in, no cookie, no secret, nothing written. /login is the one page that answers 200 without a
// session (proxy.ts PUBLIC_PATHS); every other page redirects to it.
//
// What it checks on GET /login:
//   - HTTP 200 on the same origin and path (a Vercel Authentication wall or a redirect elsewhere fails);
//   - the sign-in card renders, with "Sign in with company email" (the production build has the booking-staff
//     client secret; without it the button disappears and staff have no way in since the legacy form is gone);
//   - no console error, no uncaught page error, no failed or >= 400 same-origin request while the page loads;
//   - the response carries every header next.config.ts sets, with the same value, and at least the six AA-879
//     security headers (CSP frame-ancestors, HSTS, X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy);
//     no X-Powered-By. AA-1111 (S284): plus COOP; the CSP comes from proxy.ts with a per-response nonce (scripts by
//     nonce, no 'unsafe-inline' for scripts, default-src 'self'), so the page loading without a console error also
//     proves every script it needs carries the nonce. /robots.txt, a 404 and the BFF carry the set too.
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
  "cross-origin-opener-policy",
];

/** AA-1111: the full policy, scripts by nonce only (proxy.ts / lib/securityHeaders.ts). */
function expectNonceCsp(csp: string | undefined, label: string, nonce = true) {
  expect(csp, `${label}: CSP`).toContain("frame-ancestors 'none'");
  expect(csp, `${label}: CSP default-src`).toContain("default-src 'self'");
  const script = (csp ?? "").split(";").map((d) => d.trim()).find((d) => d.startsWith("script-src ")) ?? "";
  expect(script, `${label}: script-src without unsafe-inline`).not.toContain("'unsafe-inline'");
  if (nonce) expect(script, `${label}: script-src nonce`).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
}

async function configuredHeaders(): Promise<Record<string, string>> {
  const rules = (await nextConfig.headers?.()) ?? [];
  const all = rules.find((r) => r.source === "/:path*");
  expect(all, "next.config.ts headers() has a /:path* rule").toBeTruthy();
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
    expect(Object.keys(got), `the response carries ${name}`).toContain(name);
  }
  for (const [name, value] of Object.entries(want)) {
    expect(got[name], `response header ${name}`).toBe(value);
  }
  expectNonceCsp(got["content-security-policy"], "/login");
  expect(got["x-powered-by"], "no X-Powered-By").toBeUndefined();

  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS_DIR, "login-desktop.png"), fullPage: true });

  expect(failed, "failed same-origin requests").toEqual([]);
  expect(consoleErrors, "console errors").toEqual([]);
});

// AA-1111 (S284, ZAP baseline): a static route, a 404 (the gate sends it to /login: a 307) and the BFF carry the set.
for (const target of ["/robots.txt", "/aa1111-no-such-page", "/api/auth/session"]) {
  test(`GET ${target} carries the security headers`, async ({ request }) => {
    const res = await request.get(target, { maxRedirects: 0 });
    const got = res.headers();
    for (const name of REQUIRED_HEADERS) expect(Object.keys(got), `${target}: ${name}`).toContain(name);
    expectNonceCsp(got["content-security-policy"], target, !target.startsWith("/api/"));
    expect(got["x-powered-by"], `${target}: no X-Powered-By`).toBeUndefined();
  });
}
