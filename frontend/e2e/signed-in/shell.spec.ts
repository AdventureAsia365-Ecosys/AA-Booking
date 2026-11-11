// e2e/signed-in/shell.spec.ts
// AA-1073 / AA-1074 signed-in smoke (a): the staff shell renders for the E2E staff user and the API sees it as a full
// Booking admin (role admin, not a capped preview viewer) with the permissions the finance and departures pages need.
import { expect, test } from "@playwright/test";
import { assertClean, bffGet, capture, pageSettled, watch } from "./helpers/page";
import { E2E_BOOKING_EMAIL } from "./helpers/mailbox";

type StaffMe = { email: string; role: string; active: boolean; permissions: string[]; preview_viewer?: boolean };

test("shell + GET staff/me says role admin", async ({ page, baseURL }) => {
  const w = watch(page, new URL(baseURL!).origin);
  await page.goto("/", { waitUntil: "domcontentloaded", timeout: 30_000 });
  expect(new URL(page.url()).pathname, "a signed-in session stays off /login").not.toMatch(/^\/login/);
  await pageSettled(page);
  await expect(page.getByRole("navigation").first()).toBeVisible({ timeout: 15_000 });

  const me = await bffGet<StaffMe>(page, "staff/me");
  expect(me.status, "GET /api/booking/staff/me").toBe(200);
  expect(me.body!.email.toLowerCase()).toBe(E2E_BOOKING_EMAIL);
  expect(me.body!.role).toBe("admin");
  expect(me.body!.active).toBe(true);
  expect(me.body!.preview_viewer ?? false, "not a capped preview session").toBe(false);
  for (const p of ["profit.read", "pricing.read", "bill.read", "ops.read", "supply.read", "b2b_desk.read"]) {
    expect(me.body!.permissions, `permission ${p}`).toContain(p);
  }

  await capture(page, "home", () => pageSettled(page));
  assertClean(w, "home");
});
