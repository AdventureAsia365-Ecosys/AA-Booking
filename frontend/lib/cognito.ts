// lib/cognito.ts
// AA-680 (Booking Phase 2) — staff sign-in with the company e-mail through the Cognito WORKFORCE pool:
// authorization code + PKCE (S256) against the confidential `booking-staff` app client, on the AA-branded managed
// login (staff-auth.lumiguides.it.com). Ported from AA-CIS-App frontend/lib/cognito.ts (AA-816 C4), booking-staff
// client only. SERVER ONLY: it reads the client secret. Route handlers import it; proxy.ts and client components never
// do (proxy only redirects to /api/auth/refresh).
//
// Security rules this file enforces (same as CIS):
//   - The redirect URI is always taken from a FIXED allowlist (the two production hosts, the one fixed Preview host
//     booking-preview.lumiguides.it.com, plus localhost:3001 in development), never built from the Host header. The
//     request host is only COMPARED with the allowlist; any other host (a per-deployment *.vercel.app preview URL)
//     gets no Cognito sign-in at all, because those hosts are not registered callback URLs (AA-CIS-Infra
//     identity_variables.tf). AA-911 (ADR 0006a A11): the Preview host is re-pointed at the preview under test by
//     the Vercel Deploy workflow (PR label `preview-signin` or the `alias_only_url` dispatch). A Vercel production
//     build accepts only the production hosts and a preview build only the Preview host (VERCEL_ENV).
//   - The Preview host never offers the legacy CIS password form (isPreviewSignInHost).
//   - state, nonce, the PKCE verifier and the post-login path live in one short-lived httpOnly cookie scoped to the
//     callback path; the callback checks state against it and the nonce against the ID token.
//   - Token exchange, refresh and revoke authenticate with HTTP Basic (client id + secret).
//   - Refresh is single-flight per refresh token on a warm instance, and a result is reused for a few seconds, so
//     parallel BFF calls never rotate the same refresh token several times.
//   - The Booking API verifies every access token (issuer, client_id, token_use, signature) and decides the role from
//     the booking-* groups; nothing here is an access decision.

import type { NextRequest, NextResponse } from "next/server";
import {
  ACCESS_COOKIE,
  STAFF_REFRESH_COOKIE,
  STAFF_REFRESH_COOKIE_PATH,
  cookieOptions,
  jwtPayload,
} from "./session";
import { safeNext } from "./safeNext";

// ── Configuration: public identifiers as code constants (optional env overrides, like the backend) ────────────────
// Only the client SECRET must be set in Vercel (COGNITO_BOOKING_STAFF_CLIENT_SECRET, server-only). Without it the
// company e-mail sign-in is simply not offered and the legacy CIS password form stays.
const DEFAULT_WORKFORCE_DOMAIN = "https://staff-auth.lumiguides.it.com";
const COGNITO_DOMAIN =
  (process.env.COGNITO_WORKFORCE_DOMAIN ?? "").trim().replace(/\/+$/, "") || DEFAULT_WORKFORCE_DOMAIN;
export const WORKFORCE_ISSUER =
  (process.env.COGNITO_WORKFORCE_ISSUER ?? "").trim() ||
  // Public by design (the browser sees it); the AA-902 resource-ID check skips the next line.
  "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS"; // docs-lint: ignore
const CLIENT_ID = (process.env.COGNITO_BOOKING_STAFF_CLIENT_ID ?? "").trim() || "5ifl9dgkr8a5outnmsjc8v1kt3";
const clientSecret = () => (process.env.COGNITO_BOOKING_STAFF_CLIENT_SECRET ?? "").trim();

export const CALLBACK_PATH = "/api/auth/callback/cognito";
/** Refresh-token lifetime of the booking-staff client (identity.tf): the session cap. */
const REFRESH_TTL_S = 12 * 60 * 60;

// Registered callback / logout origins of the booking-staff client (identity_variables.tf).
const PROD_ORIGINS = ["https://booking.lumiguides.it.com", "https://aa-booking-admin.vercel.app"];
// AA-911: the one fixed Preview host (Vercel domain on aa-booking-admin, aliased to the preview under test).
export const PREVIEW_ORIGIN = "https://booking-preview.lumiguides.it.com";
const DEV_ORIGIN = "http://localhost:3001";

/**
 * The origins this BUILD may run the Cognito flow on. A Vercel production build accepts only the production
 * hosts; a Vercel preview build only the fixed Preview host, so neither can run the other environment's callback.
 * Outside Vercel (VERCEL_ENV unset) a production build keeps the production hosts and `next dev` adds localhost.
 */
function origins(): string[] {
  const vercelEnv = (process.env.VERCEL_ENV ?? "").trim();
  if (vercelEnv === "production") return PROD_ORIGINS;
  if (vercelEnv === "preview") return [PREVIEW_ORIGIN];
  return process.env.NODE_ENV === "development" ? [...PROD_ORIGINS, PREVIEW_ORIGIN, DEV_ORIGIN] : PROD_ORIGINS;
}

/**
 * True when the request is served on the fixed Preview sign-in host. That host never offers the legacy CIS
 * password form or `POST /api/auth/login` (AA-911): a stable, possibly public URL must not expose a password form.
 */
export function isPreviewSignInHost(host: string | null | undefined): boolean {
  return !!host && host.trim().toLowerCase() === new URL(PREVIEW_ORIGIN).host;
}

/** Host of the managed-login page (shown on /login so staff know where the sign-in opens). */
export function workforceSignInHost(): string {
  try {
    return new URL(COGNITO_DOMAIN).host;
  } catch {
    return new URL(DEFAULT_WORKFORCE_DOMAIN).host;
  }
}

/** The allowlisted origin that matches this host, or null. The value returned is always a constant. */
export function allowedOriginForHost(host: string | null | undefined): string | null {
  if (!host) return null;
  const h = host.trim().toLowerCase();
  return origins().find((o) => new URL(o).host === h) ?? null;
}

export function allowedOriginForRequest(request: NextRequest): string | null {
  return allowedOriginForHost(request.headers.get("host") ?? request.nextUrl.host);
}

/** Company e-mail sign-in is offered only when the host is allowlisted and the client secret is set. */
export function cognitoSignInAvailable(host: string | null | undefined): boolean {
  return allowedOriginForHost(host) !== null && clientSecret() !== "";
}

export function cognitoConfigured(): boolean {
  return clientSecret() !== "";
}

// ── Small crypto helpers (Web Crypto: Node and edge) ──────────────────────────────────────────────────────────
function base64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomToken(byteLength = 32): string {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

async function pkceChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Length-safe string compare that does not short-circuit on the first differing character. */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function basicAuth(): string {
  // RFC 6749 §2.3.1: form-urlencode id and secret before base64 (a no-op for Cognito's alphabet).
  return `Basic ${btoa(`${encodeURIComponent(CLIENT_ID)}:${encodeURIComponent(clientSecret())}`)}`;
}

function secureCookies(): boolean {
  return process.env.NODE_ENV === "production";
}

// ── Authorization request ─────────────────────────────────────────────────────────────────────────────────────
export const OAUTH_TX_COOKIE = "bk_oauth_tx";
const OAUTH_TX_PATH = "/api/auth/callback";
// Cognito's auth session is 3 min (auth_session_validity); allow a little more for the round trip.
const OAUTH_TX_TTL_S = 5 * 60;

interface OAuthTx {
  s: string; // state
  n: string; // nonce
  v: string; // PKCE code verifier
  x: string; // post-login path (safeNext)
}

/** The Cognito /oauth2/authorize URL and a setter that stores the transaction cookie on the redirect. */
export async function startSignIn(
  origin: string,
  next: string,
): Promise<{ authorizeUrl: string; setTxCookie: (response: NextResponse) => void }> {
  const tx: OAuthTx = { s: randomToken(24), n: randomToken(24), v: randomToken(48), x: safeNext(next) };
  const params = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: origin + CALLBACK_PATH,
    scope: "openid email profile",
    state: tx.s,
    nonce: tx.n,
    code_challenge: await pkceChallenge(tx.v),
    code_challenge_method: "S256",
  });
  const value = base64url(new TextEncoder().encode(JSON.stringify(tx)));
  return {
    authorizeUrl: `${COGNITO_DOMAIN}/oauth2/authorize?${params.toString()}`,
    setTxCookie: (response) =>
      response.cookies.set(OAUTH_TX_COOKIE, value, {
        httpOnly: true,
        secure: secureCookies(),
        sameSite: "lax", // the return from Cognito is a top-level GET, which carries lax cookies
        path: OAUTH_TX_PATH,
        maxAge: OAUTH_TX_TTL_S,
      }),
  };
}

export function clearOAuthTx(response: NextResponse): void {
  response.cookies.set(OAUTH_TX_COOKIE, "", { path: OAUTH_TX_PATH, maxAge: 0 });
}

function readOAuthTx(request: NextRequest): OAuthTx | null {
  const raw = request.cookies.get(OAUTH_TX_COOKIE)?.value;
  if (!raw) return null;
  try {
    const bin = atob(raw.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(raw.length / 4) * 4, "="));
    const tx = JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
    if (tx && typeof tx.s === "string" && typeof tx.n === "string" && typeof tx.v === "string" && typeof tx.x === "string") {
      return tx as OAuthTx;
    }
  } catch {
    /* fall through */
  }
  return null;
}

// ── Token endpoint ────────────────────────────────────────────────────────────────────────────────────────────
export interface StaffTokens {
  accessToken: string;
  /** Present on the code exchange; on refresh only when rotation returns a new one. */
  refreshToken?: string;
  idToken?: string;
}

export class CognitoAuthError extends Error {
  constructor(public readonly code: string) {
    super(code);
  }
}

async function tokenRequest(body: URLSearchParams): Promise<StaffTokens> {
  if (!clientSecret()) throw new CognitoAuthError("not_configured");
  let res: Response;
  try {
    res = await fetch(`${COGNITO_DOMAIN}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Authorization: basicAuth() },
      body: body.toString(),
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
  } catch {
    throw new CognitoAuthError("unreachable");
  }
  // A Cognito-side error says nothing about the grant: treat it like no answer (callers keep the session).
  if (res.status >= 500) throw new CognitoAuthError("unreachable");
  if (!res.ok) {
    // Cognito answers {"error":"invalid_grant"} for a used/expired code or a revoked refresh token.
    const err = (await res.json().catch(() => ({}))) as { error?: string };
    throw new CognitoAuthError(err.error === "invalid_grant" ? "invalid_grant" : "token_error");
  }
  const data = (await res.json()) as { access_token?: string; refresh_token?: string; id_token?: string };
  if (!data.access_token) throw new CognitoAuthError("token_error");
  return { accessToken: data.access_token, refreshToken: data.refresh_token, idToken: data.id_token };
}

function checkAccessToken(token: string): void {
  const access = jwtPayload(token);
  if (!access || access.token_use !== "access" || access.client_id !== CLIENT_ID || access.iss !== WORKFORCE_ISSUER) {
    throw new CognitoAuthError("token_error");
  }
}

/**
 * Callback half of the flow: checks the transaction cookie (state), exchanges the code with the PKCE verifier,
 * checks the ID token's nonce / aud / iss and the access token's client_id / token_use. The ID token came straight
 * from the token endpoint over TLS with client authentication, so its claims are trusted without a signature check
 * (OIDC Core §3.1.3.7); the access token itself is verified by the Booking API. Returns the tokens and the
 * post-login path stored at sign-in.
 */
export async function completeSignIn(
  request: NextRequest,
  origin: string,
): Promise<{ tokens: StaffTokens; next: string }> {
  const params = request.nextUrl.searchParams;
  if (params.get("error")) throw new CognitoAuthError("denied");
  const code = params.get("code");
  const state = params.get("state");
  const tx = readOAuthTx(request);
  if (!code || !state || !tx || !timingSafeEqual(tx.s, state)) throw new CognitoAuthError("state");

  const tokens = await tokenRequest(
    new URLSearchParams({
      grant_type: "authorization_code",
      client_id: CLIENT_ID,
      code,
      redirect_uri: origin + CALLBACK_PATH,
      code_verifier: tx.v,
    }),
  );
  const id = tokens.idToken ? jwtPayload(tokens.idToken) : null;
  if (!id || typeof id.nonce !== "string" || !timingSafeEqual(id.nonce, tx.n)) throw new CognitoAuthError("nonce");
  if (id.aud !== CLIENT_ID || id.iss !== WORKFORCE_ISSUER) throw new CognitoAuthError("token_error");
  checkAccessToken(tokens.accessToken);
  if (!tokens.refreshToken) throw new CognitoAuthError("token_error");
  return { tokens, next: safeNext(tx.x) };
}

// ── Refresh (single-flight) ───────────────────────────────────────────────────────────────────────────────────
const inflight = new Map<string, Promise<StaffTokens>>();
const recent = new Map<string, { tokens: StaffTokens; until: number }>();
// Reuse one refresh result for this long: covers requests that arrive just after the first one resolved (they
// still carry the old refresh token). Well inside Cognito's 60 s rotation grace.
const RECENT_TTL_MS = 20_000;

export function refreshStaffTokens(refreshToken: string): Promise<StaffTokens> {
  const now = Date.now();
  for (const [k, v] of recent) if (v.until <= now) recent.delete(k);
  const hit = recent.get(refreshToken);
  if (hit) return Promise.resolve(hit.tokens);
  const running = inflight.get(refreshToken);
  if (running) return running;

  const p = tokenRequest(
    new URLSearchParams({ grant_type: "refresh_token", client_id: CLIENT_ID, refresh_token: refreshToken }),
  )
    .then((tokens) => {
      checkAccessToken(tokens.accessToken);
      recent.set(refreshToken, { tokens, until: Date.now() + RECENT_TTL_MS });
      return tokens;
    })
    .finally(() => inflight.delete(refreshToken));
  inflight.set(refreshToken, p);
  return p;
}

// ── Revoke + logout ───────────────────────────────────────────────────────────────────────────────────────────
/** Revokes a refresh token (and the access tokens issued from it). Best effort: never throws. */
export async function revokeRefreshToken(refreshToken: string): Promise<boolean> {
  if (!clientSecret()) return false;
  try {
    const res = await fetch(`${COGNITO_DOMAIN}/oauth2/revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Authorization: basicAuth() },
      body: new URLSearchParams({ token: refreshToken, client_id: CLIENT_ID }).toString(),
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** Cognito /logout URL that clears the managed-login session and returns to <origin>/login (registered). */
export function cognitoLogoutUrl(origin: string): string {
  const params = new URLSearchParams({ client_id: CLIENT_ID, logout_uri: `${origin}/login` });
  return `${COGNITO_DOMAIN}/logout?${params.toString()}`;
}

// ── Session cookies ───────────────────────────────────────────────────────────────────────────────────────────
// bk_staff_rt value: "<sessionEndEpochS>~<refreshToken>". The session end is fixed at sign-in (rotation does not
// extend the refresh window, ADR 0006 §7), so a rotated cookie keeps the original end.
export interface StaffRefreshCookie {
  sessionEnd: number;
  refreshToken: string;
}

export function readRefreshCookie(request: NextRequest): StaffRefreshCookie | null {
  const raw = request.cookies.get(STAFF_REFRESH_COOKIE)?.value;
  if (!raw) return null;
  const sep = raw.indexOf("~");
  if (sep < 0) return null;
  const sessionEnd = Number(raw.slice(0, sep));
  const refreshToken = raw.slice(sep + 1);
  if (!Number.isFinite(sessionEnd) || !refreshToken || sessionEnd <= Date.now() / 1000) return null;
  return { sessionEnd, refreshToken };
}

/**
 * Writes the Cognito session: bk_access holds the 15-min access token but the COOKIE lives as long as the refresh
 * window (proxy.ts must still see an expired token to send the page through the refresh route); bk_staff_rt holds
 * the refresh token. An expired access token in the cookie is useless to anyone: the Booking API rejects it.
 */
export function setStaffSessionCookies(
  response: NextResponse,
  tokens: StaffTokens,
  sessionEnd?: number,
  previousRefreshToken?: string,
): void {
  const nowS = Math.floor(Date.now() / 1000);
  const end = sessionEnd ?? nowS + REFRESH_TTL_S;
  const maxAge = Math.max(0, end - nowS);
  response.cookies.set(ACCESS_COOKIE, tokens.accessToken, cookieOptions(maxAge));
  const refreshToken = tokens.refreshToken ?? previousRefreshToken;
  if (refreshToken) {
    response.cookies.set(STAFF_REFRESH_COOKIE, `${end}~${refreshToken}`, {
      ...cookieOptions(maxAge),
      path: STAFF_REFRESH_COOKIE_PATH,
    });
  }
}
