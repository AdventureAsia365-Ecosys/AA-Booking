// lib/sameOrigin.ts
// AA-683 — one same-origin check for every state-changing route handler (BFF writes, sign-in, sign-out;
// S236 review finding 8). SameSite=Lax cookies already stop most cross-site writes; this refuses the rest,
// including login CSRF (another site signing the browser into the attacker's account).
//
// `strict`: a request with neither `Origin` nor `Sec-Fetch-Site` is refused too. Every current browser sends
// one of them on a POST, so only non-browser clients hit that branch.

import type { NextRequest } from "next/server";

export function isSameOrigin(req: NextRequest, { strict = false }: { strict?: boolean } = {}): boolean {
  const origin = req.headers.get("origin");
  if (origin) return origin === req.nextUrl.origin;
  const site = req.headers.get("sec-fetch-site");
  if (site) return site === "same-origin";
  return !strict;
}
