// lib/viewAs.ts — AA-1157 (root ADR 0019 d5 as amended): the pure parts of the owner's read-only "view as a lower role".
//
// Roles › "View as" (owners) → POST /api/view-as (app/api/view-as/route.ts) → Booking POST /booking/view-as (owner only,
// downward only, logged with the role's full capability list). The opaque session id it answers goes into the httpOnly
// cookie VIEW_AS_COOKIE (never readable by the page); the BFF proxy forwards it as VIEW_AS_HEADER on every read and
// refuses every write itself (403 view_as_read_only) while it is set. The API answers 409 view_as_ended when the view
// is over; the BFF then clears the cookie. The company sign-in cookies are never touched. Dependency-free.

export const VIEW_AS_COOKIE = "bk_view_as";
export const VIEW_AS_HEADER = "x-booking-view-as";
export const VIEW_AS_API = "/api/view-as";
export const VIEW_AS_CLEAR = "/api/view-as/clear";
export const VIEW_AS_ENDED = "view_as_ended";
export const VIEW_AS_READ_ONLY = "view_as_read_only";
export const MAX_MINUTES = 60;
export const MINUTE_CHOICES = [15, 30, 60] as const;
export const DEFAULT_MINUTES = 30;

const SESSION_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The view-as session id held in the cookie, or null when absent or not a session id. */
export function viewAsRef(cookie: string | undefined | null): string | null {
  return cookie && SESSION_RE.test(cookie) ? cookie : null;
}

/** The header that carries the view to the API (none outside a view). */
export function viewAsHeaders(cookie: string | undefined | null): Record<string, string> {
  const ref = viewAsRef(cookie);
  return ref ? { [VIEW_AS_HEADER]: ref } : {};
}

/** Does the BFF proxy refuse this request itself? Every write inside a view, and the start / end paths of the view
 *  (they go through /api/view-as, which keeps the session id in the httpOnly cookie). */
export function proxyRefusal(method: string, path: string, cookie: string | undefined | null): string | null {
  const read = method === "GET" || method === "HEAD";
  if (!read && viewAsRef(cookie)) return VIEW_AS_READ_ONLY;
  if (!read && /^view-as(\/|$)/i.test(path)) return "view_as_bff_only";
  return null;
}

/** Is an API answer the end of the view (the BFF clears the cookie)? */
export function isViewEnded(status: number, body: unknown): boolean {
  return status === 409 && !!body && typeof body === "object" && (body as { detail?: unknown }).detail === VIEW_AS_ENDED;
}

/** The cookie lifetime of a view: until its expiry, at most MAX_MINUTES, at least one second. */
export function cookieMaxAge(expiresAt: string | null | undefined, nowMs: number = Date.now()): number {
  const end = expiresAt ? Date.parse(expiresAt) : NaN;
  const left = Number.isFinite(end) ? Math.floor((end - nowMs) / 1000) : 0;
  return Math.min(MAX_MINUTES * 60, Math.max(1, left));
}

/** Whole minutes left (never below 0) for the banner. */
export function minutesLeft(expiresAt: string | null | undefined, nowMs: number = Date.now()): number {
  const end = expiresAt ? Date.parse(expiresAt) : NaN;
  return Number.isFinite(end) ? Math.max(0, Math.ceil((end - nowMs) / 60000)) : 0;
}
