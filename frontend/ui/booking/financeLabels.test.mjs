// ui/booking/financeLabels.test.mjs — AA-1074 (S277 signed-in UAT fixes): the tour label of a finance departure row
// and the per-currency totals kept next to the USD summary of a case. Run: npm run test:unit.
import assert from "node:assert/strict";
import { test } from "node:test";
import { nativeTotals, tourLabel } from "./financeLabels.ts";

const SOURCE = "6f1c2a9e-0000-4000-8000-000000000041";
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

test("a departure row names its tour by the catalog title", () => {
  assert.equal(tourLabel("Lantern Coast Loop", SOURCE), "Lantern Coast Loop");
});

test("without a title the row shows a short key, never the raw catalog id", () => {
  for (const title of [null, undefined, "", "  "]) {
    const label = tourLabel(title, SOURCE);
    assert.equal(label, "Tour 6f1c2a9e");
    assert.doesNotMatch(label, UUID);
  }
  assert.equal(tourLabel(null, null), "—");
});

const USD = { currency: "USD", net: "100.00", billed: "0.00", variance: null };
const EUR = { currency: "EUR", net: "50.00", billed: "50.00", variance: "0.00" };

test("a USD-only case shows the USD summary alone", () => {
  assert.deepEqual(nativeTotals([USD], true), []);
});

test("other currencies keep their own line next to the USD summary", () => {
  assert.deepEqual(nativeTotals([USD, EUR], true), [EUR]);
});

test("without the USD summary every currency keeps its line", () => {
  assert.deepEqual(nativeTotals([USD, EUR], false), [USD, EUR]);
});
