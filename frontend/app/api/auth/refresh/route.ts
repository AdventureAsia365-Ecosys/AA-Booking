// app/api/auth/refresh/route.ts
// AA-680 — GET ?next=/path: proxy.ts sends a page navigation here when the Cognito access token in bk_access has
// under 2 minutes left (the refresh cookie is path-scoped to /api, so only this route and the BFF can use it).
//   refreshed            → rotated cookies (session end unchanged), redirect to `next`;
//   Cognito unreachable  → session kept, /login?reason=unavailable (going back to `next` would loop here);
//   anything else        → session cleared (revoked / expired refresh token), /login?reason=expired.

import { NextRequest, NextResponse } from "next/server";
import { CognitoAuthError, readRefreshCookie, refreshStaffTokens, setStaffSessionCookies } from "@/lib/cognito";
import { ACCESS_COOKIE, clearSessionCookies, needsRefresh } from "@/lib/session";
import { safeNext } from "@/lib/safeNext";

export const dynamic = "force-dynamic";

function redirect(request: NextRequest, path: string): NextResponse {
  const response = NextResponse.redirect(new URL(path, request.url), { status: 303 });
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export async function GET(request: NextRequest) {
  const next = safeNext(request.nextUrl.searchParams.get("next"));
  const rt = readRefreshCookie(request);
  const current = request.cookies.get(ACCESS_COOKIE)?.value;
  if (!rt) {
    const response = redirect(request, `/login?reason=expired&next=${encodeURIComponent(next)}`);
    clearSessionCookies(response);
    return response;
  }
  if (current && !needsRefresh(current)) return redirect(request, next);

  try {
    const tokens = await refreshStaffTokens(rt.refreshToken);
    const response = redirect(request, next);
    setStaffSessionCookies(response, tokens, rt.sessionEnd, rt.refreshToken);
    return response;
  } catch (e) {
    const code = e instanceof CognitoAuthError ? e.code : "token_error";
    console.warn(`[auth] cognito refresh refused: reason=${code}`);
    if (code === "unreachable") return redirect(request, `/login?reason=unavailable&next=${encodeURIComponent(next)}`);
    const response = redirect(request, `/login?reason=expired&next=${encodeURIComponent(next)}`);
    clearSessionCookies(response);
    return response;
  }
}
