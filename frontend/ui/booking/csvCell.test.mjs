// ui/booking/csvCell.test.mjs — AA-996 review fix: the CSV formula guard.
// Run: npm run test:unit (Node's built-in runner strips the TypeScript types of csvCell.ts itself).
import assert from "node:assert/strict";
import { test } from "node:test";
import { csvCell, safeCsvColumns } from "./csvCell.ts";

test("csvCell neutralises cells a spreadsheet would run as a formula", () => {
  assert.equal(csvCell('=HYPERLINK("http://x","click")'), `'=HYPERLINK("http://x","click")`);
  assert.equal(csvCell("=cmd|' /C calc'!A0"), "'=cmd|' /C calc'!A0");
  assert.equal(csvCell("+1+1"), "'+1+1");
  assert.equal(csvCell("-2+3"), "'-2+3");
  assert.equal(csvCell("@SUM(A1)"), "'@SUM(A1)");
  assert.equal(csvCell("\t=1"), "'\t=1");
  assert.equal(csvCell("\r=1"), "'\r=1");
});

test("csvCell keeps plain text, numbers and amounts as they are", () => {
  assert.equal(csvCell("Nguyen Van A"), "Nguyen Van A");
  assert.equal(csvCell("a=b"), "a=b");
  assert.equal(csvCell("-50.00"), "-50.00");
  assert.equal(csvCell("-50.00 USD"), "-50.00 USD");
  assert.equal(csvCell("+12"), "+12");
  assert.equal(csvCell(-50), -50);
  assert.equal(csvCell(null), null);
  assert.equal(csvCell(undefined), undefined);
  assert.equal(csvCell(""), "");
});

test("safeCsvColumns guards every value and keeps the headers", () => {
  const cols = safeCsvColumns([{ header: "Customer", value: (r) => r.name }]);
  assert.equal(cols[0].header, "Customer");
  assert.equal(cols[0].value({ name: "=1+1" }), "'=1+1");
  assert.equal(cols[0].value({ name: "Ann" }), "Ann");
});
