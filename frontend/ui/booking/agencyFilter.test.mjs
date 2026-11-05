// ui/booking/agencyFilter.test.mjs — AA-1037: the `tenant` deep link of Agency billing → Invoices (CIS Tenant 360
// "Open in Booking") and the name its filter chip shows.
// Run: npm run test:unit (Node 22 strips the TypeScript types of agencyFilter.ts itself).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { agencyFilterLabel, tenantFilter } from "./agencyFilter.ts";

const ID = "5d1e7c3a-2b4f-4c6d-8e9f-0a1b2c3d4e5f";

test("a UUID passes through, lower-cased and trimmed", () => {
  assert.equal(tenantFilter(ID), ID);
  assert.equal(tenantFilter(` ${ID.toUpperCase()} `), ID);
});

test("missing or malformed values give no filter (the unfiltered list, never a 422)", () => {
  for (const raw of [null, undefined, "", "   ", "abc", `${ID}x`, ID.slice(0, 35), ID.replaceAll("-", ""), "' OR 1=1 --", `${ID},${ID}`]) {
    assert.equal(tenantFilter(raw), null, String(raw));
  }
});

test("the chip names the agency from the status rows first, then from a listed invoice, else a neutral label", () => {
  const status = [{ tenant_id: ID, agency_name: "Lotus Lantern Travel" }];
  const invoices = [{ party_tenant_id: ID, agency_name: "Lotus Lantern Travel (old name)" }];
  assert.equal(agencyFilterLabel(ID, status, invoices), "Lotus Lantern Travel");
  assert.equal(agencyFilterLabel(ID.toUpperCase(), undefined, invoices), "Lotus Lantern Travel (old name)");
  assert.equal(agencyFilterLabel(ID, [], []), "One agency");
  assert.equal(agencyFilterLabel(ID, undefined, undefined), "One agency");
});

test("the invoices tab reads `tenant` through tenantFilter and sends it as tenant_id", () => {
  const view = readFileSync(fileURLToPath(new URL("../../app/(staff)/agency-billing/_parts/AgencyBillingView.tsx", import.meta.url)), "utf8");
  assert.match(view, /const tenant = tenantFilter\(get\("tenant"\)\);/);
  assert.match(view, /agencyBillingApi\.invoices\(\{[^}]*tenant_id: tenant \?\? undefined/);
  assert.match(view, /set\("tenant", null\)/, "the chip removes the filter");
  const api = readFileSync(fileURLToPath(new URL("../../lib/api/agencyBilling.ts", import.meta.url)), "utf8");
  assert.match(api, /invoices: \(q: \{[^}]*tenant_id\?: string/);
});
