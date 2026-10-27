// lib/sessionFacts.ts
// AA-996 — what the BFF can say about the current staff session, for Settings → Sign-in & security (Figma SETTINGS-2 /
// RESET PASSWORD, replaced: Booking holds no password, ADR 0004 O1, root ADR 0006a). Computed from the session cookies
// only (lib/session.ts), payloads decoded WITHOUT checking a signature: these are display facts about the caller's
// own cookies, never an authorization decision (the Booking API verifies every token). No token or token part is
// ever returned, only the kind of session and its times.

import { jwtPayload } from "./session";

export type SessionKind = "company_email" | "cis_password" | "none";

export interface SessionFacts {
  kind: SessionKind;
  /** When the person proved who they are (Cognito `auth_time`, or the CIS session's `iat`), epoch seconds. */
  signed_in_at: number | null;
  /** When the current short-lived Booking access token expires (it renews on its own until the session ends). */
  access_expires_at: number | null;
  /** When this session ends and the person must sign in again (refresh window cap, or the CIS session expiry). */
  session_ends_at: number | null;
}

function claim(payload: Record<string, unknown> | null, key: string): number | null {
  const v = payload?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Session end of the Cognito refresh cookie "<sessionEndEpochS>~<refreshToken>" (lib/cognito.ts), or null. */
export function refreshCookieEnd(raw: string | undefined | null, nowS: number): number | null {
  if (!raw) return null;
  const sep = raw.indexOf("~");
  if (sep <= 0) return null;
  const end = Number(raw.slice(0, sep));
  return Number.isFinite(end) && end > nowS ? end : null;
}

export function sessionFacts(
  cookies: { access?: string | null; refresh?: string | null; cisSession?: string | null },
  nowS: number = Date.now() / 1000,
): SessionFacts {
  const access = jwtPayload(cookies.access);
  const cognito = !!access && access.token_use === "access" && typeof access.client_id === "string";
  if (cognito && !cookies.cisSession) {
    return {
      kind: "company_email",
      signed_in_at: claim(access, "auth_time") ?? claim(access, "iat"),
      access_expires_at: claim(access, "exp"),
      session_ends_at: refreshCookieEnd(cookies.refresh, nowS),
    };
  }
  const cis = jwtPayload(cookies.cisSession);
  if (cis) {
    return {
      kind: "cis_password",
      signed_in_at: claim(cis, "iat"),
      access_expires_at: claim(access, "exp"),
      session_ends_at: claim(cis, "exp"),
    };
  }
  return { kind: "none", signed_in_at: null, access_expires_at: null, session_ends_at: null };
}
