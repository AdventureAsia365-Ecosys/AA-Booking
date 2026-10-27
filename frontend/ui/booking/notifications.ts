// ui/booking/notifications.ts
// AA-683 — staff notification vocabulary (backend app/notifications/service.py STAFF_TYPES, UI spec §4.3)
// and how an item links to its entity.

import type { StaffNotification } from "@/lib/api/types";

export const NOTIFICATION_TYPE_LABEL: Record<string, string> = {
  "trip_case.new_inquiry": "New inquiry",
  "trip_case.assigned": "Case assigned",
  "trip_case.status_changed": "Status changed",
  "proposal.approved": "Proposal approved",
  // AA-986 L5 (root ADR 0015, Q4): a CIS tour used by an open case or a trip plan left the catalog / came back.
  "tour.retired": "Tour withdrawn",
  "tour.reinstated": "Tour back in catalog",
  // AA-975: a confirmed B2B booking has on-request lines for the B2B desk.
  "b2b.on_request": "B2B on request",
  // AA-682 S4 (bk-030): bk_ops_reminders announces a supplier bill to approve / pay that is due; approving one asks an
  // admin to pay it.
  "supplier_bill.due": "Supplier bill due",
  "supplier_bill.approved": "Bill approved: pay it",
};

/** Where an item leads, and the action label (one per type, spec §4.3). */
export function notificationTarget(n: StaffNotification): { href: string; label: string } | null {
  const code = typeof n.data?.code === "string" ? n.data.code : null;
  if (n.type === "b2b.on_request") return { href: "/b2b-desk", label: "Open B2B desk" };
  if (n.entity_type === "trip_case" || n.entity_type === "trip_case_version") {
    const ref = code ?? n.entity_id;
    return ref ? { href: `/cases/${encodeURIComponent(ref)}`, label: "Open case" } : null;
  }
  if (n.entity_type === "trip_plan") {
    // An orphaned plan has no detail page any more (its CIS content is gone): open the Orphaned filter.
    const key = typeof n.data?.source_id === "string" ? n.data.source_id : "";
    return n.type === "tour.retired"
      ? { href: `/catalog/trip-plans?show=orphaned${key ? `&q=${encodeURIComponent(key)}` : ""}`, label: "Open orphaned plans" }
      : key
        ? { href: `/catalog/trip-plans/${encodeURIComponent(key)}`, label: "Open trip plan" }
        : null;
  }
  if (n.entity_type === "customer" && n.entity_id) {
    return { href: `/customers/${encodeURIComponent(n.entity_id)}`, label: "Open customer" };
  }
  if (n.entity_type === "supplier_bill") {
    // The bill drawer opens by URL on the Supplier bills tab of /billing (code BIL-…, or the id).
    const ref = code ?? n.entity_id;
    return ref ? { href: `/billing?tab=supplier-bills&bill=${encodeURIComponent(ref)}`, label: "Open bill" } : null;
  }
  return null;
}
