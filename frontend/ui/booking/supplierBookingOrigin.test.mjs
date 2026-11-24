// ui/booking/supplierBookingOrigin.test.mjs — S291 (AA-1149 follow-up): a supplier booking generated from a sold tour
// is labelled as coming from the tour, not as an extra item. Run: npm run test:unit.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bookingOrigin } from "./supplierBookingOrigin.ts";

const PART = "5d1e7c3a-2b4f-4c6d-8e9f-0a1b2c3d4e5f";

test("a version item keeps its position", () => {
  assert.deepEqual(bookingOrigin({ item_position: 3, quote_part_id: null, tour_day: null }), { key: " · item {0}", args: [3] });
});

test("a booking of a sold tour component comes from the tour, with its day when known", () => {
  assert.deepEqual(bookingOrigin({ item_position: null, quote_part_id: PART, tour_day: 2 }), {
    key: " · from the tour, day {0}",
    args: [2],
  });
  assert.deepEqual(bookingOrigin({ item_position: null, quote_part_id: PART, tour_day: null }), { key: " · from the tour", args: [] });
});

test("only a booking added by hand is an extra item", () => {
  assert.deepEqual(bookingOrigin({ item_position: null, quote_part_id: null, tour_day: null }), { key: " · extra item", args: [] });
});
