// app/api/auth/session/route.ts
// AA-996 — GET: facts about the caller's own staff session for Settings → Sign-in & security (lib/sessionFacts.ts):
// the kind of sign-in and its times. Lives under /api so it receives the path-scoped Cognito refresh cookie (it reads
// only the session end in front of the "~", never the token). Returns no token and no claim other than times.

import { NextRequest, NextResponse } from "next/server";
import { ACCESS_COOKIE, CIS_SESSION_COOKIE, STAFF_REFRESH_COOKIE } from "@/lib/session";
import { sessionFacts } from "@/lib/sessionFacts";

export const dynamic = "force-dynamic";

export function GET(request: NextRequest) {
  const facts = sessionFacts({
    access: request.cookies.get(ACCESS_COOKIE)?.value,
    refresh: request.cookies.get(STAFF_REFRESH_COOKIE)?.value,
    cisSession: request.cookies.get(CIS_SESSION_COOKIE)?.value,
  });
  const res = NextResponse.json(facts, { status: facts.kind === "none" ? 401 : 200 });
  res.headers.set("Cache-Control", "no-store");
  return res;
}
