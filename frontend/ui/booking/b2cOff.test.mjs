// ui/booking/b2cOff.test.mjs — AA-1034 R0 (root ADR 0019 d8, B2B-only model): with NEXT_PUBLIC_B2C_INTAKE unset the
// staff admin shows no B2C intake entry point (New lead, New trip case for a customer, the New inquiries tile, the
// new-case target of "Use for trip case"). Source guards: each entry point must stay behind B2C_INTAKE_OPEN.
// Run: node --test ui/booking/*.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const read = (rel) => readFileSync(ROOT + rel, "utf8");

test("the flag is off unless NEXT_PUBLIC_B2C_INTAKE is exactly on", () => {
  const src = read("lib/config.ts");
  assert.match(src, /export const B2C_INTAKE_OPEN = process\.env\.NEXT_PUBLIC_B2C_INTAKE === "on";/);
});

const GUARDED = [
  ["app/(staff)/page.tsx", /B2C_INTAKE_OPEN && \(\s*<TileLink href=\{casesHref\(\["new_inquiry"\]\)\}/],
  ["app/(staff)/customers/page.tsx", /B2C_INTAKE_OPEN &&\s*canWrite && \(\s*<Button variant="primary" onClick=\{\(\) => setNewLead\(true\)\}>/],
  ["app/(staff)/customers/page.tsx", /if \(B2C_INTAKE_OPEN && canCreateCase && !c\.is_blocked\) \{\s*items\.push\(\{ label: "New trip case"/],
  ["app/(staff)/customers/[id]/page.tsx", /\{B2C_INTAKE_OPEN && canCreateCase && !c\.is_blocked && \(\s*<Link\s+href=\{newCaseHref\}/],
  ["app/(staff)/cases/page.tsx", /B2C_INTAKE_OPEN && canWrite \? \(\s*<Link\s+href="\/cases\/new"/],
  ["app/(staff)/catalog/trip-plans/_parts/UseForTripCaseModal.tsx", /\{B2C_INTAKE_OPEN && \(\s*<SegmentedControl/],
];

for (const [file, guard] of GUARDED) {
  test(`${file}: the B2C entry point is behind B2C_INTAKE_OPEN`, () => {
    assert.match(read(file), guard);
  });
}

test("the new-case page answers a notice, not the form, while the intake is off", () => {
  const src = read("app/(staff)/cases/new/page.tsx");
  const notice = src.indexOf("if (!B2C_INTAKE_OPEN)");
  assert.ok(notice > 0 && notice < src.indexOf("<form"), "the notice must return before the form renders");
  assert.match(src, /title="Trip cases come from partner agencies"/);
});

test("Use for trip case defaults to an existing case while the intake is off", () => {
  const src = read("app/(staff)/catalog/trip-plans/_parts/UseForTripCaseModal.tsx");
  assert.match(src, /useState<Target>\(B2C_INTAKE_OPEN \? "new_case" : "existing_case"\)/);
});
