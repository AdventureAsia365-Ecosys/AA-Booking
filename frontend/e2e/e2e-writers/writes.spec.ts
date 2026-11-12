// e2e/e2e-writers/writes.spec.ts
// AA-1087 (S278, root ADR 0006a A10e) — the Dev-only write-capable E2E staff identities sign in through the managed
// Cognito login (email code, no password) and each makes ONE write on a synthetic agency's record through the BFF of
// the signed-in browser session, then shows the same write refused on a real agency's record (403):
//
//   1. e2e-booking-staff@ (booking-sales): send the synthetic supplier booking (if it is still pending), confirm it;
//   2. e2e-booking-maker@ (booking-finance): record a payment on the synthetic agency invoice;
//   3. e2e-booking-checker@ (booking-finance): approve that payment (maker-checker: never the recorder);
//   4. e2e-booking-staff@ again (A10e-1, S278 l18): cancel the synthetic departure (body exactly {"status":
//      "cancelled"}); a departure a real agency holds seats on is refused 403 e2e_synthetic_only, any other change 403
//      e2e_read_only. The agency CRM then opens its aa_cancel_case (crm-13; checked in AA-Partner);
//   5. the A10a read-only admin (e2e-booking@, inbound/) reads the audit log: each write above names its identity.
//
// The records come from the Dev-only seeder (backend app/e2e_fixtures.py, one run = one fresh set):
//   python -m app.e2e_fixtures --apply --run <run>   ->  its JSON output, saved locally, is BOOKING_E2E_FIXTURES.
//
// Agent-only, NEVER CI: the identities' codes land in the mailbox prefix inbound-admin/, which only the MFA admin role
// reads (AA-CIS-Infra e2e_mailbox.tf). The run writes on Dev, so every write is opt-in:
//
//   BOOKING_E2E_WRITERS=1                 required, else every test skips
//   BOOKING_E2E_WRITERS_DRY_RUN=1         sign in and read the records only; no POST (the default lane check)
//   BOOKING_E2E_FIXTURES=<file>           the seeder's APPLY output: fills the three SYNTHETIC_* values below
//   SYNTHETIC_SUPPLIER_BOOKING=<uuid>     a supplier booking of a synthetic agency's B2B case (pending or requested)
//   SYNTHETIC_AGENCY_INVOICE=<uuid|AAS-…> an issued invoice of a synthetic agency with an open balance
//   SYNTHETIC_DEPARTURE=<uuid>            a departure only synthetic agencies hold seats on (test 4)
//   REAL_SUPPLIER_BOOKING / REAL_AGENCY_INVOICE / REAL_DEPARTURE   optional: real records, for the 403 checks
//   PAYMENT_AMOUNT=<decimal>              optional, default 1.00
//
//   cd frontend && AWS_PROFILE=aa365-admin AWS_REGION=us-west-1 BOOKING_E2E_WRITERS=1 SYNTHETIC_SUPPLIER_BOOKING=… \
//     SYNTHETIC_AGENCY_INVOICE=… npx playwright test --project=e2e-writers
//
// Runs with trace, video and screenshots off (playwright.config.ts): the code page is never captured. Each identity's
// session lives only in its own browser context (nothing saved to disk). The audit log (/audit-log, admin only) then
// shows each write with the identity as the actor.
import { readFileSync } from "node:fs";
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
/** The A10a read-only admin (codes in the CI-readable inbound/): it reads the audit log, never writes. */
export const E2E_READ_ONLY_ADMIN_EMAIL = "e2e-booking@e2e-mail.lumiguides.it.com";

type Fixtures = {
  supplier_booking?: { id?: string };
  invoice?: { id?: string; number?: string };
  departure_id?: string;
};

/** The seeder's APPLY output ({"mode": "APPLY", "fixtures": {...}}), or {} when BOOKING_E2E_FIXTURES is unset. */
export function loadFixtures(path: string | undefined): Fixtures {
  if (!path) return {};
  const parsed = JSON.parse(readFileSync(path, "utf-8")) as { fixtures?: Fixtures };
  if (!parsed.fixtures) throw new Error(`${path} is not the seeder's APPLY output (no "fixtures")`);
  return parsed.fixtures;
}

const FIXTURES = loadFixtures(process.env.BOOKING_E2E_FIXTURES);
const SB = process.env.SYNTHETIC_SUPPLIER_BOOKING ?? FIXTURES.supplier_booking?.id ?? "";
const INVOICE = process.env.SYNTHETIC_AGENCY_INVOICE ?? FIXTURES.invoice?.number ?? FIXTURES.invoice?.id ?? "";
const DEPARTURE = process.env.SYNTHETIC_DEPARTURE ?? FIXTURES.departure_id ?? "";
const REAL_SB = process.env.REAL_SUPPLIER_BOOKING ?? "";
const REAL_INVOICE = process.env.REAL_AGENCY_INVOICE ?? "";
const REAL_DEPARTURE = process.env.REAL_DEPARTURE ?? "";
const AMOUNT = process.env.PAYMENT_AMOUNT ?? "1.00";
const RUN = `aa1087-${Date.now()}`;

test.describe.configure({ mode: "serial" });

let paymentId = "";
/** What the audit check looks for: (identity staff key, record id) of every write that went through. */
const written: { actor: string; entity: string; what: string }[] = [];

async function signedIn(
  browser: Browser,
  email: string,
  prefix: string = MAILBOX_ADMIN_PREFIX,
): Promise<{ context: BrowserContext; page: Page; staffKey: string }> {
  const origin = new URL(baseUrl()).origin;
  if (origin !== DEFAULT_BASE_URL) throw new Error(`needs ${DEFAULT_BASE_URL} (got ${origin})`);
  const context = await browser.newContext({ baseURL: baseUrl() });
  const page = await context.newPage();
  await page.goto("/login", { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.getByRole("link", { name: /sign in with company email/i }).click({ timeout: 15_000 });
  await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20_000 });
  await signInByEmailCode(page, { email, prefix });
  await page.waitForURL((u) => u.origin === origin && !u.pathname.startsWith("/login") && !u.pathname.startsWith("/api/auth"), {
    timeout: 30_000,
  });
  const me = await page.request.get("/api/booking/staff/me", { headers: { Accept: "application/json" } });
  expect(me.status(), "staff/me answers for the E2E identity").toBe(200);
  const staffKey = String(((await me.json()) as { cis_user_id?: string }).cis_user_id ?? "");
  return { context, page, staffKey };
}

/** A write from the signed-in page itself (same-origin fetch through the BFF, as the UI sends it). */
async function post(page: Page, path: string, data?: unknown, headers: Record<string, string> = {}, method = "POST") {
  return page.evaluate(
    async ({ url, payload, extra, verb }) => {
      const res = await fetch(url, {
        method: verb,
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
    { url: `/api/booking${path}`, payload: data, extra: headers, verb: method },
  );
}

test("booking staff confirms a synthetic agency's supplier booking", async ({ browser }) => {
  test.skip(!ON || !SB, "opt-in: BOOKING_E2E_WRITERS=1 SYNTHETIC_SUPPLIER_BOOKING=<uuid>");
  test.setTimeout(240_000);
  const { context, page, staffKey } = await signedIn(browser, E2E_BOOKING_STAFF_EMAIL);
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
    written.push({ actor: staffKey, entity: SB, what: "supplier booking confirmed" });
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
  const { context, page, staffKey } = await signedIn(browser, E2E_FINANCE_MAKER_EMAIL);
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
    written.push({ actor: staffKey, entity: paymentId, what: "payment recorded" });
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
  const { context, page, staffKey } = await signedIn(browser, E2E_FINANCE_CHECKER_EMAIL);
  try {
    // The checker never records.
    const record = await post(page, `/agency-invoices/${encodeURIComponent(INVOICE)}/payments`, {
      amount: AMOUNT, method: "cash", received_on: new Date().toISOString().slice(0, 10), document_ref: "not allowed",
    }, { "Idempotency-Key": `${RUN}-checker` });
    expect([record.status, record.body?.detail]).toEqual([403, "e2e_read_only"]);
    const approved = await post(page, `/agency-payments/${paymentId}/approve`);
    expect(approved.status, JSON.stringify(approved.body)).toBe(200);
    expect(approved.body?.status).toBe("approved");
    written.push({ actor: staffKey, entity: paymentId, what: "payment approved" });
  } finally {
    await context.close();
  }
});

test("booking staff cancels a synthetic departure (A10e-1)", async ({ browser }) => {
  test.skip(!ON || !DEPARTURE, "opt-in: BOOKING_E2E_WRITERS=1 SYNTHETIC_DEPARTURE=<uuid> (or BOOKING_E2E_FIXTURES)");
  test.setTimeout(240_000);
  const { context, page, staffKey } = await signedIn(browser, E2E_BOOKING_STAFF_EMAIL);
  try {
    const path = `/catalog/departures/${DEPARTURE}`;
    // Anything but the cancel itself is refused before the route runs, dry run or not.
    const resize = await post(page, path, { capacity: 3 }, {}, "PATCH");
    expect([resize.status, resize.body?.detail]).toEqual([403, "e2e_read_only"]);
    const mixed = await post(page, path, { status: "cancelled", capacity: 3 }, {}, "PATCH");
    expect([mixed.status, mixed.body?.detail]).toEqual([403, "e2e_read_only"]);
    if (REAL_DEPARTURE) {
      const refused = await post(page, `/catalog/departures/${REAL_DEPARTURE}`, { status: "cancelled" }, {}, "PATCH");
      expect([refused.status, refused.body?.detail]).toEqual([403, "e2e_synthetic_only"]);
    }
    if (DRY_RUN) {
      console.log("[e2e-writers] dry run: refusals checked; the synthetic departure is not cancelled");
      return;
    }
    const cancelled = await post(page, path, { status: "cancelled" }, {}, "PATCH");
    expect(cancelled.status, JSON.stringify(cancelled.body)).toBe(200);
    expect(cancelled.body?.status).toBe("cancelled");
    written.push({ actor: staffKey, entity: DEPARTURE, what: "departure cancelled" });
    // The grant held for that one request only: the same session still cannot change the departure otherwise.
    const after = await post(page, path, { capacity: 3 }, {}, "PATCH");
    expect([after.status, after.body?.detail]).toEqual([403, "e2e_read_only"]);
  } finally {
    await context.close();
  }
});

test("the audit log names each identity as the actor of its write", async ({ browser }) => {
  test.skip(!ON || DRY_RUN, "opt-in: BOOKING_E2E_WRITERS=1, not a dry run");
  test.skip(written.length === 0, "no write went through in this run");
  test.setTimeout(240_000);
  // A10a: booking-admin reads (audit.read), never writes; its codes are in the default inbound/ prefix.
  const { context, page } = await signedIn(browser, E2E_READ_ONLY_ADMIN_EMAIL, "inbound/");
  try {
    for (const w of written) {
      const res = await page.request.get(
        `/api/booking/audit-logs?actor=${encodeURIComponent(w.actor)}&entity_id=${encodeURIComponent(w.entity)}&limit=20`,
        { headers: { Accept: "application/json" } },
      );
      expect(res.status(), w.what).toBe(200);
      const items = ((await res.json()) as { items?: { actor_ref?: string }[] }).items ?? [];
      expect(items.length, `${w.what}: an audit row by ${w.actor}`).toBeGreaterThan(0);
      expect(items.every((i) => i.actor_ref === w.actor)).toBe(true);
    }
  } finally {
    await context.close();
  }
});
