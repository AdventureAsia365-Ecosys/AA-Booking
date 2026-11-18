// ui/booking/plural.test.mjs — AA-1128 B4: English plurals of counts. Run: node --test ui/booking/plural.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { countText, daysText } from "./plural.ts";

test("countText uses the singular for exactly one", () => {
  assert.equal(countText(1, "day"), "1 day");
  assert.equal(countText(0, "day"), "0 days");
  assert.equal(countText(7, "day"), "7 days");
  assert.equal(countText(2, "person", "people"), "2 people");
});

test("daysText reads a trip length or falls back", () => {
  assert.equal(daysText(1), "1 day");
  assert.equal(daysText(14), "14 days");
  assert.equal(daysText(null, "—"), "—");
  assert.equal(daysText(undefined), "");
});
