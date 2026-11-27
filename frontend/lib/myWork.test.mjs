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

// AA-1190 — the empty queues fold into one line; tiles with work stay in their group, approvals first.
const { splitTiles, hasWork, hasPriceSetup, PRICE_SETUP_STEPS, workbenchStepHref } = await import(
  new URL("./myWork.ts", import.meta.url).href
);

test("tiles with nothing waiting fold into one line; approvals come first", () => {
  const row = (key, group, count, available = true) => ({ spec: { key, group }, tile: { key, available, count } });
  const rows = [
    row("my_open_cases", "work", 0),
    row("trip_checks_overdue", "work", 2),
    row("supplier_bills_to_pay", "approve", 1),
    row("addons_to_publish", "approve", 0),
    row("supplier_bills_due_soon", "work", null, false),
  ];
  const { groups, empty } = splitTiles(rows);
  assert.deepEqual(groups.map((g) => [g.group, g.rows.map((r) => r.spec.key)]), [
    ["approve", ["supplier_bills_to_pay"]],
    ["work", ["trip_checks_overdue", "supplier_bills_due_soon"]],
  ]);
  assert.deepEqual(empty.map((r) => r.spec.key), ["my_open_cases", "addons_to_publish"]);
  // while loading (no tile yet) every row is drawn as a skeleton, nothing is folded
  const loading = splitTiles([{ spec: { key: "a", group: "work" } }]);
  assert.equal(loading.groups[0].rows.length, 1);
  assert.deepEqual(loading.empty, []);
  assert.equal(hasWork({ available: false, count: null }), true);
  assert.equal(hasWork({ available: true, count: 0 }), false);
});

test("the price setup card: makers who read prices, each step opens the workbench filtered on it", () => {
  assert.equal(hasPriceSetup(["price_version.write", "pricing.read"]), true);
  assert.equal(hasPriceSetup(["price_version.write"]), false);
  assert.equal(hasPriceSetup(["pricing.read"]), false);
  assert.equal(workbenchStepHref("load_rates"), "/pricing/workbench?next_step=load_rates");
  const steps = PRICE_SETUP_STEPS.map((s) => s.step);
  assert.equal(new Set(steps).size, steps.length);
  assert.ok(!steps.includes("done") && !steps.includes("not_served"));
  assert.ok(pageRule("/pricing/workbench"));
});
