// ui/booking/phrasebook.test.mjs — AA-1128 (B5): the staff app's phrasebook (ui/local/phrasebook.ts), which shows
// English text kept in module-level tables and built by import-free helpers in the UI language.
// Run: node --test ui/booking/phrasebook.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { phrase, phrasebook, phrasebookSize } from "../local/phrasebook.ts";

const fn = (s) => (...a) => s.replace(/\{(\d)\}/g, (_, i) => String(a[Number(i)]));
phrasebook({
  en: {
    Pending: "Pending",
    "{0} days / {1} nights": fn("{0} days / {1} nights"),
    "{0} adults": fn("{0} adults"),
    "{0} adults, {1} children": fn("{0} adults, {1} children"),
    "from {0}": fn("from {0}"),
    "Showing {0} rows{1}": fn("Showing {0} rows{1}"),
    "{0}: {1}": fn("{0}: {1}"),
  },
  vi: {
    Pending: "Chờ duyệt",
    "{0} days / {1} nights": fn("{0} ngày / {1} đêm"),
    "{0} adults": fn("{0} người lớn"),
    "{0} adults, {1} children": fn("{0} người lớn, {1} trẻ em"),
    "from {0}": fn("từ {0}"),
    "Showing {0} rows{1}": fn("Đang hiện {0} dòng"),
    "{0}: {1}": fn("{0}: {1}"),
  },
});
// A second catalogue never overrides the first one's text for the same key.
phrasebook({ en: { Pending: "Pending" }, vi: { Pending: "Đang chờ" } });

test("English, null and empty text pass through unchanged", () => {
  assert.equal(phrase("Pending", "en"), "Pending");
  assert.equal(phrase(null, "vi"), null);
  assert.equal(phrase(undefined, "vi"), undefined);
  assert.equal(phrase("", "vi"), "");
});

test("a registered string is translated exactly; the first catalogue wins", () => {
  assert.equal(phrase("Pending", "vi"), "Chờ duyệt");
  assert.equal(phrase("Pending approval", "vi"), "Pending approval");
});

test("a function message matches as a pattern when every filled-in part is a number or an amount", () => {
  assert.equal(phrase("7 days / 6 nights", "vi"), "7 ngày / 6 đêm");
  assert.equal(phrase("2 adults, 3 children", "vi"), "2 người lớn, 3 trẻ em");
  assert.equal(phrase("2 adults", "vi"), "2 người lớn");
  assert.equal(phrase("from $1,250.50", "vi"), "từ $1,250.50");
  assert.equal(phrase("from VND 1.250.000", "vi"), "từ VND 1.250.000");
  assert.equal(phrase("from 1,250 USD", "vi"), "từ 1,250 USD");
});

test("data that only looks like a pattern stays as it is", () => {
  assert.equal(phrase("Vietnam 7 days / 6 nights", "vi"), "Vietnam 7 days / 6 nights");
  assert.equal(phrase("from Hanoi", "vi"), "from Hanoi");
  assert.equal(phrase("Many adults", "vi"), "Many adults");
});

test("a Vietnamese template may drop an English-only argument (plural endings)", () => {
  assert.equal(phrase("Showing 12 rows", "vi"), "Showing 12 rows"); // the empty filler is not a number
});

test("a pattern with fewer than three letters of literal text is never registered", () => {
  assert.equal(phrase("12: 30", "vi"), "12: 30");
  assert.ok(phrasebookSize().patterns >= 4);
});

test("a filler may be currency codes", () => {
  phrasebook({ en: { "Missing FX rate: {0}": fn("Missing FX rate: {0}") }, vi: { "Missing FX rate: {0}": fn("Thiếu tỷ giá: {0}") } });
  assert.equal(phrase("Missing FX rate: EUR", "vi"), "Thiếu tỷ giá: EUR");
  assert.equal(phrase("Missing FX rate: EUR, GBP", "vi"), "Thiếu tỷ giá: EUR, GBP");
  assert.equal(phrase("Missing FX rate: Euro", "vi"), "Missing FX rate: Euro");
});

test("a filler may be an ISO day", () => {
  phrasebook({ en: { "Group departure {0}": fn("Group departure {0}") }, vi: { "Group departure {0}": fn("Chuyến khởi hành đoàn {0}") } });
  assert.equal(phrase("Group departure 2026-12-01", "vi"), "Chuyến khởi hành đoàn 2026-12-01");
});
