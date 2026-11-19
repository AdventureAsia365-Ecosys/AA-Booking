// e2e/visual/session.ts
// The visual run's fake staff session (AA-1008, moved here for AA-1131 so the keyboard spec can share it): proxy.ts
// sends every path but /login to the sign-in page without a `bk_access` cookie, the kit gallery included. The token
// is an unsigned JWT shaped like a Cognito access token: proxy.ts and the (staff) layout only decode it, and trust lives
// in the Booking API, which the visual run mocks (playwright.config.ts explains why this is safe).
import type { Page } from "@playwright/test";

/** An unsigned JWT shaped like a Cognito access token (the layout check's: proxy.ts only decodes it). */
export function fakeAccessToken(): string {
  const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none", typ: "JWT" })}.${b64({ token_use: "access", client_id: "layout-mock", sub: "u-layout", exp: 4102444800 })}.layout-mock`;
}

/** Put the fake session cookie on the page's context for `origin`. AA-1128: VISUAL_LANG=vi also sets the UI language
 *  cookie (`bk_lang`, ui/local/lang.ts), so the same run checks every page in Vietnamese (console errors such as a
 *  hydration mismatch, overflow from longer text, unnamed controls). */
export async function addFakeSession(page: Page, origin: string): Promise<void> {
  const cookies = [{ name: "bk_access", value: fakeAccessToken(), url: origin }];
  if (process.env.VISUAL_LANG === "vi") cookies.push({ name: "bk_lang", value: "vi", url: origin });
  await page.context().addCookies(cookies);
}
