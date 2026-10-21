// app/api/auth/login/route.ts
// AA-683 — staff sign-in (UI spec §4.1, ADR 0004 O1, Nghiệp S236 decision 2).
//   1. CIS POST /auth/admin-login {username, password}      → CIS staff session token (24 h)
//   2. CIS POST /auth/booking-token (Bearer = CIS token)    → Booking JWT (RS256, aud=booking, ~15 min)
//   3. both tokens go into httpOnly cookies (lib/session.ts); neither is returned to the browser; a company e-mail
//      (Cognito) session cookie is cleared, so only one session kind is held at a time
// LEGACY (AA-680): kept as the fallback where company e-mail sign-in is not offered (previews, secret not set) and
// for the E2E account until AA-680 PR-4 retires the CIS bridge.
//   4. Booking GET /booking/staff/me tells the page whether the profile is active or pending activation
// Error copy never says which of the two fields was wrong. Cross-origin or non-JSON posts are refused (403).

import { NextRequest, NextResponse } from "next/server";
import { cisApiUrl, UPSTREAM_TIMEOUT_MS } from "@/lib/config";
import {
  ACCESS_COOKIE,
  CIS_SESSION_COOKIE,
  CIS_SESSION_MAX_AGE_S,
  cookieOptions,
  STAFF_REFRESH_COOKIE,
  STAFF_REFRESH_COOKIE_PATH,
  mintBookingToken,
} from "@/lib/session";
import { fetchStaffMe } from "@/lib/api/server";
import { isSameOrigin } from "@/lib/sameOrigin";
import { isPreviewSignInHost } from "@/lib/cognito";

export const dynamic = "force-dynamic";

function fail(message: string, status: number) {
  return NextResponse.json({ ok: false, message }, { status });
}

export async function POST(req: NextRequest) {
  // AA-911: the fixed Preview host has company sign-in only; the legacy password route does not exist there.
  if (isPreviewSignInHost(req.headers.get("host") ?? req.nextUrl.host)) {
    return new NextResponse(null, { status: 404 });
  }
  // Login CSRF guard: only this site's own form may sign the browser in, and only with a JSON body
  // (a cross-site HTML form can send text/plain that would still parse as JSON).
  if (!isSameOrigin(req, { strict: true }) || !req.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    return fail("Cross-site sign-in refused.", 403);
  }
  let username = "";
  let password = "";
  try {
    const body = (await req.json()) as { username?: unknown; password?: unknown };
    username = typeof body.username === "string" ? body.username.trim() : "";
    password = typeof body.password === "string" ? body.password : "";
  } catch {
    return fail("Enter your CIS username and password.", 400);
  }
  if (!username || !password || username.length > 200 || password.length > 500) {
    return fail("Enter your CIS username and password.", 400);
  }

  // 1. CIS staff login
  let cisToken = "";
  try {
    const res = await fetch(`${cisApiUrl()}/auth/admin-login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      cache: "no-store",
    });
    if (res.status === 401 || res.status === 403) {
      return fail("That username and password do not match a CIS staff account.", 401);
    }
    if (!res.ok) return fail("CIS sign-in is not answering. Try again in a moment.", 502);
    const data = (await res.json()) as { token?: unknown };
    cisToken = typeof data.token === "string" ? data.token : "";
  } catch {
    return fail("CIS sign-in is not answering. Try again in a moment.", 502);
  }
  if (!cisToken) return fail("CIS sign-in returned no session. Try again.", 502);

  // 2. Booking token
  const minted = await mintBookingToken(cisToken);
  if (!minted.ok) {
    if (minted.reason === "unauthorized") {
      return fail("This CIS account cannot open Booking (inactive or not a staff account).", 403);
    }
    if (minted.reason === "not_configured") {
      return fail("Booking sign-in is not configured on CIS yet (signing key missing).", 503);
    }
    return fail("CIS could not issue a Booking session. Try again in a moment.", 502);
  }

  // 4. Booking profile state (provisions the profile on the first sign-in)
  const me = await fetchStaffMe(minted.token);
  const response = NextResponse.json({
    ok: true,
    // "active" → go in; "pending" → the shell shows the pending-activation screen;
    // "unavailable" → signed in, but the Booking API did not answer; the shell explains it.
    state: me.kind === "ok" ? "active" : me.kind === "inactive" ? "pending" : "unavailable",
    name: me.kind === "ok" ? me.staff.name : null,
  });
  response.cookies.set(CIS_SESSION_COOKIE, cisToken, cookieOptions(CIS_SESSION_MAX_AGE_S));
  response.cookies.set(ACCESS_COOKIE, minted.token, cookieOptions(minted.expiresIn));
  response.cookies.set(STAFF_REFRESH_COOKIE, "", { ...cookieOptions(0), path: STAFF_REFRESH_COOKIE_PATH });
  return response;
}
