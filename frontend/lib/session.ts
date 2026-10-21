// lib/session.ts
// AA-683 / AA-680 — the staff session held by the BFF (UI spec §4.1, root ADR 0006 / 0006a).
//
// One kind of session, in httpOnly cookies on the Booking origin; the browser never sees a token.
//
// Company e-mail (Cognito WORKFORCE, booking-staff client; AA-680 Phase 2, lib/cognito.ts):
//   bk_access     the Cognito access token (15 min). The COOKIE lives as long as the refresh window, so proxy.ts
//                 still sees an expired token and sends the page through /api/auth/refresh.
//   bk_staff_rt   "<sessionEndEpochS>~<refreshToken>", path /api: only the auth routes and the BFF proxy
//                 (/api/booking) receive it, never a page request. Session cap 12 h (the client's refresh TTL).
//
// The CIS password bridge (bk_cis_session + a CIS-minted Booking JWT) is retired (AA-911, root ADR 0006a A11).
// Its cookie name is kept only so sign-in and sign-out can still CLEAR a cookie left in a browser.
//
// Token payloads are read WITHOUT verifying the signature — only to decide when to refresh. The Booking API verifies
// every token; a forged cookie gets a 401 there, never data.

/** The retired bridge cookie: never set any more, only cleared. */
export const CIS_SESSION_COOKIE = "bk_cis_session";
export const ACCESS_COOKIE = "bk_access";
/** Cognito refresh cookie: "<sessionEndEpochS>~<refreshToken>". Path /api (auth routes + BFF proxy only). */
export const STAFF_REFRESH_COOKIE = "bk_staff_rt";
export const STAFF_REFRESH_COOKIE_PATH = "/api";
export const REFRESH_PATH = "/api/auth/refresh";
export const REFRESH_MARGIN_S = 120;

export interface CookieOptions {
  httpOnly: true;
  secure: boolean;
  sameSite: "lax";
  path: "/";
  maxAge: number;
}

export function cookieOptions(maxAgeS: number): CookieOptions {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: Math.max(0, Math.floor(maxAgeS)),
  };
}

function base64UrlDecode(part: string): string {
  const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
  return atob(padded);
}

/** The JWT payload, decoded WITHOUT checking the signature, or null when it is not a JWT. */
export function jwtPayload(token: string | undefined | null): Record<string, unknown> | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(base64UrlDecode(parts[1])) as unknown;
    return payload && typeof payload === "object" ? (payload as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** The `exp` claim (seconds) of a JWT, or null when it cannot be read. Does NOT verify the signature. */
export function jwtExp(token: string | undefined | null): number | null {
  const exp = jwtPayload(token)?.exp;
  return typeof exp === "number" ? exp : null;
}

/** True for a Cognito access token (token_use=access + client_id). */
export function isCognitoAccess(token: string | undefined | null): boolean {
  const p = jwtPayload(token);
  return !!p && p.token_use === "access" && typeof p.client_id === "string" && p.client_id.length > 0;
}

/** Clears every staff session cookie (and the retired bridge cookie) on a response. */
export function clearSessionCookies(res: { cookies: { set: (name: string, value: string, opts: object) => unknown } }) {
  res.cookies.set(CIS_SESSION_COOKIE, "", cookieOptions(0));
  res.cookies.set(ACCESS_COOKIE, "", cookieOptions(0));
  res.cookies.set(STAFF_REFRESH_COOKIE, "", { ...cookieOptions(0), path: STAFF_REFRESH_COOKIE_PATH });
}

/** True when the Booking token is missing, unreadable or expires within REFRESH_MARGIN_S. */
export function needsRefresh(token: string | undefined | null, nowS: number = Date.now() / 1000): boolean {
  const exp = jwtExp(token);
  return exp === null || exp - nowS < REFRESH_MARGIN_S;
}
