// ui/booking/departureBoard.test.mjs — AA-1073 rest: the departure board calendar weeks, month moves and labels.
// Run: node --test ui/booking/departureBoard.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { adjustmentLabel, monthRange, monthWeeks, occupancy, shiftMonth } from "./departureBoard.ts";

test("monthWeeks starts on Monday and pads both ends", () => {
  const weeks = monthWeeks("2026-11"); // 1 Nov 2026 is a Sunday
  assert.equal(weeks.length, 6);
  assert.deepEqual(weeks[0], [null, null, null, null, null, null, "2026-11-01"]);
  assert.deepEqual(weeks[5], ["2026-11-30", null, null, null, null, null, null]);
  assert.ok(weeks.every((w) => w.length === 7));
  const feb = monthWeeks("2027-02"); // 1 Feb 2027 is a Monday, 28 days
  assert.equal(feb.length, 4);
  assert.equal(feb[0][0], "2027-02-01");
  assert.equal(feb[3][6], "2027-02-28");
});

test("shiftMonth and monthRange cross years and month lengths", () => {
  assert.equal(shiftMonth("2026-12", 1), "2027-01");
  assert.equal(shiftMonth("2027-01", -1), "2026-12");
  assert.deepEqual(monthRange("2028-02"), { from: "2028-02-01", to: "2028-02-29" });
  assert.deepEqual(monthRange("2026-11"), { from: "2026-11-01", to: "2026-11-30" });
});

test("adjustmentLabel and occupancy", () => {
  assert.equal(adjustmentLabel({ adjust_pct: "-0.1000", amount: null, currency: null }), "-10 %");
  assert.equal(adjustmentLabel({ adjust_pct: "0.0525", amount: null, currency: null }), "+5.25 %");
  assert.equal(adjustmentLabel({ adjust_pct: null, amount: "25.00", currency: "EUR" }), "+25 EUR per traveller");
  assert.equal(adjustmentLabel({ adjust_pct: null, amount: "-15", currency: "USD" }), "-15 USD per traveller");
  assert.equal(occupancy({ occupancy_pct: "50.0" }), 50);
  assert.equal(occupancy({ occupancy_pct: "120.0" }), 100);
  assert.equal(occupancy({ occupancy_pct: null }), null);
});
