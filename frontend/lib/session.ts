// lib/session.ts
// AA-683 — the staff session held by the BFF (UI spec §4.1, ADR 0004 O1).
//
// Two httpOnly cookies on the Booking origin; the browser never sees either token:
//   bk_cis_session  the CIS staff session token (POST /auth/admin-login, HS256, 24 h). Used ONLY to mint.
//   bk_access       the Booking JWT (POST /auth/booking-token, RS256, aud=booking, ~15 min).
// The Booking token is re-minted from the CIS token when it is missing or has under 2 minutes left.
// `exp` is read WITHOUT verifying the signature — only to decide when to refresh. The Booking API
// verifies every token against the CIS JWKS; a forged cookie gets a 401 there, never data.

import { cisApiUrl, UPSTREAM_TIMEOUT_MS } from "./config";

export const CIS_SESSION_COOKIE = "bk_cis_session";
export const ACCESS_COOKIE = "bk_access";
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

/** The `exp` claim (seconds) of a JWT, or null when it cannot be read. Does NOT verify the signature. */
export function jwtExp(token: string | undefined | null): number | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(base64UrlDecode(parts[1])) as { exp?: unknown };
    return typeof payload.exp === "number" ? payload.exp : null;
  } catch {
    return null;
  }
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
