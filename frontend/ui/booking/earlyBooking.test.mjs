// ui/booking/earlyBooking.test.mjs — AA-1073 rest (bk-17): the early-booking fields of the "New pricing rule" drawer.
// Run: node --test ui/booking/earlyBooking.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { earlyBookingAllowed, earlyBookingBody, earlyBookingError, earlyBookingLabel } from "./earlyBooking.ts";

test("only the b2b and fixed_departure rules carry the discount", () => {
  assert.equal(earlyBookingAllowed("b2b"), true);
  assert.equal(earlyBookingAllowed("fixed_departure"), true);
  assert.equal(earlyBookingAllowed("b2c_tailor_made"), false);
});

test("both values send both fields; points become a fraction", () => {
  assert.deepEqual(earlyBookingBody("b2b", " 60 ", "5"), { ok: true, body: { early_days: 60, early_discount_pts: "0.0500" } });
  assert.deepEqual(earlyBookingBody("fixed_departure", "1", "2.5"), { ok: true, body: { early_days: 1, early_discount_pts: "0.0250" } });
});

test("neither value, or a channel without the discount, sends nothing", () => {
  assert.deepEqual(earlyBookingBody("b2b", "", " "), { ok: true, body: {} });
  // Values left over after switching to a channel without the discount are not sent.
  assert.deepEqual(earlyBookingBody("b2c_tailor_made", "60", "5"), { ok: true, body: {} });
});

test("one value without the other is an error on the empty one", () => {
  assert.deepEqual(earlyBookingBody("b2b", "60", ""), { ok: false, field: "early_discount_pts", message: "Enter the margin points off, or clear the days." });
  const r = earlyBookingBody("b2b", "", "5");
  assert.equal(r.ok, false);
  assert.equal(r.ok === false && r.field, "early_days");
});

test("out-of-range values are refused on their field", () => {
  for (const days of ["0", "366", "1.5", "-3", "abc"]) {
    const r = earlyBookingBody("b2b", days, "5");
    assert.equal(r.ok === false && r.field, "early_days", days);
  }
  for (const pts of ["0", "100", "5.555", "-1", "x"]) {
    const r = earlyBookingBody("b2b", "30", pts);
    assert.equal(r.ok === false && r.field, "early_discount_pts", pts);
  }
});

test("the API's 422 codes map to a field", () => {
  assert.equal(earlyBookingError("early_booking_channel")?.field, "early_days");
  assert.equal(earlyBookingError("invalid_early_booking")?.field, "early_discount_pts");
  assert.equal(earlyBookingError("invalid_rule"), null);
});

test("the table label of a rule's discount", () => {
  assert.deepEqual(earlyBookingLabel({ early_days: 60, early_discount_pts: "0.0500" }), ["early −5 pts", "from 60 d"]);
  assert.deepEqual(earlyBookingLabel({ early_days: 90, early_discount_pts: "0.0250" }), ["early −2.5 pts", "from 90 d"]);
  assert.equal(earlyBookingLabel({ early_days: null, early_discount_pts: null }), null);
  assert.equal(earlyBookingLabel({}), null);
});
