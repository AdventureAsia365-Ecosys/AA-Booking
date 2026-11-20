// lib/staffPicker.test.mjs — AA-1159 (S286 F4): the staff pickers call GET /booking/staff/directory only for a
// signed-in user holding staff.directory. Finance does not hold it, so on /cases it used to get a 403 and a console
// error; now the query is disabled and the picker shows disabled.
// Run: node --test lib/staffPicker.test.mjs (`npm run test:unit` runs every lib/*.test.mjs).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const { STAFF_DIRECTORY_PERMISSION, canPickStaff } = await import(new URL("./rbac.ts", import.meta.url).href);

const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");

test("only a holder of staff.directory may list the staff directory", () => {
  assert.equal(STAFF_DIRECTORY_PERMISSION, "staff.directory");
  assert.equal(canPickStaff(["trip_case.read", "staff.directory"]), true);
  // Finance (AA-1150): reads cases, prices and bills, but not the directory.
  assert.equal(canPickStaff(["trip_case.read", "pricing.read", "bill.read", "agency_billing.read"]), false);
  assert.equal(canPickStaff([]), false);
  assert.equal(canPickStaff(undefined), false);
  assert.equal(canPickStaff(null), false);
});

test("the API grants staff.directory to admin, sales and contracting, not finance", () => {
  const src = read("../../backend/app/auth/staff.py");
  const line = src.split("\n").find((l) => l.trimStart().startsWith('"staff.directory": frozenset('));
  assert.ok(line, "staff.directory row not found in backend app/auth/staff.py");
  for (const role of ["admin", "sales", "contracting"]) assert.ok(line.includes(`"${role}"`), role);
  assert.ok(!line.includes('"finance"'), "finance now holds staff.directory: the picker gate can be relaxed");
});

test("the directory query is gated on the permission and the picker disables without it", () => {
  const src = read("../app/(staff)/cases/_parts/StaffSelect.tsx");
  assert.match(src, /enabled:\s*canPickStaff\(me\.permissions\)/);
  assert.match(src, /disabled=\{!allowed \|\| directory\.isLoading\}/);
  // The /cases page reuses the same hook for its "Sales rep" filter, so it is gated too.
  assert.match(read("../app/(staff)/cases/page.tsx"), /useStaffDirectory\(\)/);
});
