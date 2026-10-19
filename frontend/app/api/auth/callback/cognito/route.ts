// app/api/auth/callback/cognito/route.ts
// AA-680 — GET: the booking-staff client's registered callback (identity_variables.tf). Exchanges the code
// (lib/cognito.ts checks state, nonce, issuer, client), then asks the Booking API who this is
// (GET /booking/staff/me with the new access token: the API takes the role from the booking-* group and creates
// or finds the staff row by the Cognito identity, bk-016):
//   200                 → session cookies, legacy bridge cookie cleared, redirect to `next`;
//   403 staff_inactive  → session cookies too: the (staff) layout shows the "suspended / pending" screen;
//   403 no_booking_role → no session; the refresh token is revoked; /login?reason=no_booking_role;
//   other 403 / 401     → no session; /login?reason=forbidden | signin_failed;
//   API not answering   → session cookies (the layout shows the outage and offers "Try again").

import { NextRequest, NextResponse } from "next/server";
import {
  CognitoAuthError,
  allowedOriginForRequest,
  clearOAuthTx,
  completeSignIn,
  revokeRefreshToken,
  setStaffSessionCookies,
} from "@/lib/cognito";
import { fetchStaffMe } from "@/lib/api/server";
import { CIS_SESSION_COOKIE, clearSessionCookies, cookieOptions } from "@/lib/session";

export const dynamic = "force-dynamic";

function toLogin(request: NextRequest, reason: string): NextResponse {
  const url = new URL("/login", request.url);
  url.searchParams.set("reason", reason);
  const response = NextResponse.redirect(url, { status: 303 });
  clearOAuthTx(response);
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export async function GET(request: NextRequest) {
  const origin = allowedOriginForRequest(request);
  if (!origin) return toLogin(request, "cognito_unavailable");

  let signedIn;
  try {
    signedIn = await completeSignIn(request, origin);
  } catch (e) {
    const code = e instanceof CognitoAuthError ? e.code : "token_error";
    console.warn(`[auth] cognito callback refused: reason=${code}`);
    return toLogin(request, code === "denied" ? "denied" : code === "unreachable" ? "unavailable" : "signin_failed");
  }
  const { tokens, next } = signedIn;

  const me = await fetchStaffMe(tokens.accessToken);
  if (me.kind === "unauthorized" || me.kind === "forbidden") {
    const reason = me.kind === "forbidden" && me.code === "no_booking_role" ? "no_booking_role" : me.kind === "forbidden" ? "forbidden" : "signin_failed";
    console.warn(`[auth] cognito callback: Booking refused the sign-in (${me.kind === "forbidden" ? me.code : "401"})`);
    if (tokens.refreshToken) await revokeRefreshToken(tokens.refreshToken);
    const response = toLogin(request, reason);
    clearSessionCookies(response);
    return response;
  }

  const response = NextResponse.redirect(new URL(next, origin), { status: 303 });
  clearOAuthTx(response);
  // One session kind at a time: a company e-mail sign-in replaces a legacy CIS password session.
  response.cookies.set(CIS_SESSION_COOKIE, "", cookieOptions(0));
  setStaffSessionCookies(response, tokens);
  response.headers.set("Cache-Control", "no-store");
  return response;
}
