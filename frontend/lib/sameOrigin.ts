// lib/sameOrigin.ts
// AA-683 — one same-origin check for every state-changing route handler (BFF writes, sign-in, sign-out;
// S236 review finding 8). SameSite=Lax cookies already stop most cross-site writes; this refuses the rest,
// including login CSRF (another site signing the browser into the attacker's account).
//
// `strict`: a request with neither `Origin` nor `Sec-Fetch-Site` is refused too. Every current browser sends
// one of them on a POST, so only non-browser clients hit that branch.

import type { NextRequest } from "next/server";

/** The part of NextRequest these checks read (testable without Next). */
type OriginCheckRequest = Pick<NextRequest, "headers"> & { nextUrl: { origin: string } };

/**
 * AA-911 (staff web on ECS, root ADR 0019 d23) — the public origin this request was sent to. Behind the ALB the Next
 * standalone server builds `nextUrl` from its own bind address (https://0.0.0.0:3001), not from the request, so a
 * request that came over HTTPS (the ALB sets `X-Forwarded-Proto`) takes its origin from the `Host` header instead. A
 * browser sets `Host` to the site it is talking to and `Origin` to the page that sent the request; a cross-site page
 * controls neither, so comparing the two keeps the check. On Vercel and in `next dev` both ways give the same value.
 */
export function requestOrigin(req: OriginCheckRequest): string {
  const proto = (req.headers.get("x-forwarded-proto") ?? "").split(",")[0].trim().toLowerCase();
  const host = (req.headers.get("host") ?? "").trim().toLowerCase();
  if (proto === "https" && host) return `https://${host}`;
  return req.nextUrl.origin;
}

export function isSameOrigin(req: OriginCheckRequest, { strict = false }: { strict?: boolean } = {}): boolean {
  const origin = req.headers.get("origin");
  if (origin) return origin.toLowerCase() === requestOrigin(req);
  const site = req.headers.get("sec-fetch-site");
  if (site) return site === "same-origin";
  return !strict;
}
