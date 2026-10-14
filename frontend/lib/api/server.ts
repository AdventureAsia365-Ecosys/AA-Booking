// lib/api/server.ts
// AA-683 — server-side Booking API calls (route handlers and server components). Never imported by
// client code: it reads the server-only BOOKING_API_URL.

import { bookingApiUrl, UPSTREAM_TIMEOUT_MS } from "../config";
import type { StaffMe } from "./types";

export type StaffMeResult =
  | { kind: "ok"; staff: StaffMe }
  | { kind: "unauthorized" } // 401: token missing / invalid / expired
  | { kind: "inactive" } // 403 staff_inactive: profile exists, waits for a Booking admin
  | { kind: "forbidden"; code: string } // 403 other (e.g. unknown_cis_role)
  | { kind: "unavailable"; status: number | null }; // network, 404 (endpoint not deployed), 5xx

/** GET /booking/staff/me with a Booking JWT. Provisions the profile on first call (backend rule). */
export async function fetchStaffMe(accessToken: string): Promise<StaffMeResult> {
  let res: Response;
  try {
    res = await fetch(`${bookingApiUrl()}/booking/staff/me`, {
      headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      cache: "no-store",
    });
  } catch {
    return { kind: "unavailable", status: null };
  }
  if (res.status === 401) return { kind: "unauthorized" };
  if (res.status === 403) {
    let code = "";
    try {
      const body = (await res.json()) as { detail?: unknown };
      code = typeof body.detail === "string" ? body.detail : "";
    } catch {
      /* ignore */
    }
    return code === "staff_inactive" ? { kind: "inactive" } : { kind: "forbidden", code };
  }
  if (!res.ok) return { kind: "unavailable", status: res.status };
  try {
    return { kind: "ok", staff: (await res.json()) as StaffMe };
  } catch {
    return { kind: "unavailable", status: res.status };
  }
}
