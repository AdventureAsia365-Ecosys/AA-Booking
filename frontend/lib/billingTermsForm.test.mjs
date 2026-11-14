// lib/billingTermsForm.test.mjs — AA-1096 (S280 settings UI audit): the payment terms form keeps the typed deposit
// (no float noise), refuses empty and out-of-range day counts, and checks suspend > warn. Run: npm run test:unit.
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseTerms, pctText, termsToText } from "./billingTermsForm.ts";

const TERMS = {
  deposit_pct: "0.0700",
  balance_days_before_departure: 30,
  saas_due_days: 14,
  settlement_due_days: 7,
  warn_after_days: 7,
  suspend_after_days: 30,
  saas_currency: "USD",
};

test("the deposit reads and saves without float noise", () => {
  assert.equal(pctText("0.0700"), "7");
  assert.equal(pctText("0.2900"), "29");
  assert.equal(pctText("0.5800"), "58");
  assert.equal(pctText("0.1250"), "12.5");
  const text = termsToText(TERMS);
  assert.equal(text.deposit, "7");
  assert.equal(parseTerms({ ...text, deposit: "29" }).body.deposit_pct, "0.2900");
  assert.equal(parseTerms({ ...text, deposit: "12,5" }).body.deposit_pct, "0.1250");
});

test("every day count is required, whole and inside the API range; suspend after warn", () => {
  const text = termsToText(TERMS);
  assert.deepEqual(parseTerms(text).errors, {});
  assert.equal(parseTerms(text).body.saas_due_days, 14);
  assert.equal(parseTerms({ ...text, saas_due_days: "" }).errors.saas_due_days, "Required.");
  assert.equal(parseTerms({ ...text, saas_due_days: "" }).body, null);
  assert.match(parseTerms({ ...text, settlement_due_days: "121" }).errors.settlement_due_days, /0–120/);
  assert.match(parseTerms({ ...text, balance_days_before_departure: "366" }).errors.balance_days_before_departure, /0–365/);
  assert.equal(parseTerms({ ...text, warn_after_days: "1.5" }).errors.warn_after_days, "Whole days only.");
  assert.match(parseTerms({ ...text, suspend_after_days: "7" }).errors.suspend_after_days, /more than the warning/);
  assert.ok(parseTerms({ ...text, deposit: "101" }).errors.deposit);
  assert.ok(parseTerms({ ...text, deposit: "" }).errors.deposit);
});
