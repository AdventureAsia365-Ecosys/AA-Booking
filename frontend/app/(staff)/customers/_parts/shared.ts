// app/(staff)/customers/_parts/shared.ts
// AA-683 (fe-people) — customer list tabs and the URL form of a customer reference.

import type { CustomerSummary } from "@/lib/api/types";

/** Tab key → API `status` (computed in booking.v_customer_summary; "blocked" is the is_blocked flag). */
export const CUSTOMER_TABS = [
  { key: "all", label: "All", status: undefined },
  { key: "lead", label: "Leads", status: "lead" },
  { key: "active", label: "Active", status: "active" },
  { key: "past", label: "Past clients", status: "past" },
  { key: "blocked", label: "Blocked", status: "blocked" },
] as const;

/** Customers are addressed by their CUS- code when they have one, else by UUID (both accepted by the API). */
export function customerRef(c: Pick<CustomerSummary, "code" | "id">): string {
  return encodeURIComponent(c.code ?? c.id);
}

export const SOURCE_LABEL: Record<string, string> = {
  tripplanner_lead: "TripPlanner",
  manual: "Added by staff",
  otp: "Signed up (OTP)",
};
