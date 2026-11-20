// lib/myWork.test.mjs — AA-1159: the My work tile table of the staff dashboard.
// Every tile opens a page the shell knows, its link carries the API's filter as the query string, and a role sees the
// band only when it holds a tile capability (shown by capability, never by person).
// Run: node --test lib/myWork.test.mjs (`npm run test:unit` runs every lib/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";

const { MY_WORK_TILES, MY_WORK_CAPABILITIES, hasMyWork, tileHref, tileSpec } = await import(
  new URL("./myWork.ts", import.meta.url).href
);
const { pageRule } = await import(new URL("./rbac.ts", import.meta.url).href);

test("each tile has a unique key, a known group and a page the shell routes", () => {
  const keys = MY_WORK_TILES.map((t) => t.key);
  assert.equal(new Set(keys).size, keys.length);
  for (const t of MY_WORK_TILES) {
    assert.ok(["work", "approve"].includes(t.group), t.key);
    assert.ok(pageRule(t.path), `${t.key}: ${t.path} has no page rule`);
    assert.ok(t.label && t.aria && t.empty, t.key);
    assert.equal(tileSpec(t.key), t);
  }
  assert.equal(tileSpec("no_such_tile"), undefined);
});

test("the link is the page path plus the API's filter", () => {
  assert.equal(tileHref("/billing", { bill_status: "open", bill_due: "soon" }), "/billing?bill_status=open&bill_due=soon");
  assert.equal(tileHref("/b2b-desk", {}), "/b2b-desk");
  assert.equal(
    tileHref("/cases", { tab: "mine", status: "discovery,building" }),
    "/cases?tab=mine&status=discovery%2Cbuilding",
  );
  assert.equal(tileHref("/trip-checks", { due: "overdue", empty: "" }), "/trip-checks?due=overdue");
});

test("the band shows for a holder of any tile capability, never for a role with none", () => {
  assert.ok(MY_WORK_CAPABILITIES.includes("bill.approve"));
  assert.equal(hasMyWork(["catalog.read"]), false);
  assert.equal(hasMyWork([]), false);
  assert.equal(hasMyWork(["catalog.read", "ops.read"]), true);
  assert.equal(hasMyWork(["agency_payment.approve"]), true);
});
