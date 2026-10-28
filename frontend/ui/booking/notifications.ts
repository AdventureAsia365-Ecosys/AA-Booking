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
  // AA-996 (Figma audit): every other type the backend writes to the staff feed, so no row shows a raw key and the
  // Type filter can select each one. AA-682 operations (app/ops/lifecycle.py, supplier_bookings.py, payments.py,
  // invoices.py) and retention (app/retention/jobs.py).
  "trip_case.gate_blocked": "Case cannot move on",
  "trip_check.due": "Trip check due",
  "supplier_booking.waiting": "Supplier not answering",
  "supplier_booking.cancel_with_supplier": "Cancel with supplier",
  "payment.recorded": "Payment to verify",
  "payment.due_soon": "Payment due soon",
  "invoice.overdue": "Invoice overdue",
  "invoice.overpaid": "Invoice overpaid",
  "invoice.balance_reopened": "Balance reopened",
  "invoice.case_cancelled": "Invoice of a cancelled trip",
  "retention.held": "Retention held",
};

/** The text of a data field, or null. */
function text(n: StaffNotification, key: string): string | null {
  const v = n.data?.[key];
  return typeof v === "string" && v ? v : null;
}

/** Where an item leads, and the action label (one per type, spec §4.3). */
export function notificationTarget(n: StaffNotification): { href: string; label: string } | null {
  const code = text(n, "code");
  // The operations reminders name their case in `case_code` (trip_check.due, supplier_booking.waiting).
  const caseCode = code ?? text(n, "case_code");
  if (n.type === "b2b.on_request") return { href: "/b2b-desk", label: "Open B2B desk" };
  if (n.type === "trip_check.due") {
    const ref = caseCode ?? n.entity_id;
    return ref ? { href: `/cases/${encodeURIComponent(ref)}?tab=checks`, label: "Open checks" } : null;
  }
  if (n.entity_type === "trip_case" || n.entity_type === "trip_case_version") {
    const ref = caseCode ?? n.entity_id;
    return ref ? { href: `/cases/${encodeURIComponent(ref)}`, label: "Open case" } : null;
  }
  // AA-996: money rows open the invoice page (by its number), a recorded payment opens the payment to verify.
  if (n.entity_type === "invoice") {
    const number = text(n, "invoice_number");
    return number ? { href: `/billing/invoices/${encodeURIComponent(number)}`, label: "Open invoice" } : null;
  }
  if (n.entity_type === "payment") {
    const ref = text(n, "ref") ?? n.entity_id;
    return ref ? { href: `/billing/payments/${encodeURIComponent(ref)}`, label: "Open payment" } : null;
  }
  if (n.entity_type === "supplier_booking") {
    // Here `code` is the booking's own SB- code: the case is `case_code`.
    const ref = text(n, "case_code");
    return ref ? { href: `/cases/${encodeURIComponent(ref)}?tab=suppliers`, label: "Open suppliers" } : null;
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
