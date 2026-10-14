// app/api/auth/logout/route.ts
// AA-683 — end the Booking session: both httpOnly cookies are cleared server-side (client JS cannot).
// The CIS session itself is not revoked (CIS has no revoke endpoint); it simply stops being held here.

import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, CIS_SESSION_COOKIE, cookieOptions } from "@/lib/session";
import { isSameOrigin } from "@/lib/sameOrigin";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  // Another site must not be able to sign staff out (logout CSRF).
  if (!isSameOrigin(req, { strict: true })) {
    return NextResponse.json({ ok: false, message: "Cross-site sign-out refused." }, { status: 403 });
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set(CIS_SESSION_COOKIE, "", cookieOptions(0));
  response.cookies.set(ACCESS_COOKIE, "", cookieOptions(0));
  return response;
}
