// lib/policyGuide.test.mjs — AA-1096 l23: Pricing > Policy explains every parameter (the guide in the page's language,
// the direction words by value type) and a decimal takes a comma or a point. Run: npm run test:unit.
import assert from "node:assert/strict";
import { test } from "node:test";

import { decimalText, directionWords, guideFor, searchParams } from "./policyGuide.ts";

test("a decimal takes a comma or a point; the API gets a point", () => {
  assert.equal(decimalText("2,5"), "2.5");
  assert.equal(decimalText(" 3.25 "), "3.25");
  assert.equal(decimalText("5"), "5");
  assert.equal(decimalText("3,364"), null);
  assert.equal(decimalText("1.000,5"), null);
  assert.equal(decimalText("abc"), null);
});

test("the guide in the page's language, else English; the directions by value type", () => {
  const en = { what: "w", raise: "r", lower: "l", when: "n", example: "e" };
  const vi = { what: "vw", raise: "vr", lower: "vl", when: "vn", example: "ve" };
  assert.equal(guideFor({ guide: { en, vi } }, "vi"), vi);
  assert.equal(guideFor({ guide: { en, vi } }, "en"), en);
  assert.equal(guideFor({ guide: { en } }, "vi"), en);
  assert.equal(guideFor({ guide: null }, "en"), null);
  assert.equal(guideFor({}, "vi"), null);
  assert.equal(directionWords("integer"), "number");
  assert.equal(directionWords("decimal"), "number");
  assert.equal(directionWords("boolean"), "switch");
  assert.equal(directionWords("month_day"), "date");
  assert.equal(directionWords("amount_map"), "choice");
  assert.equal(directionWords("codes"), "choice");
});

// AA-1096 (S280 UX fix round): one search over every section; the key and the guide text match, not only the label.
test("the page search matches key, label, help and guide in either language; every word must match", () => {
  const g = (s) => ({ what: `${s} what`, raise: "r", lower: "l", when: "w", example: "e" });
  const params = [
    { key: "booking.hold.hold_hours", help: "How long a hold lasts.", guide: { en: g("Hold"), vi: g("Giữ chỗ") }, section: "holds" },
    { key: "booking.ops.reminder_days", help: "Days before a reminder.", guide: null, section: "operations" },
  ];
  const label = (p) => (p.key.endsWith("hold_hours") ? "Hold length" : "Reminder lead time");
  assert.equal(searchParams(params, "  ", label), null);
  assert.deepEqual(searchParams(params, "hold_hours", label).map((p) => p.key), ["booking.hold.hold_hours"]);
  assert.deepEqual(searchParams(params, "REMINDER lead", label).map((p) => p.key), ["booking.ops.reminder_days"]);
  assert.deepEqual(searchParams(params, "giữ chỗ", label).map((p) => p.key), ["booking.hold.hold_hours"]);
  assert.deepEqual(searchParams(params, "hold reminder", label), []);
});
