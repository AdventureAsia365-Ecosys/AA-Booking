// ui/booking/b2cFrozen.test.mjs — AA-1037 / AA-1046 (root ADR 0019 d8): the B2C half of Booking is retired, a b2c
// case is read-only history, the staff admin offers no B2C write, and the nav leads with the B2B pages.
//   1. b2cFrozen.ts: the permissions taken out everywhere and inside a b2c case;
//   2. navMatch.ts: Partners (Agency billing > Agencies) and Settlement (the rest of Agency billing) on one page;
//   3. source guards: the staff layout and the case layout use them; every other page that offers a case, version or
//      operations write gates it per case; the nav order; the phone bottom nav.
// Run: npm run test:unit (node --test ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
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

test("the read-only note names no internal id", () => {
  assert.doesNotMatch(frozen.B2C_READ_ONLY_NOTE, /AA-\d/);
  assert.equal(frozen.frozenView, undefined, "the billing read mask went with the customer invoices (AA-1046)");
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

test("the layout and the case layout apply the freeze", () => {
  assert.match(read("app/(staff)/layout.tsx"), /withoutB2cWrites\(me\.staff\.permissions, B2C_INTAKE_OPEN\)/);
  // Every route under /cases/{code} (workspace, proposal builder, preview) sits inside this provider.
  assert.match(
    read("app/(staff)/cases/[code]/layout.tsx"),
    /<StaffProvider staff=\{\{ \.\.\.staff, permissions: casePermissions\(staff\.permissions, q\.data\?\.channel, B2C_INTAKE_OPEN\) \}\}>/,
  );
  // No page below it hands out its own staff profile again (that would drop the case mask).
  for (const file of filesUnder("app/(staff)/cases/[code]")) {
    if (file.endsWith("/layout.tsx")) continue;
    assert.doesNotMatch(read(file), /<StaffProvider\b/, file);
  }
  assert.match(read("lib/config.ts"), /export const B2C_INTAKE_OPEN = false;/);
});

// Each page outside /cases/{code} that reads a case, version or operations write permission gates it itself: per case
// (isFrozenCase on the row's or check's channel) or for the whole page (B2C_INTAKE_OPEN: it only creates b2c cases).
// A new entry point fails here until it is gated and listed.
const CASE_WRITES = /useCan\("(trip_case\.write|trip_case\.assign|version\.write|ops\.write)"\)/;
const GATED_ELSEWHERE = {
  "app/(staff)/proposals/page.tsx": /isFrozenCase\(row\.trip_case_channel, B2C_INTAKE_OPEN\)[\s\S]*isFrozenCase\(c\.channel, B2C_INTAKE_OPEN\)/,
  "app/(staff)/trip-checks/_parts/CheckDrawer.tsx": /useCan\("ops\.write"\) && writable && !frozen/,
  "app/(staff)/catalog/trip-plans/_parts/TripPlanDetailView.tsx": /UseForTripCaseModal/,
};

function filesUnder(rel) {
  return readdirSync(ROOT + rel, { recursive: true })
    .map((f) => `${rel}/${String(f).replaceAll("\\", "/")}`)
    .filter((f) => /\.tsx?$/.test(f));
}

test("every case, version or operations write outside a case page is gated per case or for the whole page", () => {
  assert.match(read("lib/api/types.ts"), /trip_case_channel: TripCaseChannel;/);
  assert.doesNotMatch(read("app/(staff)/catalog/trip-plans/_parts/UseForTripCaseModal.tsx"), /new_case|CustomerPicker/);
  const found = [];
  for (const file of [...filesUnder("app"), ...filesUnder("ui")]) {
    if (file.startsWith("app/(staff)/cases/[code]/") || !CASE_WRITES.test(read(file))) continue;
    found.push(file);
    assert.ok(file in GATED_ELSEWHERE, `${file} offers a case write: gate it with isFrozenCase / B2C_INTAKE_OPEN and list it`);
    assert.match(read(file), GATED_ELSEWHERE[file], file);
  }
  assert.deepEqual(found.sort(), Object.keys(GATED_ELSEWHERE).sort());
});

test("the nav leads with B2B; no B2C records group, Operations lists the supplier bills", () => {
  const nav = read("ui/local/nav.tsx");
  const order = ['label: "Overview"', "B2B_GROUP,", 'label: "Sales"', 'label: "Operations"', "SUPPLIER_BILLS,",
    'label: "Supply"', 'label: "Catalog (from CIS)"', 'label: "Admin"'];
  let at = -1;
  for (const marker of order) {
    const i = nav.indexOf(marker, at + 1);
    assert.ok(i > at, `${marker} out of order`);
    at = i;
  }
  assert.match(nav, /label: "Partners",[\s\S]*?tab: "agencies"/);
  assert.match(nav, /label: "Settlement",[^\n]*notTabs: \["agencies"\]/);
  assert.match(nav, /export const NAV_GROUPS: NavGroup\[\] = navGroups\(\);/);
  assert.doesNotMatch(nav, /B2C records|"\/customers"|Payments & Invoices/);
});

test("the phone bottom nav offers the B2B desk (Customers is retired)", () => {
  const bottom = read("ui/local/BottomNav.tsx");
  assert.match(bottom, /\{ href: "\/b2b-desk", label: "B2B desk", icon: Handshake \}/);
  assert.doesNotMatch(bottom, /\/customers/);
});
