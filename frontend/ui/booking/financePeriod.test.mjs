// ui/booking/financePeriod.test.mjs — AA-1074: the travel-date windows of the Costs & profit page.
// Run: npm run test:unit (Node's built-in runner strips the TypeScript types of financePeriod.ts itself).
import assert from "node:assert/strict";
import { test } from "node:test";
import { periodWindow } from "./financePeriod.ts";

const TODAY = new Date(2026, 10, 11); // 11 Nov 2026

test("the default is the next 3 months from the 1st", () => {
  assert.deepEqual(periodWindow("next_3", TODAY), { from: "2026-11-01", to: "2027-01-31" });
  assert.deepEqual(periodWindow("unknown", TODAY), { from: "2026-11-01", to: "2027-01-31" });
});

test("named windows", () => {
  assert.deepEqual(periodWindow("this_month", TODAY), { from: "2026-11-01", to: "2026-11-30" });
  assert.deepEqual(periodWindow("last_3", TODAY), { from: "2026-08-01", to: "2026-10-31" });
  assert.deepEqual(periodWindow("this_year", TODAY), { from: "2026-01-01", to: "2026-12-31" });
  assert.deepEqual(periodWindow("all", TODAY), {});
});
