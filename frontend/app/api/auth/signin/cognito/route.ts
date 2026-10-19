// app/api/auth/signin/cognito/route.ts
// AA-680 — GET: starts the company e-mail sign-in (authorization code + PKCE, booking-staff client) and redirects
// to the managed login on staff-auth.lumiguides.it.com. `?next=` is kept (same-origin paths only, lib/safeNext).
// A host that is not on the allowlist (a Vercel preview) or a missing client secret → back to /login, where the
// legacy CIS password form is shown instead.

import { NextRequest, NextResponse } from "next/server";
import { allowedOriginForRequest, cognitoConfigured, startSignIn } from "@/lib/cognito";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const origin = allowedOriginForRequest(request);
  if (!origin || !cognitoConfigured()) {
    const url = new URL("/login", request.url);
    url.searchParams.set("reason", "cognito_unavailable");
    return NextResponse.redirect(url, { status: 303 });
  }
  const { authorizeUrl, setTxCookie } = await startSignIn(origin, request.nextUrl.searchParams.get("next") ?? "/");
  const response = NextResponse.redirect(authorizeUrl, { status: 303 });
  setTxCookie(response);
  response.headers.set("Cache-Control", "no-store");
  return response;
}
