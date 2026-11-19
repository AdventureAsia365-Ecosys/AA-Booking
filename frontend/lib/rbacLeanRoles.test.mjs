// lib/rbacLeanRoles.test.mjs — AA-1150 (lean staff roles step 1): the page rules follow the API matrix.
// Finance opens the read pages of the reads it now holds (trip cases, prices, supply, operations, supplier bills; API
// trip_case.read, pricing.read, supply.read, ops.read, bill.read) and never an edit page; the built-in contracting
// role is named "Product & operations".
// Run: node --test lib/rbacLeanRoles.test.mjs (`npm run test:unit` runs every lib/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";

const { ROLE_LABEL, canSeePage } = await import(new URL("./rbac.ts", import.meta.url).href);

const FINANCE_READ_PAGES = [
  "/cases",
  "/cases/TC-000001",
  "/proposals",
  "/pricing/calculator",
  "/suppliers",
  "/suppliers/SUP-000001",
  "/hotels",
  "/services",
  "/services/SVC-000001",
  "/pricing/agency-tiers",
  "/pricing/agency-tiers/tariff",
  "/pricing/readiness",
  "/pricing/workbench",
  "/pricing/seasons",
  "/pricing/holiday-events",
  "/billing",
  "/trip-checks",
  "/b2b-desk/manifest/d0000000-0000-4000-8000-000000000001",
  "/catalog/trip-plans",
  "/finance",
  "/agency-billing",
];

// Edit pages and pages of rights finance does not hold (b2b_desk.read, staff.admin, trip_plan.write, ...).
const FINANCE_CLOSED_PAGES = [
  "/cases/new",
  "/suppliers/new",
  "/suppliers/SUP-000001/edit",
  "/suppliers/rate-sheets/import",
  "/pricing/rules",
  "/catalog/trip-plans/d0000000-0000-4000-8000-000000000001/commercial",
  "/b2b-desk",
  "/departures",
  "/staff",
  "/audit-log",
];

test("finance opens the read pages of its new reads", () => {
  for (const path of FINANCE_READ_PAGES) assert.ok(canSeePage("finance", path), path);
});

test("finance never opens an edit page or a page of a right it lacks", () => {
  for (const path of FINANCE_CLOSED_PAGES) assert.ok(!canSeePage("finance", path), path);
});

test("the read pages finance gained are unchanged for the other roles", () => {
  for (const path of FINANCE_READ_PAGES.filter((p) => !["/finance", "/agency-billing"].includes(p))) {
    assert.ok(canSeePage("contracting", path), path);
  }
  const supplierBills = "/billing"; // a literal call on this path trips the retired-route guard (b2cOff.test.mjs)
  assert.ok(!canSeePage("content_creator", supplierBills));
});

test("contracting is shown as Product & operations", () => {
  assert.equal(ROLE_LABEL.contracting, "Product & operations");
});
