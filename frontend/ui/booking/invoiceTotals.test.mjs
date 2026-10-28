// ui/booking/invoiceTotals.test.mjs — AA-996 slice 5: the invoice totals footer and the dates pair.
// Run: node --test ui/booking/*.test.mjs (Node 22 strips the TypeScript types of invoiceTotals.ts itself).
import assert from "node:assert/strict";
import { test } from "node:test";
import { invoiceDueDates, invoiceTotals } from "./invoiceTotals.ts";

test("one locked line: no adjustments row", () => {
  assert.deepEqual(invoiceTotals([{ kind: "version_total", amount: "2400.00" }]), {
    approved: "2400.00",
    adjustments: null,
    adjustmentCount: 0,
  });
});

test("adjustments add up exactly, negative ones included (no float drift)", () => {
  const t = invoiceTotals([
    { kind: "version_total", amount: "2400.00" },
    { kind: "adjustment", amount: "0.10" },
    { kind: "adjustment", amount: "0.20" },
    { kind: "adjustment", amount: "-150.05" },
  ]);
  assert.deepEqual(t, { approved: "2400.00", adjustments: "-149.75", adjustmentCount: 3 });
});

test("VND whole amounts and an unreadable amount", () => {
  assert.equal(invoiceTotals([{ kind: "version_total", amount: "1284500000" }])?.approved, "1284500000.00");
  assert.equal(invoiceTotals([{ kind: "version_total", amount: "12,00" }]), null);
});

test("due date is the last live schedule row; next due only when earlier", () => {
  const schedule = [
    { due_date: "2026-11-06", state: "overdue" },
    { due_date: "2027-01-10", state: "scheduled" },
    { due_date: "2027-03-01", state: "cancelled" },
  ];
  assert.deepEqual(invoiceDueDates(schedule, "2026-11-06"), { due: "2027-01-10", next: "2026-11-06" });
  assert.deepEqual(invoiceDueDates(schedule, "2027-01-10"), { due: "2027-01-10", next: null });
  assert.deepEqual(invoiceDueDates([], null), { due: null, next: null });
});
