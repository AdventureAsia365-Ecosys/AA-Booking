// ui/booking/staffRoles.test.mjs — AA-1155 T7: the role editor's pure helpers (ui/booking/staffRoles.ts) and the page's
// wiring (app/(staff)/roles): built from the kit, writes only for an owner, never an approve-class capability offered.
// Run: node --test ui/booking/staffRoles.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const { suggestCode, groupCapabilities, cloneOf, toggle, sameSet, CUSTOM_CODE } = await import(
  new URL("./staffRoles.ts", import.meta.url).href
);
const PAGE = readFileSync(new URL("../../app/(staff)/roles/_parts/RolesView.tsx", import.meta.url), "utf8");

const CAPS = [
  { name: "supply.read", assignable: true },
  { name: "supply.write", assignable: true },
  { name: "supplier.activate", assignable: false },
  { name: "pricing.write", assignable: true },
  { name: "pricing.approve", assignable: false },
  { name: "b2b_desk.read", assignable: true },
];

test("a code is suggested from the label in the custom pattern", () => {
  assert.equal(suggestCode("Quay desk"), "custom_quay_desk");
  assert.equal(suggestCode("  Product, ops & accounting "), "custom_product_ops_accounting");
  assert.equal(suggestCode("Điều hành tour"), "custom_dieu_hanh_tour");
  assert.equal(suggestCode("2nd line"), "custom_nd_line");
  assert.equal(suggestCode("!!"), "");
  assert.equal(suggestCode("x"), ""); // too short for the pattern
  const long = suggestCode("a".repeat(80));
  assert.ok(CUSTOM_CODE.test(long) && long.length === 54, long);
});

test("capabilities are grouped by area in order", () => {
  const groups = groupCapabilities(CAPS);
  assert.deepEqual(groups.map((g) => g.area), ["b2b_desk", "pricing", "supplier", "supply"]);
  assert.deepEqual(groups[1].items.map((c) => c.name), ["pricing.approve", "pricing.write"]);
});

test("a clone drops the approve class", () => {
  assert.deepEqual(cloneOf(["supply.write", "supplier.activate", "pricing.approve", "supply.read"], CAPS), ["supply.read", "supply.write"]);
});

test("an approve-class capability can never be ticked", () => {
  assert.deepEqual(toggle(["supply.read"], "pricing.approve", true, CAPS), ["supply.read"]);
  assert.deepEqual(toggle(["supply.read"], "pricing.write", true, CAPS), ["pricing.write", "supply.read"]);
  assert.deepEqual(toggle(["pricing.write", "supply.read"], "pricing.write", false, CAPS), ["supply.read"]);
  assert.deepEqual(toggle([], "made.up", true, CAPS), []);
});

test("sets compare by names", () => {
  assert.ok(sameSet(["a", "b"], ["b", "a"]));
  assert.ok(!sameSet(["a"], ["a", "b"]));
});

test("the page is kit-built, owner-gated and offers no approval", () => {
  assert.match(PAGE, /from "@\/ui"/);
  assert.doesNotMatch(PAGE, /<(input|select|textarea|button)\b/); // kit controls only (no-raw-form-control)
  assert.match(PAGE, /can_edit/);
  assert.match(PAGE, /disabled=\{!c\.assignable/);
  assert.match(PAGE, /staffRolesApi\.(create|update)/);
  assert.doesNotMatch(PAGE, /["'`][^"'`\n]*AA-\d+[^"'`\n]*["'`]/); // no issue ids in UI text
});
