// proxy.ts (Next 16's name for middleware.ts)
// AA-683 — every page except /login needs a staff session (allow-list by default, CIS AA-252 lesson).
//
// Company e-mail session (AA-680, bk_access holds a Cognito access token):
// - token fresh → through; under 2 minutes left or expired → /api/auth/refresh?next=<path> (the refresh cookie is
//   scoped to /api, so the refresh happens there; this file never sees it). The bk_access cookie lives as long as
//   the 12 h session, so an expired token still tells this proxy which path to take.
// Legacy CIS password session (bk_cis_session, until AA-680 PR-4):
// - The Booking token is re-minted when missing or under 2 minutes from expiry; the fresh cookie is set
//   on the response AND forwarded on the request, so the server render in this same request uses it.
// Neither → /login?next=<path>.
// - CIS refuses the mint (session expired, user deactivated in CIS) → both cookies cleared → /login.
// - CIS unreachable → the request goes on with the old token; the (staff) layout shows the outage
//   instead of logging the user out because of a network blip.
// /api/* is not matched: the BFF route handlers answer 401 JSON themselves (lib/session.ts).
// Role checks per page happen in the shell (lib/rbac.ts); the Booking API enforces its own matrix.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import {
  ACCESS_COOKIE,
  CIS_SESSION_COOKIE,
  REFRESH_PATH,
  clearSessionCookies,
  cookieOptions,
  isCognitoAccess,
  mintBookingToken,
  needsRefresh,
} from "./lib/session";

const PUBLIC_PATHS = ["/login"];

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

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p + "/"))) {
    return NextResponse.next();
  }

  const access = request.cookies.get(ACCESS_COOKIE)?.value;
  const cisSession = request.cookies.get(CIS_SESSION_COOKIE)?.value;
  if (isCognitoAccess(access) && !cisSession) {
    return needsRefresh(access) ? toRefresh(request) : NextResponse.next();
  }
  if (!cisSession) return toLogin(request, false);

  if (!needsRefresh(access)) return NextResponse.next();

  const minted = await mintBookingToken(cisSession);
  if (!minted.ok) {
    if (minted.reason === "unauthorized") return toLogin(request, true, "expired");
    return NextResponse.next();
  }
  request.cookies.set(ACCESS_COOKIE, minted.token);
  const res = NextResponse.next({ request: { headers: request.headers } });
  res.cookies.set(ACCESS_COOKIE, minted.token, cookieOptions(minted.expiresIn));
  return res;
}

export const config = {
  // Everything except the BFF, Next internals and static files.
  matcher: ["/((?!api/|_next/static|_next/image|assets/|favicon.ico|robots.txt).*)"],
};
