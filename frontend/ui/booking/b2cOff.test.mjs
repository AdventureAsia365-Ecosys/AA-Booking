// ui/booking/b2cOff.test.mjs — AA-1046 (root ADR 0019 d8, B2B-only model; AA-1034 R0 before it): the B2C half of
// Booking is retired. The staff admin has no B2C entry point left (no New lead, no staff-made trip case for a
// customer, no New inquiries tile, no new-case target of "Use for trip case", no Send to the customer) and no page
// calls a retired API route (customers, customer invoices and payments, the billing summary, Send). Source guards.
// Run: node --test ui/booking/*.test.mjs
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const read = (rel) => readFileSync(ROOT + rel, "utf8");

function filesUnder(rel) {
  return readdirSync(ROOT + rel, { recursive: true })
    .map((f) => `${rel}/${String(f).replaceAll("\\", "/")}`)
    .filter((f) => /\.(tsx?|mjs)$/.test(f));
}

test("the B2C part is closed for good: no build switch is read", () => {
  const src = read("lib/config.ts");
  assert.match(src, /export const B2C_INTAKE_OPEN = false;/);
  for (const file of [...filesUnder("app"), ...filesUnder("ui"), ...filesUnder("lib")]) {
    assert.doesNotMatch(read(file), /process\.env\.NEXT_PUBLIC_B2C_INTAKE/, file);
  }
});

test("the retired pages are gone", () => {
  for (const page of [
    "app/(staff)/customers/page.tsx",
    "app/(staff)/customers/[id]/page.tsx",
    "app/(staff)/cases/new/page.tsx",
    "app/(staff)/billing/invoices/[number]/page.tsx",
    "app/(staff)/billing/payments/[ref]/page.tsx",
    "app/(staff)/cases/[code]/BillingTab.tsx",
    "app/(staff)/proposals/_parts/SendProposalModal.tsx",
  ]) {
    assert.equal(existsSync(ROOT + page), false, page);
  }
});

// The API routes AA-1046 removed (backend app/routers/customers.py, billing.py, customer.py; versions …/send).
const RETIRED_CALLS = [
  /bk(?:Get|Send)<[^>]*>\(\s*["`]customers/,
  /bk(?:Get|Send)<[^>]*>\(\s*["`]invoices/,
  /bk(?:Get|Send)<[^>]*>\(\s*["`]payments/,
  /["`]billing\/summary["`]/,
  /\/billing["`]\)/,
  /\$\{versionPath\([^)]*\)\}\/send`/,
  /bkSend<TripCaseDetail>\("trip-cases", \{ body \}\)/,
];

test("no client call reaches a retired route", () => {
  for (const file of [...filesUnder("lib"), ...filesUnder("app"), ...filesUnder("ui")]) {
    const src = read(file);
    for (const call of RETIRED_CALLS) assert.doesNotMatch(src, call, `${file}: ${call}`);
  }
});

test("no entry point offers a new case for a customer", () => {
  assert.doesNotMatch(read("app/(staff)/cases/page.tsx"), /href="\/cases\/new"/);
  assert.doesNotMatch(read("app/(staff)/page.tsx"), /label="New inquiries"/);
  const useModal = read("app/(staff)/catalog/trip-plans/_parts/UseForTripCaseModal.tsx");
  assert.doesNotMatch(useModal, /new_customer|customer_id|CustomerPicker/);
  assert.match(useModal, /tripPlansApi\.useForTripCase\(plan\.source_id, \{ trip_case: caseCode\.trim\(\)\.toUpperCase\(\) \}\)/);
});

test("no page links to a retired page", () => {
  for (const file of [...filesUnder("app"), ...filesUnder("ui")]) {
    if (file.endsWith(".test.mjs")) continue; // the guards themselves name what they look for
    const src = read(file);
    assert.doesNotMatch(src, /[`"]\/customers[/`"?]/, file);
    assert.doesNotMatch(src, /\/billing\/(invoices|payments)\//, file);
    assert.doesNotMatch(src, /Send to customer/, file);
  }
});
