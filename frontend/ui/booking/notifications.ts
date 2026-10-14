// ui/booking/notifications.ts
// AA-683 — staff notification vocabulary (backend app/notifications/service.py STAFF_TYPES, UI spec §4.3)
// and how an item links to its entity.

import type { StaffNotification } from "@/lib/api/types";

export const NOTIFICATION_TYPE_LABEL: Record<string, string> = {
  "trip_case.new_inquiry": "New inquiry",
  "trip_case.assigned": "Case assigned",
  "trip_case.status_changed": "Status changed",
  "proposal.approved": "Proposal approved",
};

/** Where an item leads, and the action label (one per type, spec §4.3). */
export function notificationTarget(n: StaffNotification): { href: string; label: string } | null {
  const code = typeof n.data?.code === "string" ? n.data.code : null;
  if (n.entity_type === "trip_case" || n.entity_type === "trip_case_version") {
    const ref = code ?? n.entity_id;
    return ref ? { href: `/cases/${encodeURIComponent(ref)}`, label: "Open case" } : null;
  }
  if (n.entity_type === "customer" && n.entity_id) {
    return { href: `/customers/${encodeURIComponent(n.entity_id)}`, label: "Open customer" };
  }
  return null;
}
