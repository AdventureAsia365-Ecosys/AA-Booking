// lib/bffAccess.ts — AA-1157: the company sign-in access token for a BFF route other than the proxy, refreshed like
// app/api/booking/[...path]/route.ts does it (single-flight lib/cognito.ts; a missing or ending session answers 401,
// an unreachable sign-in 503). The rotated cookies are set on the route's answer.
import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, clearSessionCookies, needsRefresh } from "./session";
import { CognitoAuthError, readRefreshCookie, refreshStaffTokens, setStaffSessionCookies, type StaffTokens } from "./cognito";

function ended(status: number, detail: string): NextResponse {
  const res = NextResponse.json({ detail, message: "Your session has ended. Sign in again." }, { status });
  if (status === 401) clearSessionCookies(res);
  return res;
}

export async function withAccess(
  req: NextRequest,
  run: (access: string) => Promise<NextResponse>,
): Promise<NextResponse> {
  const rt = readRefreshCookie(req);
  if (!rt) return ended(401, "not_signed_in");
  let access = req.cookies.get(ACCESS_COOKIE)?.value ?? "";
  let rotated: StaffTokens | null = null;
  if (needsRefresh(access)) {
    try {
      rotated = await refreshStaffTokens(rt.refreshToken);
    } catch (e) {
      return e instanceof CognitoAuthError && e.code === "unreachable" ? ended(503, "auth_unavailable")
        : ended(401, "session_expired");
    }
    access = rotated.accessToken;
  }
  let out: NextResponse;
  try {
    out = await run(access);
  } catch {
    return NextResponse.json({ detail: "upstream_unavailable" }, { status: 502 });
  }
  if (rotated) setStaffSessionCookies(out, rotated, rt.sessionEnd, rt.refreshToken);
  return out;
}
