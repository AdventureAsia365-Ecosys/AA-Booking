// e2e/signed-in/auth.setup.ts
// AA-1073 / AA-1074 — the setup project `signed-in-setup` of the signed-in smoke (design e2e/SIGNED-IN-SMOKE.md). It
// signs the Booking E2E staff user (e2e-booking@…, Cognito group booking-admin only, read-only session: the backend
// answers 403 e2e_read_only to every write) in ONCE with an email code read from the SES test mailbox, and saves the
// session to STATE_FILE (gitignored, a live refresh token) for the `signed-in` specs.
//
// Runs with trace, video and screenshots OFF (playwright.config.ts): the code page must never be captured.
// Needs AWS credentials that can read the mailbox: locally AWS_PROFILE=aa365-admin (MFA session) and AWS_REGION=us-west-1.
// BOOKING_E2E_REUSE_STATE=1 keeps a saved session that still answers GET staff/me 200 (a rerun asks no new code).
import fs from "node:fs";
import path from "node:path";
import { expect, request, test as setup, type Browser } from "@playwright/test";
import { STAFF_AUTH_HOST, signInByEmailCode } from "./helpers/cognitoSignIn";
import { baseUrl, DEFAULT_BASE_URL, STATE_FILE } from "./helpers/env";
import { E2E_BOOKING_EMAIL } from "./helpers/mailbox";

/** The session cookie the BFF sets after a Cognito sign-in (lib/session.ts ACCESS_COOKIE). */
const ACCESS_COOKIE = "bk_access";

async function savedStateStillValid(): Promise<boolean> {
  if (process.env.BOOKING_E2E_REUSE_STATE !== "1" || !fs.existsSync(STATE_FILE)) return false;
  const ctx = await request.newContext({ baseURL: baseUrl(), storageState: STATE_FILE });
  try {
    const res = await ctx.get("/api/booking/staff/me", { timeout: 20_000, headers: { Accept: "application/json" } });
    return res.status() === 200;
  } catch {
    return false;
  } finally {
    await ctx.dispose();
  }
}

async function signIn(browser: Browser): Promise<void> {
  const origin = new URL(baseUrl()).origin;
  if (origin !== DEFAULT_BASE_URL) {
    // Only the registered callback host completes a Cognito sign-in (AA-911); fail before any navigation.
    throw new Error(`The signed-in smoke needs BOOKING_E2E_BASE_URL ${DEFAULT_BASE_URL} (got ${origin})`);
  }
  const context = await browser.newContext({ baseURL: baseUrl() });
  const page = await context.newPage();
  try {
    await page.goto("/login", { waitUntil: "domcontentloaded", timeout: 30_000 });
    const companyEmail = page.getByRole("link", { name: /sign in with company email/i });
    await expect(companyEmail, `${origin}/login offers "Sign in with company email"`).toBeVisible({ timeout: 15_000 });
    await companyEmail.click({ timeout: 10_000 });
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20_000 });
    await signInByEmailCode(page, { email: E2E_BOOKING_EMAIL });
    await page.waitForURL((u) => u.origin === origin && !u.pathname.startsWith("/login") && !u.pathname.startsWith("/api/auth"), {
      timeout: 30_000,
    });
    const cookies = await context.cookies(origin);
    if (!cookies.some((c) => c.name === ACCESS_COOKIE && c.value !== "")) {
      throw new Error(`The sign-in came back to ${origin} but set no ${ACCESS_COOKIE} cookie`);
    }
    fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
    await context.storageState({ path: STATE_FILE });
  } finally {
    await context.close();
  }
}

setup("sign in the Booking E2E staff user", async ({ browser }) => {
  setup.setTimeout(180_000); // one code mail (up to 90 s) plus the page steps
  if (await savedStateStillValid()) {
    console.log("[signed-in] reusing the saved session (BOOKING_E2E_REUSE_STATE=1)");
    return;
  }
  await signIn(browser);
  console.log("[signed-in] signed in with an email code");
});
