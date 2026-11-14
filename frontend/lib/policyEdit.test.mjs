// lib/policyEdit.test.mjs — AA-1096 (S280 settings UI audit): Pricing > Policy finds a parameter by key or translated
// words, shows the value on the chosen day, and checks the range and the cross-parameter bound before sending.
// Run: npm run test:unit.
import assert from "node:assert/strict";
import { test } from "node:test";

import { boundProblem, crossHolds, fold, matchesSearch, scheduledAfter, searchText, valueOn } from "./policyEdit.ts";

const v = (version_no, value, effective_from) => ({ version_no, value, effective_from });

test("one search finds a parameter by key, label, Vietnamese guide or approver name, accents ignored", () => {
  const p = {
    key: "agency_reminder_steps_days",
    label: "Overdue invoice reminders",
    help: "Days after the due date",
    guide: { en: { what: "When unpaid invoices are reminded" }, vi: { what: "Khi nào hoá đơn chưa trả được nhắc" } },
  };
  const hay = searchText(p, ["Nhắc hoá đơn quá hạn", "Finance", "Kế toán"]);
  assert.ok(matchesSearch(hay, "agency_reminder_steps_days"));
  assert.ok(matchesSearch(hay, "reminder steps"));
  assert.ok(matchesSearch(hay, "hoa don"));
  assert.ok(matchesSearch(hay, "KE TOAN"));
  assert.ok(matchesSearch(hay, ""));
  assert.ok(!matchesSearch(hay, "retention"));
  assert.equal(fold("Quản trị đ"), "quan tri d");
});

test("the value on a day: the newest of the value in force and the scheduled ones starting by then", () => {
  const p = { current: v(1, 14, "2026-01-01"), scheduled: [v(3, 10, "2027-03-01"), v(2, 12, "2027-01-01")] };
  assert.equal(valueOn(p, "2026-12-31").value, 14);
  assert.equal(valueOn(p, "2027-01-01").value, 12);
  assert.equal(valueOn(p, "2027-06-01").value, 10);
  assert.equal(valueOn({ current: null, scheduled: [] }, "2027-01-01"), null);
  assert.deepEqual(scheduledAfter(p, "2026-12-01").map((x) => x.version_no), [2, 3]);
  assert.deepEqual(scheduledAfter(p, "2027-01-01").map((x) => x.version_no), [3]);
});

test("range: every number of a value inside min and max; text values are not numeric", () => {
  const p = { type: "integers", minimum: "1", maximum: "365" };
  assert.equal(boundProblem(p, [1, 7, 365]), null);
  assert.equal(boundProblem(p, [0, 7]), "min");
  assert.equal(boundProblem(p, [7, 400]), "max");
  assert.equal(boundProblem({ type: "decimal", minimum: "0", maximum: "100" }, "100.5"), "max");
  assert.equal(boundProblem({ type: "amount_map", minimum: "0", maximum: "1000" }, { USD: "5", EUR: "2000" }), "max");
  assert.equal(boundProblem({ type: "month_day", minimum: null, maximum: null }, "11-01"), null);
});

test("cross bound against the other parameter's value on the same day", () => {
  assert.ok(crossHolds("lte", 7, 14));
  assert.ok(!crossHolds("lte", 21, 14));
  assert.ok(crossHolds("gt", 12, 2));
  assert.ok(!crossHolds("lt", 12, 11));
  assert.ok(!crossHolds("ne", "11-01", "11-01"));
  assert.ok(crossHolds("ne", "10-01", "11-01"));
  assert.ok(crossHolds("gte", 5, null));
});
