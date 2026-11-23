// lib/sellExceptionToast.test.mjs — AA-1149 review: after "Allow sale" the price readiness drawer tells the
// administrator what the server answered. An exception covers a night without a hotel (N1) or a supplier not active
// (N3) only, so a tour that also misses a rate or has an inactive room type, hotel or service stays "blocked": the toast
// must not claim agencies can buy it.
// Run: node --test lib/sellExceptionToast.test.mjs (`npm run test:unit` runs every lib/*.test.mjs).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { allowOutcome } from "../app/(staff)/pricing/readiness/_parts/operableText.ts";

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

test("a covered tour says agencies can buy it", () => {
  for (const status of ["exception", "warnings", "ready"]) {
    assert.deepEqual(allowOutcome(status), { tone: "success", key: "Agencies can buy this tour until the exception ends" });
  }
});

test("a tour still blocked says so, not that it is on sale", () => {
  const out = allowOutcome("blocked");
  assert.equal(out.tone, "info");
  assert.match(out.key, /still cannot buy this tour/);
});

test("the drawer toasts the returned status and sends a fixed English revoke reason", () => {
  const src = read("app/(staff)/pricing/readiness/_parts/SellExceptionDrawer.tsx");
  assert.match(src, /allowOutcome\(next\.status\)/);
  assert.match(src, /operabilityApi\.revoke\(tourKey, REVOKE_REASON\)/);
  assert.doesNotMatch(src, /revoke\(tourKey, t\(/);
  const messages = read("app/(staff)/pricing/readiness/_parts/SellExceptionDrawer.messages.ts");
  for (const key of ["Agencies can buy this tour until the exception ends", allowOutcome("blocked").key]) {
    assert.equal(messages.split(`"${key}"`).length - 1, 3, key); // English key + value, Vietnamese key
  }
});
