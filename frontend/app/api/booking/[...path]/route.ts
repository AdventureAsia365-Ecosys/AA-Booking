// app/api/booking/[...path]/route.ts
// AA-683 — the BFF proxy (UI spec §6): browser → /api/booking/<path> → <BOOKING_API_URL>/booking/<path>
// with `Authorization: Bearer <Booking JWT>` from the httpOnly cookie. The browser never holds a token.
//
// - Company e-mail session (AA-680: bk_staff_rt refresh cookie, path /api): the Cognito access token is refreshed
//   (single-flight, lib/cognito.ts) when it is missing or under 2 minutes from expiry, and once more if the API still
//   answers 401; the rotated cookies are set on the answer. A revoked / expired refresh token ends the session (401).
// - No refresh cookie → 401 (the page sends the user to /login). The CIS password bridge is retired (AA-911).
// - Only JSON bodies are forwarded (no uploads in Wave 1); the upstream status and body pass through.
// - Path segments are re-encoded and `.` / `..` are refused, so a request can never leave /booking/.

import { NextRequest, NextResponse } from "next/server";
import { bookingApiUrl, UPSTREAM_TIMEOUT_MS } from "@/lib/config";
import { ACCESS_COOKIE, clearSessionCookies, needsRefresh } from "@/lib/session";
import {
  CognitoAuthError,
  readRefreshCookie,
  refreshStaffTokens,
  setStaffSessionCookies,
  type StaffTokens,
} from "@/lib/cognito";
import { isSameOrigin } from "@/lib/sameOrigin";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 1_000_000;
const PASS_HEADERS = ["content-type", "cache-control", "retry-after"];

type Ctx = { params: Promise<{ path: string[] }> };

function json(status: number, detail: string, message: string) {
  return NextResponse.json({ detail, message }, { status });
}

function endSession(status: number, detail: string, message: string) {
  const res = json(status, detail, message);
  clearSessionCookies(res);
  return res;
}

async function handler(req: NextRequest, { params }: Ctx) {
  const { path } = await params;
  if (!path?.length || path.some((s) => s === "." || s === ".." || s.includes("\\"))) {
    return json(400, "bad_path", "Invalid API path.");
  }

  // Defence in depth on top of SameSite=Lax: a write must come from this origin.
  if (req.method !== "GET" && req.method !== "HEAD") {
    if (!isSameOrigin(req)) return json(403, "bad_origin", "Cross-site request refused.");
  }

  const rt = readRefreshCookie(req);
  if (!rt) return endSession(401, "not_signed_in", "Your session has ended. Sign in again.");

  let access = req.cookies.get(ACCESS_COOKIE)?.value ?? "";
  let rotated: StaffTokens | null = null;

  async function remint(): Promise<NextResponse | null> {
    try {
      rotated = await refreshStaffTokens(rt!.refreshToken);
    } catch (e) {
      if (e instanceof CognitoAuthError && e.code === "unreachable") {
        return json(503, "auth_unavailable", "Company sign-in is not answering right now. Try again in a moment.");
      }
      return endSession(401, "session_expired", "Your session has ended. Sign in again.");
    }
    access = rotated.accessToken;
    return null;
  }

  if (needsRefresh(access)) {
    const failed = await remint();
    if (failed) return failed;
  }

  let body: string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.text();
    if (body.length > MAX_BODY_BYTES) return json(413, "body_too_large", "Request body is too large.");
    if (!body) body = undefined;
  }

  const url = `${bookingApiUrl()}/booking/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const send = () =>
    fetch(url, {
      method: req.method,
      headers: {
        Authorization: `Bearer ${access}`,
        Accept: "application/json",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body,
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      cache: "no-store",
    });

  let upstream: Response;
  try {
    upstream = await send();
    if (upstream.status === 401 && !rotated) {
      const failed = await remint();
      if (failed) return failed;
      upstream = await send();
    }
  } catch {
    return json(502, "upstream_unavailable", "The Booking service is not answering. Try again in a moment.");
  }

  const headers = new Headers();
  for (const h of PASS_HEADERS) {
    const v = upstream.headers.get(h);
    if (v) headers.set(h, v);
  }
  const out = new NextResponse(upstream.status === 204 ? null : await upstream.arrayBuffer(), {
    status: upstream.status,
    headers,
  });
  const cognito = rotated as StaffTokens | null;
  if (cognito && rt) setStaffSessionCookies(out, cognito, rt.sessionEnd, rt.refreshToken);
  return out;
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
