// app/api/view-as/route.ts — AA-1157 (root ADR 0019 d5 as amended): start, read and end the owner's read-only "view as
// a lower role" of this browser.
//
//   POST   { role, minutes?, reason? } → Booking POST /booking/view-as (owner only, downward only, logged). The session
//          id it answers goes into the httpOnly cookie bk_view_as (SameSite=Strict, at most 60 minutes) and is not
//          sent to the page.
//   GET    → Booking GET /booking/view-as with the cookie's session ({ active: false } outside a view; a view the API
//          ended clears the cookie). Without the cookie it still asks the API (no view header): the API then ends the
//          member's views that are past their expiry (their end rows), e.g. at the banner's check at 0 minutes.
//   DELETE → the "Back" button: Booking POST /booking/view-as/{id}/end WITHOUT the view header (as the owner), then the
//          cookie is cleared (also when the API call fails: the view then ends at its expiry).
// The company sign-in cookies are never touched, so the sign-in session is the same before and after. The access token
// is refreshed the way the BFF proxy does it (lib/bffAccess.ts).

import { NextRequest, NextResponse } from "next/server";
import { bookingApiUrl, UPSTREAM_TIMEOUT_MS } from "@/lib/config";
import { isSameOrigin } from "@/lib/sameOrigin";
import { withAccess } from "@/lib/bffAccess";
import { VIEW_AS_COOKIE, cookieMaxAge, isViewEnded, refusedWriteLog, viewAsHeaders, viewAsRef } from "@/lib/viewAs";
import { clearViewAsCookie, setViewAsCookie } from "@/lib/viewAsCookie";

export const dynamic = "force-dynamic";

function upstream(path: string, access: string, init: { method?: string; body?: string; view?: string | null } = {}) {
  return fetch(`${bookingApiUrl()}/booking/${path}`, {
    method: init.method ?? "GET",
    headers: {
      Authorization: `Bearer ${access}`,
      Accept: "application/json",
      ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...viewAsHeaders(init.view),
    },
    body: init.body,
    signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    cache: "no-store",
  });
}

function crossSite(req: NextRequest): NextResponse | null {
  return isSameOrigin(req) ? null : NextResponse.json({ detail: "bad_origin" }, { status: 403 });
}

/** The page's answer to a started view: the session without its id, which goes into the httpOnly cookie. */
function started(data: { session_id?: unknown; expires_at?: unknown }): NextResponse {
  const { session_id: sessionId, ...session } = data;
  const ref = viewAsRef(typeof sessionId === "string" ? sessionId : null);
  if (!ref) return NextResponse.json({ detail: "The view could not be opened" }, { status: 502 });
  const out = NextResponse.json({ active: true, ...session }, { status: 201 });
  return setViewAsCookie(out, ref, cookieMaxAge(typeof session.expires_at === "string" ? session.expires_at : null));
}

export async function POST(req: NextRequest) {
  const refused = crossSite(req);
  if (refused) return refused;
  const current = req.cookies.get(VIEW_AS_COOKIE)?.value;
  if (viewAsRef(current)) {
    console.warn(refusedWriteLog(req.method, "/booking/view-as", current));
    return NextResponse.json({ detail: "view_as_read_only", message: "Go back first." }, { status: 403 });
  }
  const body = await req.text();
  if (body.length > 2000) return NextResponse.json({ detail: "body_too_large" }, { status: 413 });
  return withAccess(req, async (access) => {
    const res = await upstream("view-as", access, { method: "POST", body });
    const data = (await res.json().catch(() => ({}))) as { session_id?: unknown; expires_at?: unknown };
    return res.ok ? started(data) : NextResponse.json(data, { status: res.status });
  });
}

export async function GET(req: NextRequest) {
  const ref = viewAsRef(req.cookies.get(VIEW_AS_COOKIE)?.value);
  return withAccess(req, async (access) => {
    const res = await upstream("view-as", access, { view: ref });
    const data = await res.json().catch(() => ({}));
    if (isViewEnded(res.status, data) || res.status === 403) return clearViewAsCookie(NextResponse.json({ active: false }));
    return NextResponse.json(data, { status: res.status });
  });
}

export async function DELETE(req: NextRequest) {
  const refused = crossSite(req);
  if (refused) return refused;
  const ref = viewAsRef(req.cookies.get(VIEW_AS_COOKIE)?.value);
  if (!ref) return clearViewAsCookie(NextResponse.json({ ended: false }));
  return withAccess(req, async (access) => {
    let ended = false;
    try {
      ended = (await upstream(`view-as/${ref}/end`, access, { method: "POST" })).ok;
    } catch {
      /* the cookie is cleared anyway; the view ends at its expiry */
    }
    return clearViewAsCookie(NextResponse.json({ ended }));
  });
}
