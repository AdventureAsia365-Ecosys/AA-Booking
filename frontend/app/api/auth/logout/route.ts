// app/api/auth/logout/route.ts
// AA-683 / AA-680 — end the Booking session: every session cookie is cleared server-side (client JS cannot).
// - Company e-mail session (Cognito): the refresh token is revoked (best effort, which also invalidates the access
//   tokens issued from it) and the answer carries `logoutUrl`, the Cognito /logout URL that ends the managed-login
//   session and returns to <origin>/login (a registered logout URL). The browser navigates there (ui/signOut.ts).
// - A leftover cookie of the retired CIS password bridge (AA-911) is cleared too.

import { NextRequest, NextResponse } from "next/server";
import { allowedOriginForRequest, cognitoLogoutUrl, readRefreshCookie, revokeRefreshToken } from "@/lib/cognito";
import { ACCESS_COOKIE, clearSessionCookies, isCognitoAccess } from "@/lib/session";
import { isSameOrigin } from "@/lib/sameOrigin";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  // Another site must not be able to sign staff out (logout CSRF).
  if (!isSameOrigin(req, { strict: true })) {
    return NextResponse.json({ ok: false, message: "Cross-site sign-out refused." }, { status: 403 });
  }
  const rt = readRefreshCookie(req);
  const cognito = rt !== null || isCognitoAccess(req.cookies.get(ACCESS_COOKIE)?.value);
  if (rt) await revokeRefreshToken(rt.refreshToken);
  const origin = cognito ? allowedOriginForRequest(req) : null;
  const response = NextResponse.json({ ok: true, logoutUrl: origin ? cognitoLogoutUrl(origin) : null });
  clearSessionCookies(response);
  return response;
}
