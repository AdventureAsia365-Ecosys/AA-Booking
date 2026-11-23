// lib/paxBands.test.mjs — AA-1175 (bk-066): Pricing > Policy shows the group-size bands as "1, 2, 5–7, 11+ travellers"
// and reads them back from the text staff type ("5-7", "5–7", "11+"), refusing text that is not bands.
// Run: npm run test:unit.
import assert from "node:assert/strict";
import { test } from "node:test";

import { bandLabel, bandsToText, formatBands, textToBands } from "./paxBands.ts";

const OWNER = [[1, 1], [2, 2], [3, 3], [4, 4], [5, 7], [8, 10], [11, null]];

test("the bands read as staff see them, with the translated unit", () => {
  assert.equal(formatBands(OWNER, "travellers"), "1, 2, 3, 4, 5–7, 8–10, 11+ travellers");
  assert.equal(formatBands([[1, null]], "khách"), "1+ khách");
  assert.equal(formatBands([[1, 2], [3, 10]], null), "1–2, 3–10");
  assert.equal(bandLabel([5, 7]), "5–7");
});

test("editor text round-trips, with a plain hyphen or an en dash", () => {
  assert.equal(bandsToText(OWNER), "1, 2, 3, 4, 5-7, 8-10, 11+");
  assert.deepEqual(textToBands(bandsToText(OWNER)), OWNER);
  assert.deepEqual(textToBands(" 1 , 2, 3 – 4,5—7 , 8 + "), [[1, 1], [2, 2], [3, 4], [5, 7], [8, null]]);
  assert.deepEqual(textToBands("1, 2, 3-10"), [[1, 1], [2, 2], [3, 10]]);
});

test("text that is not bands is refused (the API checks gaps, overlaps and the range)", () => {
  for (const bad of ["", " , ", "1, two", "1, 2-", "1, -4", "1.5", "1, 5+, 8", "1; 2", "1-2-3"]) {
    assert.equal(textToBands(bad), null, bad);
  }
  assert.deepEqual(textToBands("1, 3"), [[1, 1], [3, 3]]); // a gap is the API's refusal, with its reason
});
