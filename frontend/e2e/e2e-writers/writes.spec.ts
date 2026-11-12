// e2e/e2e-writers/writes.spec.ts
// AA-1087 (S278, root ADR 0006a A10e) — the Dev-only write-capable E2E staff identities sign in through the managed
// Cognito login (email code, no password) and each makes ONE write on a synthetic agency's record through the BFF of
// the signed-in browser session, then shows the same write refused on a real agency's record (403):
//
//   1. e2e-booking-staff@ (booking-sales): send the synthetic supplier booking (if it is still pending), confirm it;
//   2. e2e-booking-maker@ (booking-finance): record a payment on the synthetic agency invoice;
//   3. e2e-booking-checker@ (booking-finance): approve that payment (maker-checker: never the recorder).
//
// Agent-only, NEVER CI: the identities' codes land in the mailbox prefix inbound-admin/, which only the MFA admin role
// reads (AA-CIS-Infra e2e_mailbox.tf). The run writes on Dev, so every write is opt-in:
//
//   BOOKING_E2E_WRITERS=1                 required, else every test skips
//   BOOKING_E2E_WRITERS_DRY_RUN=1         sign in and read the records only; no POST (the default lane check)
//   SYNTHETIC_SUPPLIER_BOOKING=<uuid>     a supplier booking of a synthetic agency's B2B case (pending or requested)
//   SYNTHETIC_AGENCY_INVOICE=<uuid|AAS-…> an issued invoice of a synthetic agency with an open balance
//   REAL_SUPPLIER_BOOKING / REAL_AGENCY_INVOICE   optional: a real agency's records, for the 403 checks
//   PAYMENT_AMOUNT=<decimal>              optional, default 1.00
//
//   cd frontend && AWS_PROFILE=aa365-admin AWS_REGION=us-west-1 BOOKING_E2E_WRITERS=1 SYNTHETIC_SUPPLIER_BOOKING=… \
//     SYNTHETIC_AGENCY_INVOICE=… npx playwright test --project=e2e-writers
//
// Runs with trace, video and screenshots off (playwright.config.ts): the code page is never captured. Each identity's
// session lives only in its own browser context (nothing saved to disk). The audit log (/audit-log, admin only) then
// shows each write with the identity as the actor.
import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";
import { STAFF_AUTH_HOST, signInByEmailCode } from "../signed-in/helpers/cognitoSignIn";
import { baseUrl, DEFAULT_BASE_URL } from "../signed-in/helpers/env";

/** Must equal backend app/auth/e2e_staff.py and AA-CIS-Infra workforce.E2E_DEV_ONLY_TEST_STAFF. */
export const E2E_BOOKING_STAFF_EMAIL = "e2e-booking-staff@e2e-mail.lumiguides.it.com";
export const E2E_FINANCE_MAKER_EMAIL = "e2e-booking-maker@e2e-mail.lumiguides.it.com";
export const E2E_FINANCE_CHECKER_EMAIL = "e2e-booking-checker@e2e-mail.lumiguides.it.com";
/** The MFA-admin-only mailbox prefix (rule e2e-cognito-admin). */
export const MAILBOX_ADMIN_PREFIX = "inbound-admin/";

const ON = process.env.BOOKING_E2E_WRITERS === "1";
const DRY_RUN = process.env.BOOKING_E2E_WRITERS_DRY_RUN === "1";
const SB = process.env.SYNTHETIC_SUPPLIER_BOOKING ?? "";
const INVOICE = process.env.SYNTHETIC_AGENCY_INVOICE ?? "";
const REAL_SB = process.env.REAL_SUPPLIER_BOOKING ?? "";
const REAL_INVOICE = process.env.REAL_AGENCY_INVOICE ?? "";
const AMOUNT = process.env.PAYMENT_AMOUNT ?? "1.00";
const RUN = `aa1087-${Date.now()}`;

test.describe.configure({ mode: "serial" });

let paymentId = "";

async function signedIn(browser: Browser, email: string): Promise<{ context: BrowserContext; page: Page }> {
  const origin = new URL(baseUrl()).origin;
  if (origin !== DEFAULT_BASE_URL) throw new Error(`needs ${DEFAULT_BASE_URL} (got ${origin})`);
  const context = await browser.newContext({ baseURL: baseUrl() });
  const page = await context.newPage();
  await page.goto("/login", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.getByRole("link", { name: /sign in with company email/i }).click({ timeout: 15_000 });
  await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20_000 });
  await signInByEmailCode(page, { email, prefix: MAILBOX_ADMIN_PREFIX });
  await page.waitForURL((u) => u.origin === origin && !u.pathname.startsWith("/login") && !u.pathname.startsWith("/api/auth"), {
    timeout: 30_000,
  });
  const me = await page.request.get("/api/booking/staff/me", { headers: { Accept: "application/json" } });
  expect(me.status(), "staff/me answers for the E2E identity").toBe(200);
  return { context, page };
}

/** A write from the signed-in page itself (same-origin fetch through the BFF, as the UI sends it). */
async function post(page: Page, path: string, data?: unknown, headers: Record<string, string> = {}) {
  return page.evaluate(
    async ({ url, payload, extra }) => {
      const res = await fetch(url, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json", ...extra },
        body: JSON.stringify(payload ?? {}),
        credentials: "same-origin",
      });
      let body: Record<string, unknown> | null = null;
      try {
        body = (await res.json()) as Record<string, unknown>;
      } catch {
        body = null;
      }
      return { status: res.status, body };
    },
    { url: `/api/booking${path}`, payload: data, extra: headers },
  );
}

test("booking staff confirms a synthetic agency's supplier booking", async ({ browser }) => {
  test.skip(!ON || !SB, "opt-in: BOOKING_E2E_WRITERS=1 SYNTHETIC_SUPPLIER_BOOKING=<uuid>");
  test.setTimeout(240_000);
  const { context, page } = await signedIn(browser, E2E_BOOKING_STAFF_EMAIL);
  try {
    const read = await page.request.get(`/api/booking/supplier-bookings/${SB}`, { headers: { Accept: "application/json" } });
    expect(read.status()).toBe(200);
    const status = ((await read.json()) as { status?: string }).status;
    if (DRY_RUN) {
      console.log(`[e2e-writers] dry run: supplier booking is ${status}; no write`);
      return;
    }
    if (status === "pending") expect((await post(page, `/supplier-bookings/${SB}/request`)).status).toBe(200);
    const confirmed = await post(page, `/supplier-bookings/${SB}/confirm`, { confirmation_ref: RUN.slice(0, 100) });
    expect(confirmed.status, JSON.stringify(confirmed.body)).toBe(200);
    expect(confirmed.body?.status).toBe("confirmed");
    if (REAL_SB) {
      const refused = await post(page, `/supplier-bookings/${REAL_SB}/confirm`, { confirmation_ref: RUN.slice(0, 100) });
      expect([refused.status, refused.body?.detail]).toEqual([403, "e2e_synthetic_only"]);
    }
    // Any write outside its allow-list is refused before the route runs.
    const other = await post(page, `/supplier-bookings/${SB}/cancel`, { reason: "not allowed for this identity" });
    expect([other.status, other.body?.detail]).toEqual([403, "e2e_read_only"]);
  } finally {
    await context.close();
  }
});

test("finance maker records a payment on a synthetic agency invoice", async ({ browser }) => {
  test.skip(!ON || !INVOICE, "opt-in: BOOKING_E2E_WRITERS=1 SYNTHETIC_AGENCY_INVOICE=<id or number>");
  test.setTimeout(240_000);
  const { context, page } = await signedIn(browser, E2E_FINANCE_MAKER_EMAIL);
  try {
    const read = await page.request.get(`/api/booking/agency-invoices/${encodeURIComponent(INVOICE)}`, {
      headers: { Accept: "application/json" },
    });
    expect(read.status()).toBe(200);
    if (DRY_RUN) {
      console.log("[e2e-writers] dry run: the invoice reads; no write");
      return;
    }
    const body = {
      amount: AMOUNT,
      method: "bank_transfer",
      received_on: new Date().toISOString().slice(0, 10),
      document_ref: `synthetic rehearsal ${RUN}`,
    };
    const recorded = await post(page, `/agency-invoices/${encodeURIComponent(INVOICE)}/payments`, body, { "Idempotency-Key": RUN });
    expect(recorded.status, JSON.stringify(recorded.body)).toBe(201);
    paymentId = String(recorded.body?.id ?? "");
    expect(paymentId).not.toBe("");
    if (REAL_INVOICE) {
      const refused = await post(page, `/agency-invoices/${encodeURIComponent(REAL_INVOICE)}/payments`, body, {
        "Idempotency-Key": `${RUN}-real`,
      });
      expect([refused.status, refused.body?.detail]).toEqual([403, "e2e_synthetic_only"]);
    }
    // The maker never approves, not even its own payment.
    const approve = await post(page, `/agency-payments/${paymentId}/approve`);
    expect([approve.status, approve.body?.detail]).toEqual([403, "e2e_read_only"]);
  } finally {
    await context.close();
  }
});

test("finance checker approves the maker's payment", async ({ browser }) => {
  test.skip(!ON || !INVOICE || DRY_RUN, "opt-in: BOOKING_E2E_WRITERS=1 SYNTHETIC_AGENCY_INVOICE=<…>, not a dry run");
  test.skip(!paymentId, "the maker test recorded no payment");
  test.setTimeout(240_000);
  const { context, page } = await signedIn(browser, E2E_FINANCE_CHECKER_EMAIL);
  try {
    // The checker never records.
    const record = await post(page, `/agency-invoices/${encodeURIComponent(INVOICE)}/payments`, {
      amount: AMOUNT, method: "cash", received_on: new Date().toISOString().slice(0, 10), document_ref: "not allowed",
    }, { "Idempotency-Key": `${RUN}-checker` });
    expect([record.status, record.body?.detail]).toEqual([403, "e2e_read_only"]);
    const approved = await post(page, `/agency-payments/${paymentId}/approve`);
    expect(approved.status, JSON.stringify(approved.body)).toBe(200);
    expect(approved.body?.status).toBe("approved");
  } finally {
    await context.close();
  }
});
