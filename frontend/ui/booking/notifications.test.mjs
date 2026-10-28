// ui/booking/notifications.test.mjs — AA-996 slice 5: every staff notification type has a label, and each item
// leads to its record (invoice, payment, case checks, supplier bookings).
// Run: node --test ui/booking/*.test.mjs (Node 22 strips the TypeScript types of notifications.ts itself).
import assert from "node:assert/strict";
import { test } from "node:test";
import { NOTIFICATION_TYPE_LABEL, notificationTarget } from "./notifications.ts";

const n = (over) => ({ id: "n", type: "x", title: null, message: null, entity_type: null, entity_id: null, data: {}, created_at: "", read: false, ...over });

// The types the backend writes to the staff feed (app/notifications/service.py STAFF_TYPES, app/ops/*, retention).
const BACKEND_STAFF_TYPES = [
  "trip_case.new_inquiry", "trip_case.assigned", "trip_case.status_changed", "proposal.approved", "tour.retired",
  "tour.reinstated", "b2b.on_request", "trip_case.gate_blocked", "trip_check.due", "supplier_booking.waiting",
  "supplier_booking.cancel_with_supplier", "payment.recorded", "payment.due_soon", "invoice.overdue", "invoice.overpaid",
  "invoice.balance_reopened", "invoice.case_cancelled", "supplier_bill.due", "supplier_bill.approved", "retention.held",
];

test("every backend staff type has a human label (no raw key in a chip or the Type filter)", () => {
  for (const t of BACKEND_STAFF_TYPES) {
    assert.ok(NOTIFICATION_TYPE_LABEL[t], `${t} has a label`);
    assert.ok(!NOTIFICATION_TYPE_LABEL[t].includes("."), `${t} label is not a key`);
  }
});

test("invoice rows open the invoice by number", () => {
  const t = notificationTarget(n({ type: "invoice.overdue", entity_type: "invoice", entity_id: "uuid-1", data: { invoice_number: "INV-000004", case_code: "TC-000022" } }));
  assert.deepEqual(t, { href: "/billing/invoices/INV-000004", label: "Open invoice" });
  assert.equal(notificationTarget(n({ type: "payment.due_soon", entity_type: "invoice", entity_id: "uuid-1", data: {} })), null);
});

test("a recorded payment opens the payment to verify", () => {
  const t = notificationTarget(n({ type: "payment.recorded", entity_type: "payment", entity_id: "p-1", data: { ref: "PMT-000007", invoice_number: "INV-000004" } }));
  assert.deepEqual(t, { href: "/billing/payments/PMT-000007", label: "Open payment" });
});

test("operations reminders name the case in case_code", () => {
  assert.deepEqual(
    notificationTarget(n({ type: "trip_check.due", entity_type: "trip_case", entity_id: "c-1", data: { case_code: "TC-000010" } })),
    { href: "/cases/TC-000010?tab=checks", label: "Open checks" },
  );
  assert.deepEqual(
    notificationTarget(n({ type: "supplier_booking.waiting", entity_type: "supplier_booking", entity_id: "sb-1", data: { code: "SB-000001", case_code: "TC-000010" } })),
    { href: "/cases/TC-000010?tab=suppliers", label: "Open suppliers" },
  );
});

test("the case of a gate-blocked row (data.code) and a row with no page", () => {
  assert.deepEqual(
    notificationTarget(n({ type: "trip_case.gate_blocked", entity_type: "trip_case", entity_id: "c-1", data: { code: "TC-000003" } })),
    { href: "/cases/TC-000003", label: "Open case" },
  );
  assert.equal(notificationTarget(n({ type: "retention.held", entity_type: "retention", entity_id: "j-1" })), null);
});
