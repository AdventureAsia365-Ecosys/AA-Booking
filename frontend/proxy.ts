// proxy.ts (Next 16's name for middleware.ts)
// AA-683 — every page except /login needs a staff session (allow-list by default, CIS AA-252 lesson).
//
// Company e-mail session (AA-680, bk_access holds a Cognito access token):
// - token fresh → through; under 2 minutes left or expired → /api/auth/refresh?next=<path> (the refresh cookie is
//   scoped to /api, so the refresh happens there; this file never sees it). The bk_access cookie lives as long as
//   the 12 h session, so an expired token still tells this proxy which path to take.
// No company e-mail session → /login?next=<path>. AA-911 (S271): the legacy CIS password session (bk_cis_session)
// is retired with the CIS -> Booking bridge; a browser still holding that cookie is sent to /login with it cleared.
// /api/* is not matched: the BFF route handlers answer 401 JSON themselves (lib/session.ts).
// Role checks per page happen in the shell (lib/rbac.ts); the Booking API enforces its own matrix.
// AA-1111 (S284, ZAP baseline): every response this proxy handles carries the full CSP with a fresh nonce
// (lib/securityHeaders.ts), also set on the request so Next puts the nonce on its scripts (app/layout.tsx reads x-nonce
// for the restore scripts). The proxy now runs on /robots.txt, /favicon.ico and /assets/* too (they were outside the
// matcher): they pass without the session gate, as before.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import {
  ACCESS_COOKIE,
  CIS_SESSION_COOKIE,
  REFRESH_PATH,
  clearSessionCookies,
  isCognitoAccess,
  needsRefresh,
} from "./lib/session";
import { NONCE_HEADER, contentSecurityPolicy, newNonce, sentryOrigin } from "./lib/securityHeaders";

const PUBLIC_PATHS = ["/login"];

// The paths the matcher used to leave out (besides /api and the build chunks): static files, no session gate.
function isStaticFile(pathname: string): boolean {
  return pathname === "/favicon.ico" || pathname === "/robots.txt" || pathname.startsWith("/assets/");
}

// The browser posts Sentry events to the DSN's origin (a public build-time value; none = no Sentry).
const SENTRY_ORIGIN = sentryOrigin(process.env.NEXT_PUBLIC_SENTRY_DSN);

function toLogin(request: NextRequest, clear: boolean, reason?: string) {
  const url = new URL("/login", request.url);
  const next = request.nextUrl.pathname + request.nextUrl.search;
  if (next && next !== "/") url.searchParams.set("next", next);
  if (reason) url.searchParams.set("reason", reason);
  const res = NextResponse.redirect(url);
  if (clear) clearSessionCookies(res);
  return res;
}

function toRefresh(request: NextRequest) {
  const url = new URL(REFRESH_PATH, request.url);
  url.searchParams.set("next", request.nextUrl.pathname + request.nextUrl.search);
  return NextResponse.redirect(url);
}

export function proxy(request: NextRequest) {
  const nonce = newNonce();
  const csp = contentSecurityPolicy({
    nonce,
    dev: process.env.NODE_ENV === "development",
    connectSrc: [SENTRY_ORIGIN],
  });
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set(NONCE_HEADER, nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const res = gate(request, () => NextResponse.next({ request: { headers: requestHeaders } }));
  res.headers.set("Content-Security-Policy", csp);
  return res;
}

function gate(request: NextRequest, pass: () => NextResponse): NextResponse {
  const { pathname } = request.nextUrl;
  if (isStaticFile(pathname) || PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p + "/"))) {
    return pass();
  }

  const access = request.cookies.get(ACCESS_COOKIE)?.value;
  const legacy = request.cookies.get(CIS_SESSION_COOKIE)?.value;
  if (isCognitoAccess(access) && !legacy) {
    return needsRefresh(access) ? toRefresh(request) : pass();
  }
  // A retired bridge session (or a bridge token left in bk_access) ends here: clear it, sign in again.
  return legacy || access ? toLogin(request, true, "expired") : toLogin(request, false);
}

export const config = {
  // Everything except the BFF and the build chunks (AA-1111: static files now get the nonce CSP too, isStaticFile()).
  // /api/* stays out: a running proxy clones the request body and cuts it at 10 MiB; next.config.ts gives /api and
  // /_next the same policy without a nonce.
  matcher: ["/((?!api/|_next/static|_next/image).*)"],
};
