// ui/booking/ruleDrops.test.mjs — AA-1128 (B0 review): the scheduled pricing rules that lower margin, floor or RRP.
// Run: node --test ui/booking/ruleDrops.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { scheduledDrops } from "./ruleDrops.ts";

const rule = (id, channel, status, valid_from, target, floor, rrp = null) => ({
  id, channel, status, valid_from, target_margin: target, floor_margin: floor, rrp_markup_pct: rrp,
});

test("a scheduled rule that lowers target, floor and RRP against the rule in force is listed once per value", () => {
  const rows = [
    rule("now", "b2b", "in_force", "2026-10-14", "0.1600", "0.1000", "0.2500"),
    rule("next", "b2b", "scheduled", "2027-01-01", "0.0500", "0.0200", "0.1000"),
  ];
  assert.deepEqual(scheduledDrops(rows), [
    { id: "next", channel: "b2b", valid_from: "2027-01-01", field: "target_margin", from: "0.1600", to: "0.0500" },
    { id: "next", channel: "b2b", valid_from: "2027-01-01", field: "floor_margin", from: "0.1000", to: "0.0200" },
    { id: "next", channel: "b2b", valid_from: "2027-01-01", field: "rrp_markup_pct", from: "0.2500", to: "0.1000" },
  ]);
});

test("raising or keeping a value is no warning; a removed RRP is one", () => {
  const rows = [
    rule("now", "b2b", "in_force", "2026-10-14", "0.1600", "0.1000", "0.2500"),
    rule("next", "b2b", "scheduled", "2027-01-01", "0.1600", "0.1200", null),
  ];
  assert.deepEqual(scheduledDrops(rows), [
    { id: "next", channel: "b2b", valid_from: "2027-01-01", field: "rrp_markup_pct", from: "0.2500", to: null },
  ]);
});

test("each scheduled rule is compared with the approved rule just before it on its own channel", () => {
  const rows = [
    rule("old", "fit", "superseded", "2026-01-01", "0.9000", "0.9000"),
    rule("now", "fit", "in_force", "2026-10-14", "0.2000", "0.1500"),
    rule("up", "fit", "scheduled", "2026-12-01", "0.2500", "0.1500"),
    rule("down", "fit", "scheduled", "2027-02-01", "0.2200", "0.1500"),
    rule("other", "b2b", "in_force", "2026-10-14", "0.1000", "0.0500"),
  ];
  assert.deepEqual(scheduledDrops(rows), [
    { id: "down", channel: "fit", valid_from: "2027-02-01", field: "target_margin", from: "0.2500", to: "0.2200" },
  ]);
});

test("pending, rejected and withdrawn rows are never compared, and a first rule has nothing to lower", () => {
  const rows = [
    rule("pending", "b2b", "pending", "2026-12-01", "0.0100", "0.0100"),
    rule("rejected", "b2b", "rejected", "2026-12-02", "0.0100", "0.0100"),
    rule("first", "fit", "scheduled", "2026-12-01", "0.1000", "0.0500"),
  ];
  assert.deepEqual(scheduledDrops(rows), []);
  assert.deepEqual(scheduledDrops([]), []);
});

test("the list is ordered by day, then channel", () => {
  const rows = [
    rule("a0", "fit", "in_force", "2026-10-14", "0.2000", "0.1000"),
    rule("a1", "fit", "scheduled", "2027-01-01", "0.1000", "0.1000"),
    rule("b0", "b2b", "in_force", "2026-10-14", "0.2000", "0.1000"),
    rule("b1", "b2b", "scheduled", "2027-01-01", "0.1000", "0.1000"),
    rule("c1", "b2b", "scheduled", "2026-11-30", "0.2000", "0.0500"),
  ];
  assert.deepEqual(scheduledDrops(rows).map((d) => d.id), ["c1", "b1", "a1"]);
});
