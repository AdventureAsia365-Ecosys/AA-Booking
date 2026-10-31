// ui/booking/cisReadback.test.mjs — AA-1013: the CIS read-back helpers of the proposal builder and preview.
// Run: node --test ui/booking/*.test.mjs (Node 22 strips the TypeScript types of cisReadback.ts itself).
// Tour and day names are invented.
import assert from "node:assert/strict";
import { test } from "node:test";
import { cisNewTitle, cisTourName, cisUpdates, refreshFromCis } from "./cisReadback.ts";

const tour = (title, extra = {}) => ({
  source_id: "LR-5D",
  tour_title: "Lantern Ridge Trek — 5 Days",
  tour_updated_at: "2027-03-04T05:06:00Z",
  title,
  ...extra,
});
const item = (title, day_from, cis, day_to = day_from) => ({ title, day_from, day_to, cis });

test("a one-day item whose CIS day title moved on gets the new title", () => {
  assert.equal(cisNewTitle(item("Arrival at Lantern Ridge", 1, tour("Lantern Ridge arrival"))), "Lantern Ridge arrival");
});

test("whitespace, a missing CIS title or no tour is no change", () => {
  assert.equal(cisNewTitle(item("Ridge  walk\n", 2, tour("Ridge walk"))), null);
  assert.equal(cisNewTitle(item("Day 3", 3, tour(null))), null);
  assert.equal(cisNewTitle(item("Free day", 4, null)), null);
  assert.equal(cisNewTitle(item("Free day", 4, undefined)), null);
});

test("refresh replaces only the changed titles and keeps every other field", () => {
  const items = [
    { key: "a", ...item("Arrival at Lantern Ridge", 1, tour("Lantern Ridge arrival")), notes: "keep" },
    { key: "b", ...item("Ridge walk", 2, tour("Ridge walk")) },
    { key: "c", ...item("Own day", 3, null) },
  ];
  const out = refreshFromCis(items);
  assert.deepEqual(
    out.map((i) => [i.key, i.title]),
    [["a", "Lantern Ridge arrival"], ["b", "Ridge walk"], ["c", "Own day"]],
  );
  assert.equal(out[0].notes, "keep");
  assert.equal(out[1], items[1], "an unchanged item is the same object");
  assert.equal(cisUpdates(out).count, 0, "nothing left to refresh");
});

test("the summary counts changed items and names each tour once", () => {
  const other = { source_id: "SB-2D", tour_title: "Salt Bay Escape — 2 Days", tour_updated_at: null, title: "Bay arrival" };
  const s = cisUpdates([
    item("Old one", 1, tour("New one")),
    item("Old two", 2, tour("New two")),
    item("Same", 3, tour("Same")),
    item("Old bay", 4, other),
  ]);
  assert.equal(s.count, 3);
  assert.deepEqual(
    s.tours.map((t) => t.source_id),
    ["LR-5D", "SB-2D"],
  );
  assert.equal(s.tours[0].updated_at, "2027-03-04T05:06:00Z");
});

test("a multi-day item names its tour unless its title already does", () => {
  assert.equal(cisTourName(item("trip_plan tp:x · line 1", 1, tour(null), 5)), "Lantern Ridge Trek — 5 Days");
  assert.equal(cisTourName(item("Lantern Ridge Trek — 5 Days", 1, tour(null), 5)), null);
  assert.equal(cisTourName(item("Arrival", 1, tour("Arrival"))), null, "one-day items carry the day title");
  assert.equal(cisTourName(item("Own leg", 1, null, 3)), null);
});
