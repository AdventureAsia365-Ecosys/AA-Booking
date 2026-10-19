// lib/session.ts
// AA-683 / AA-680 — the staff session held by the BFF (UI spec §4.1, root ADR 0006 / 0006a).
//
// Two kinds of session, both in httpOnly cookies on the Booking origin; the browser never sees a token:
//
// Company e-mail (Cognito WORKFORCE, booking-staff client; AA-680 Phase 2, lib/cognito.ts):
//   bk_access     the Cognito access token (15 min). The COOKIE lives as long as the refresh window, so proxy.ts
//                 still sees an expired token and sends the page through /api/auth/refresh.
//   bk_staff_rt   "<sessionEndEpochS>~<refreshToken>", path /api: only the auth routes and the BFF proxy
//                 (/api/booking) receive it, never a page request. Session cap 12 h (the client's refresh TTL).
//
// CIS password bridge (legacy, until AA-680 PR-4; ADR 0004 O1):
//   bk_cis_session  the CIS staff session token (POST /auth/admin-login, HS256, 24 h). Used ONLY to mint.
//   bk_access       the Booking JWT (POST /auth/booking-token, RS256, aud=booking, ~15 min), re-minted from the CIS
//                   token when it is missing or has under 2 minutes left.
//
// Token payloads are read WITHOUT verifying the signature — only to decide when to refresh and which kind of
// session this is. The Booking API verifies every token; a forged cookie gets a 401 there, never data.

import { cisApiUrl, UPSTREAM_TIMEOUT_MS } from "./config";

export const CIS_SESSION_COOKIE = "bk_cis_session";
export const ACCESS_COOKIE = "bk_access";
/** Cognito refresh cookie: "<sessionEndEpochS>~<refreshToken>". Path /api (auth routes + BFF proxy only). */
export const STAFF_REFRESH_COOKIE = "bk_staff_rt";
export const STAFF_REFRESH_COOKIE_PATH = "/api";
export const REFRESH_PATH = "/api/auth/refresh";
export const CIS_SESSION_MAX_AGE_S = 60 * 60 * 24; // matches the CIS JWT_EXPIRY_H (24 h)
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

/** True for a Cognito access token (token_use=access + client_id); the bridge JWT has neither claim. */
export function isCognitoAccess(token: string | undefined | null): boolean {
  const p = jwtPayload(token);
  return !!p && p.token_use === "access" && typeof p.client_id === "string" && p.client_id.length > 0;
}

/** Clears every staff session cookie (both kinds) on a response. */
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

export type MintResult =
  | { ok: true; token: string; expiresIn: number }
  | { ok: false; reason: "unauthorized" | "not_configured" | "unavailable" };

/** Swap the CIS staff session for a Booking JWT (CIS POST /auth/booking-token, Bearer = CIS token). */
export async function mintBookingToken(cisToken: string): Promise<MintResult> {
  let res: Response;
  try {
    res = await fetch(`${cisApiUrl()}/auth/booking-token`, {
      method: "POST",
      headers: { Authorization: `Bearer ${cisToken}` },
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      cache: "no-store",
    });
  } catch {
    return { ok: false, reason: "unavailable" };
  }
  if (res.status === 401 || res.status === 403) return { ok: false, reason: "unauthorized" };
  if (res.status === 503) return { ok: false, reason: "not_configured" };
  if (!res.ok) return { ok: false, reason: "unavailable" };
  try {
    const body = (await res.json()) as { access_token?: unknown; expires_in?: unknown };
    if (typeof body.access_token !== "string" || !body.access_token) return { ok: false, reason: "unavailable" };
    const expiresIn =
      typeof body.expires_in === "number" && body.expires_in > 0
        ? body.expires_in
        : (jwtExp(body.access_token) ?? Date.now() / 1000) - Date.now() / 1000;
    return { ok: true, token: body.access_token, expiresIn };
  } catch {
    return { ok: false, reason: "unavailable" };
  }
}
