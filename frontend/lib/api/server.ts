// lib/api/server.ts
// AA-683 — server-side Booking API calls (route handlers and server components). Never imported by
// client code: it reads the server-only BOOKING_API_URL.

import { bookingApiUrl, UPSTREAM_TIMEOUT_MS } from "../config";
import type { StaffMe } from "./types";
import { viewAsHeaders } from "../viewAs";

export type StaffMeResult =
  | { kind: "ok"; staff: StaffMe }
  | { kind: "unauthorized" } // 401: token missing / invalid / expired
  | { kind: "inactive" } // 403 staff_inactive: profile exists, waits for (or was suspended by) a Booking admin
  | { kind: "forbidden"; code: string } // 403 other: no_booking_role (no booking-* group), identity_conflict, unknown_cis_role
  | { kind: "unavailable"; status: number | null } // network, 404 (endpoint not deployed), 5xx
  | { kind: "view_ended" }; // AA-1157: 409 view_as_ended, the owner's view is over (the layout clears the cookie)

/** GET /booking/staff/me with a Booking JWT. Provisions the profile on first call (backend rule). */
export async function fetchStaffMe(accessToken: string, viewRef?: string | null): Promise<StaffMeResult> {
  let res: Response;
  try {
    res = await fetch(`${bookingApiUrl()}/booking/staff/me`, {
      // AA-1157: inside the owner's "view as a lower role" the profile is the view's (lib/viewAs.ts).
      headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json", ...viewAsHeaders(viewRef) },
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      cache: "no-store",
    });
  } catch {
    return { kind: "unavailable", status: null };
  }
  if (res.status === 401) return { kind: "unauthorized" };
  if (res.status === 409 && viewRef) return { kind: "view_ended" };
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
