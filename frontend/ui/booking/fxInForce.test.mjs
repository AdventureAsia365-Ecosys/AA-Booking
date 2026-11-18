// ui/booking/fxInForce.test.mjs — AA-1128: the currencies that can price today (USD + FX rows in force).
// Run: node --test ui/booking/fxInForce.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { currenciesInForce, currencyOptions } from "./fxInForce.ts";

test("USD first, then each currency with an FX row in force, once, sorted", () => {
  const rows = [
    { currency: "VND", status: "in_force" },
    { currency: "EUR", status: "pending" },
    { currency: "AUD", status: "in_force" },
    { currency: "VND", status: "superseded" },
    { currency: "GBP", status: "scheduled" },
    { currency: "USD", status: "in_force" },
  ];
  assert.deepEqual(currenciesInForce(rows), ["USD", "AUD", "VND"]);
  assert.deepEqual(currenciesInForce([]), ["USD"]);
});

test("a currency without an FX rate today is not usable; unknown FX keeps every option", () => {
  assert.deepEqual(currencyOptions(["USD", "EUR", "AUD"], ["USD", "AUD"]), [
    { code: "USD", usable: true },
    { code: "EUR", usable: false },
    { code: "AUD", usable: true },
  ]);
  assert.ok(currencyOptions(["USD", "EUR"], null).every((o) => o.usable));
  // the value already chosen stays listed even when the candidates do not hold it
  assert.deepEqual(currencyOptions(["USD"], ["USD"], "THB"), [
    { code: "USD", usable: true },
    { code: "THB", usable: false },
  ]);
});
