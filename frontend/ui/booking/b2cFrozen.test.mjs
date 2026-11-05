// ui/booking/b2cFrozen.test.mjs — AA-1037 (root ADR 0019 d8, the AA-1034 R0 follow-up): while the B2C part is closed
// (NEXT_PUBLIC_B2C_INTAKE unset) the staff admin offers no B2C write, and the nav leads with the B2B pages.
//   1. b2cFrozen.ts: the permissions taken out everywhere and inside a b2c case; the billing read mask;
//   2. navMatch.ts: Partners (Agency billing > Agencies) and Settlement (the rest of Agency billing) on one page;
//   3. source guards: the layout, the case page and the billing client use them; the nav order; the phone bottom nav.
// Run: npm run test:unit (node --test ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const read = (rel) => readFileSync(ROOT + rel, "utf8");
const frozen = await import(pathToFileURL(ROOT + "ui/booking/b2cFrozen.ts").href);
const match = await import(pathToFileURL(ROOT + "ui/local/navMatch.ts").href);

const ADMIN = ["billing.read", "billing.write", "payment.verify", "customer.read", "customer.write", "customer.admin",
  "trip_case.read", "trip_case.write", "trip_case.assign", "version.write", "ops.read", "ops.write", "bill.write",
  "agency_billing.write"];

test("closed: the B2C-only writes leave the staff profile; open: nothing changes", () => {
  const closed = frozen.withoutB2cWrites(ADMIN, false);
  for (const p of ["billing.write", "payment.verify", "customer.write", "customer.admin"]) assert.ok(!closed.includes(p), p);
  for (const p of ["billing.read", "customer.read", "trip_case.write", "ops.write", "bill.write", "agency_billing.write"]) {
    assert.ok(closed.includes(p), p);
  }
  assert.deepEqual(frozen.withoutB2cWrites(ADMIN, true), ADMIN);
});

test("a b2c case also loses the case, version and operations writes; a B2B case keeps them", () => {
  const b2c = frozen.casePermissions(ADMIN, "b2c", false);
  for (const p of ["trip_case.write", "trip_case.assign", "version.write", "ops.write", "billing.write"]) {
    assert.ok(!b2c.includes(p), p);
  }
  assert.ok(b2c.includes("trip_case.read") && b2c.includes("bill.write"));
  const b2b = frozen.casePermissions(ADMIN, "b2b", false);
  for (const p of ["trip_case.write", "trip_case.assign", "version.write", "ops.write"]) assert.ok(b2b.includes(p), p);
  assert.deepEqual(frozen.casePermissions(ADMIN, "b2c", true), ADMIN);
  assert.equal(frozen.isFrozenCase("b2c", false), true);
  assert.equal(frozen.isFrozenCase("b2b", false), false);
  assert.equal(frozen.isFrozenCase("b2c", true), false);
});

test("the billing read mask turns every action off and closes the case invoice", () => {
  const invoice = { invoice_number: "INV-000001", actions: { send: true, edit: true }, payments: [{ actions: { verify: true } }] };
  const out = frozen.frozenView({ invoice, invoiceable: true, reason: null, history: [] });
  assert.deepEqual(out.invoice.actions, { send: false, edit: false });
  assert.deepEqual(out.invoice.payments[0].actions, { verify: false });
  assert.equal(out.invoiceable, false);
  assert.equal(out.reason, "b2b_only");
  assert.equal(invoice.actions.send, true, "the input is not modified");
  assert.deepEqual(frozen.frozenView({ invoiceable: false, reason: "no_approved_version" }), {
    invoiceable: false,
    reason: "no_approved_version",
  });
  assert.deepEqual(frozen.frozenView({ items: [{ actions: { void: true } }], total: 1 }), {
    items: [{ actions: { void: false } }],
    total: 1,
  });
  assert.doesNotMatch(frozen.B2C_READ_ONLY_NOTE, /AA-\d/);
});

test("Partners is the Agencies tab of Agency billing, Settlement the rest of it", () => {
  const partners = { href: "/agency-billing?tab=agencies", tab: "agencies" };
  const settlement = { href: "/agency-billing", notTabs: ["agencies"] };
  assert.equal(match.hrefPath(partners.href), "/agency-billing");
  assert.equal(match.navActive(partners, "/agency-billing", "agencies"), true);
  assert.equal(match.navActive(settlement, "/agency-billing", "agencies"), false);
  for (const tab of [null, "invoices", "payments", "settings"]) {
    assert.equal(match.navActive(partners, "/agency-billing", tab), false, String(tab));
    assert.equal(match.navActive(settlement, "/agency-billing", tab), true, String(tab));
  }
  // An invoice page belongs to Settlement whatever its tab key.
  assert.equal(match.navActive(settlement, "/agency-billing/invoices/AAS-2026-000001", "agencies"), true);
  assert.equal(match.navActive(partners, "/agency-billing/invoices/AAS-2026-000001", "agencies"), false);
  assert.equal(match.navActive({ href: "/" }, "/cases"), false);
  assert.equal(match.navActive({ href: "/cases" }, "/cases/TC-000001"), true);
});

test("the layout, the case page and the billing client apply the freeze", () => {
  assert.match(read("app/(staff)/layout.tsx"), /withoutB2cWrites\(me\.staff\.permissions, B2C_INTAKE_OPEN\)/);
  assert.match(
    read("app/(staff)/cases/[code]/page.tsx"),
    /<StaffProvider staff=\{\{ \.\.\.staff, permissions: casePermissions\(staff\.permissions, c\.channel, B2C_INTAKE_OPEN\) \}\}>/,
  );
  const billing = read("lib/api/billing.ts");
  for (const call of ["invoices", "invoice", "forCase", "payments", "payment"]) {
    assert.match(billing, new RegExp(`\\b${call}: \\([^)]*\\)[^=]*=> b2cRead\\(bkGet<`), call);
  }
});

test("the nav leads with B2B and moves the B2C records to a read-only group while closed", () => {
  const nav = read("ui/local/nav.tsx");
  const order = ['label: "Overview"', "B2B_GROUP,", 'label: "Sales"', 'label: "Operations"', 'label: "Supply"',
    'label: "Catalog (from CIS)"', 'label: "B2C records (read-only)"', 'label: "Admin"'];
  let at = -1;
  for (const marker of order) {
    const i = nav.indexOf(marker, at + 1);
    assert.ok(i > at, `${marker} out of order`);
    at = i;
  }
  assert.match(nav, /label: "Partners",[\s\S]*?tab: "agencies"/);
  assert.match(nav, /label: "Settlement",[^\n]*notTabs: \["agencies"\]/);
  assert.match(nav, /export const NAV_GROUPS: NavGroup\[\] = navGroups\(B2C_INTAKE_OPEN\);/);
});

test("the phone bottom nav offers the B2B desk instead of Customers while closed", () => {
  const bottom = read("ui/local/BottomNav.tsx");
  assert.match(
    bottom,
    /B2C_INTAKE_OPEN\s*\?\s*\{ href: "\/customers", label: "Customers", icon: Users \}\s*:\s*\{ href: "\/b2b-desk", label: "B2B desk", icon: Handshake \}/,
  );
});
