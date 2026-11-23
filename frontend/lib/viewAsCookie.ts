// lib/viewAsCookie.ts — AA-1157: set and clear the httpOnly view-as cookie on a BFF answer (lib/viewAs.ts).
import type { NextResponse } from "next/server";
import { cookieOptions } from "./session";
import { VIEW_AS_COOKIE } from "./viewAs";

export function setViewAsCookie(res: NextResponse, sessionId: string, maxAgeS: number): NextResponse {
  res.cookies.set(VIEW_AS_COOKIE, sessionId, { ...cookieOptions(maxAgeS), httpOnly: true, sameSite: "strict", path: "/" });
  return res;
}

export function clearViewAsCookie<T extends NextResponse>(res: T): T {
  res.cookies.set(VIEW_AS_COOKIE, "", { ...cookieOptions(0), httpOnly: true, sameSite: "strict", path: "/", maxAge: 0 });
  return res;
}
