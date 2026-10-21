// lib/config.ts
// AA-683 — runtime configuration. Server-only values are read through functions (never inlined into
// the browser bundle); NEXT_PUBLIC_* values are inlined at build time and must be referenced literally.
//
// Dev defaults (UI spec §6, ADR 0004 O2): the Booking REST API execute-api stage until
// api-booking.lumiguides.it.com has its ACM certificate, and the live CIS API (catalog photos only; the CIS sign-in
// bridge is retired, AA-911).

export const DEV_BOOKING_API_URL = "https://api-booking.lumiguides.it.com"; // custom domain (ADR 0004 O2); same API as the execute-api stage
export const DEV_CIS_API_URL = "https://api-cis.lumiguides.it.com";
export const DEV_CIS_ADMIN_URL = "https://aa-cis.lumiguides.it.com";

function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

/** Booking API base (server only). Paths are appended as `/booking/...`. */
export function bookingApiUrl(): string {
  return trimSlash(process.env.BOOKING_API_URL || DEV_BOOKING_API_URL);
}

/** CIS admin web app, for "Managed in CIS" / "Open CIS admin" links. */
export const CIS_ADMIN_URL = trimSlash(process.env.NEXT_PUBLIC_CIS_ADMIN_URL || DEV_CIS_ADMIN_URL);

/** CIS console entry point. CIS middleware sends /admin/dashboard to the role's landing page (admin:
 *  Overview, reviewer/content: Review Queue); the bare origin redirects to Upload for every role. */
export const CIS_CONSOLE_PATH = "/admin/dashboard";

/** The CIS console link used by the topbar, the account menu and ManagedInCisLink's default. */
export const CIS_CONSOLE_URL = `${CIS_ADMIN_URL}${CIS_CONSOLE_PATH}`;

/** CIS public API base for catalog photos (contract catalog.tour.v1: GET {base}{path}?size=small|large, no auth).
 *  A constant on Dev; it becomes configurable together with the production hostnames (AA-818). */
export const CIS_MEDIA_URL = DEV_CIS_API_URL;

/** CIS Platform → Staff: where Booking roles are granted (booking-* Cognito groups, ADR 0006a A1). Staff never sign
 *  in with Google (ADR 0006a A9), so the old NEXT_PUBLIC_GOOGLE_SIGNIN_URL placeholder is gone (AA-680). */
export const CIS_STAFF_PATH = "/admin/platform/staff";
export const CIS_STAFF_URL = `${CIS_ADMIN_URL}${CIS_STAFF_PATH}`;

/** Upstream request timeout for BFF calls, in milliseconds. */
export const UPSTREAM_TIMEOUT_MS = 10_000;
