// ui/booking/priceWorkbench.test.mjs — AA-1091: Price setup helpers (next step links, the component set saved after
// accepting suggestions, import problem lines, bulk action skip reasons).
// Run: node --test ui/booking/priceWorkbench.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { mergeComponents, problemLine, problemsBySheet, skippedReasons, stepHref } from "./priceWorkbench.ts";

const KEY = "aa109100-0000-4000-8000-000000000001";

test("every next step links to the page where it is done", () => {
  assert.equal(stepHref("add_components", KEY), `/catalog/trip-plans/${KEY}?tab=components`);
  assert.equal(stepHref("fix_inactive", KEY), `/catalog/trip-plans/${KEY}?tab=components`);
  assert.equal(stepHref("load_rates", KEY), "/suppliers");
  assert.equal(stepHref("approve_fx", KEY), "/pricing/rules?tab=fx");
  assert.equal(stepHref("approve_rule", KEY), "/pricing/rules");
  assert.equal(stepHref("recompute", KEY), `/catalog/trip-plans/${KEY}/commercial`);
  assert.equal(stepHref("make_sellable", KEY), `/catalog/trip-plans/${KEY}`);
  assert.equal(stepHref("done", "a b"), "/catalog/trip-plans/a%20b");
});

test("accepted suggestions are added to the plan's set, removed items leave it, each item once a day", () => {
  const current = [
    { day_no: 2, service_id: "sv-guide", hotel_room_type_id: null },
    { day_no: 1, hotel_room_type_id: "rt-1", service_id: null },
  ];
  const accepted = [{ day_no: 1, hotel_room_type_id: "rt-1" }, { day_no: 3, service_id: "sv-jeep" }];
  assert.deepEqual(mergeComponents(current, accepted, [{ day_no: 2, service_id: "sv-guide" }]), [
    { day_no: 1, hotel_room_type_id: "rt-1" },
    { day_no: 3, service_id: "sv-jeep" },
  ]);
  assert.deepEqual(mergeComponents([], []), []);
});

test("import problems read as sheet, row, column and message, grouped in workbook order", () => {
  assert.equal(
    problemLine({ sheet: "Prices", row: 3, column: "Valid from", message: "overlaps row 2" }),
    "Prices, row 3, Valid from: overlaps row 2",
  );
  assert.equal(problemLine({ sheet: "Tours", row: null, column: null, message: "no rows" }), "Tours: no rows");
  const groups = problemsBySheet([
    { sheet: "Departures" },
    { sheet: "Tours" },
    { sheet: "Other" },
    { sheet: "Tours" },
  ]);
  assert.deepEqual(groups.map(([s, l]) => [s, l.length]), [["Tours", 2], ["Departures", 1], ["Other", 1]]);
});

test("skipped reasons are counted, most frequent first", () => {
  assert.deepEqual(
    skippedReasons([
      { outcome: "done", reason: null },
      { outcome: "skipped", reason: "no_components" },
      { outcome: "skipped", reason: "has_supplier_price" },
      { outcome: "skipped", reason: "no_components" },
      { outcome: "skipped", reason: null },
    ]),
    [["no_components", 2], ["has_supplier_price", 1], ["other", 1]],
  );
});
