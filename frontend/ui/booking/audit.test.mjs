// ui/booking/audit.test.mjs — AA-996 slice 5: audit badge colours follow UI spec §1.3
// (CREATE → success · UPDATE → info · STATUS → warning · DELETE / BLOCK / VOID → danger).
// Run: node --test ui/booking/*.test.mjs (Node 22 strips the TypeScript types of audit.ts itself).
import assert from "node:assert/strict";
import { test } from "node:test";
import { auditActionLabel, auditActionTone } from "./audit.ts";

test("create class is success", () => {
  for (const a of ["trip_case.created", "staff.provisioned", "payment.recorded", "supplier_media.uploaded", "b2b_hold.placed"]) {
    assert.equal(auditActionTone(a), "success", a);
  }
});

test("status class is warning", () => {
  for (const a of ["trip_case.status_changed", "invoice.sent", "payment.verified", "supplier_contract.published", "invoice.amended", "invoice.reminded"]) {
    assert.equal(auditActionTone(a), "warning", a);
  }
});

test("delete / block / void class is danger", () => {
  for (const a of ["customer.archived", "customer.blocked", "payment.voided", "invoice.cancelled", "invoice.case_cancelled", "supplier.deactivated", "trip_case_version.deleted"]) {
    assert.equal(auditActionTone(a), "danger", a);
  }
});

test("updates and their opposites stay info", () => {
  for (const a of ["supplier.updated", "customer.unblocked", "trip_case.unassigned", "retention.dry_run", "staff.role_synced"]) {
    assert.equal(auditActionTone(a), "info", a);
  }
});

test("a staff.updated row that switched access reads as what it did", () => {
  const off = { action: "staff.updated", before: { active: true, role: "sales" }, after: { active: false, role: "sales", reason: "left" } };
  assert.equal(auditActionTone(off), "danger");
  assert.equal(auditActionLabel(off), "deactivated");
  const on = { action: "staff.updated", before: { active: false }, after: { active: true } };
  assert.equal(auditActionTone(on), "warning");
  assert.equal(auditActionLabel(on), "activated");
  const role = { action: "staff.updated", before: { active: true, role: "sales" }, after: { active: true, role: "admin" } };
  assert.equal(auditActionTone(role), "info");
  assert.equal(auditActionLabel("trip_case_version.items_replaced"), "items replaced");
});
