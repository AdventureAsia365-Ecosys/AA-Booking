// app/api/auth/logout/route.ts
// AA-683 / AA-680 — end the Booking session: every session cookie is cleared server-side (client JS cannot).
// - Company e-mail session (Cognito): the refresh token is revoked (best effort, which also invalidates the access
//   tokens issued from it) and the answer carries `logoutUrl`, the Cognito /logout URL that ends the managed-login
//   session and returns to <origin>/login (a registered logout URL). The browser navigates there (ui/signOut.ts).
// - Legacy CIS password session: the CIS session itself is not revoked (CIS has no revoke endpoint); it simply stops
//   being held here.
// - AA-1157: the member's open "view as a lower role" sessions are ended first (Booking POST /booking/view-as/end-all,
//   before the revoke invalidates the access token; also when the view cookie is already gone, so a view that ran out
//   gets its end row) and the view cookie is cleared.

import { NextRequest, NextResponse } from "next/server";
import { allowedOriginForRequest, cognitoLogoutUrl, readRefreshCookie, revokeRefreshToken } from "@/lib/cognito";
import { ACCESS_COOKIE, clearSessionCookies, isCognitoAccess } from "@/lib/session";
import { isSameOrigin } from "@/lib/sameOrigin";
import { bookingApiUrl } from "@/lib/config";
import { clearViewAsCookie } from "@/lib/viewAsCookie";

const VIEW_AS_END_TIMEOUT_MS = 4000;

/** AA-1157: end the member's open views (one end row each) before the refresh token is revoked. Best effort. */
async function endViews(access: string | undefined): Promise<void> {
  if (!access) return;
  try {
    await fetch(`${bookingApiUrl()}/booking/view-as/end-all`, {
      method: "POST",
      headers: { Authorization: `Bearer ${access}`, Accept: "application/json" },
      signal: AbortSignal.timeout(VIEW_AS_END_TIMEOUT_MS),
      cache: "no-store",
    });
  } catch {
    console.warn("[auth] logout: ending the open view-as sessions failed");
  }
}

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  // Another site must not be able to sign staff out (logout CSRF).
  if (!isSameOrigin(req, { strict: true })) {
    return NextResponse.json({ ok: false, message: "Cross-site sign-out refused." }, { status: 403 });
  }
  const rt = readRefreshCookie(req);
  const cognito = rt !== null || isCognitoAccess(req.cookies.get(ACCESS_COOKIE)?.value);
  if (cognito) await endViews(req.cookies.get(ACCESS_COOKIE)?.value);
  if (rt) await revokeRefreshToken(rt.refreshToken);
  const origin = cognito ? allowedOriginForRequest(req) : null;
  const response = NextResponse.json({ ok: true, logoutUrl: origin ? cognitoLogoutUrl(origin) : null });
  clearSessionCookies(response);
  return clearViewAsCookie(response);
}
