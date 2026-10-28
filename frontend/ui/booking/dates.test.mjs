// ui/booking/dates.test.mjs — AA-996 audit slice 1: unit tests for the date / time text helpers.
// Run: npm run test:unit (Node's built-in runner; Node >= 22.18 strips the TypeScript types of dates.ts itself).
// TZ is pinned by the npm script (America/Los_Angeles) so a day that would shift west of Greenwich is caught.
import assert from "node:assert/strict";
import { test } from "node:test";
import { formatDay, isIsoDay, localizeIsoDates, normalizeTime, parseDayInput, parseTimeInput } from "./dates.ts";

test("formatDay shows the en-GB day without a time-zone shift", () => {
  assert.equal(formatDay("2026-10-09"), "9 Oct 2026");
  assert.equal(formatDay("2026-01-01"), "1 Jan 2026");
  assert.equal(formatDay("2026-12-31"), "31 Dec 2026");
  assert.equal(formatDay(""), "—");
  assert.equal(formatDay(null), "—");
  assert.equal(formatDay("2026-02-30"), "—");
  assert.equal(formatDay("not a date"), "—");
  // A timestamp: the viewer's day, as the kit formatDate (America/Los_Angeles under npm run test:unit).
  assert.equal(formatDay("2026-10-09T03:00:00Z"), "8 Oct 2026");
});

test("isIsoDay accepts only real calendar days", () => {
  assert.equal(isIsoDay("2028-02-29"), true);
  assert.equal(isIsoDay("2027-02-29"), false);
  assert.equal(isIsoDay("2026-1-5"), false);
});

test("parseDayInput reads day-first numbers, month names and ISO", () => {
  assert.equal(parseDayInput("28/10/2026"), "2026-10-28");
  assert.equal(parseDayInput("28-10-2026"), "2026-10-28");
  assert.equal(parseDayInput("28.10.26"), "2026-10-28");
  assert.equal(parseDayInput("5/1/2027"), "2027-01-05");
  assert.equal(parseDayInput("10/11/2026"), "2026-11-10"); // day first, never US order
  assert.equal(parseDayInput("28 Oct 2026"), "2026-10-28");
  assert.equal(parseDayInput("28 October 2026"), "2026-10-28");
  assert.equal(parseDayInput("9 Sept 2026"), "2026-09-09");
  assert.equal(parseDayInput("Oct 28, 2026"), "2026-10-28");
  assert.equal(parseDayInput("2026-10-28"), "2026-10-28");
  assert.equal(parseDayInput("  "), "");
});

test("parseDayInput refuses what is not a day", () => {
  assert.equal(parseDayInput("31/02/2026"), null);
  assert.equal(parseDayInput("13/13/2026"), null);
  assert.equal(parseDayInput("tomorrow"), null);
  assert.equal(parseDayInput("28 Foo 2026"), null);
});

test("parseTimeInput gives a 24-hour HH:MM", () => {
  assert.equal(parseTimeInput("14:05"), "14:05");
  assert.equal(parseTimeInput("14.05"), "14:05");
  assert.equal(parseTimeInput("14h05"), "14:05");
  assert.equal(parseTimeInput("1405"), "14:05");
  assert.equal(parseTimeInput("905"), "09:05");
  assert.equal(parseTimeInput("9"), "09:00");
  assert.equal(parseTimeInput("2:05 pm"), "14:05");
  assert.equal(parseTimeInput("2pm"), "14:00");
  assert.equal(parseTimeInput("12 am"), "00:00");
  assert.equal(parseTimeInput("12:30 PM"), "12:30");
  assert.equal(parseTimeInput(""), "");
  assert.equal(parseTimeInput("24:00"), null);
  assert.equal(parseTimeInput("14:75"), null);
  assert.equal(parseTimeInput("13 pm"), null);
  assert.equal(parseTimeInput("noon"), null);
});

test("normalizeTime trims stored seconds", () => {
  assert.equal(normalizeTime("14:00:00"), "14:00");
  assert.equal(normalizeTime("07:30"), "07:30");
  assert.equal(normalizeTime(null), "");
  assert.equal(normalizeTime("25:00"), "");
});

test("localizeIsoDates rewrites ISO days and instants inside text", () => {
  assert.equal(localizeIsoDates("Invoice INV-000004 next due 2026-11-06."), "Invoice INV-000004 next due 6 Nov 2026.");
  assert.equal(localizeIsoDates("Check due 2026-10-01 for TC-000023"), "Check due 1 Oct 2026 for TC-000023");
  assert.equal(localizeIsoDates("Departure 2026-12-01 14:30"), "Departure 1 Dec 2026, 14:30");
  assert.equal(localizeIsoDates("No date here"), "No date here");
  assert.equal(localizeIsoDates("Bad 2026-02-30 stays"), "Bad 2026-02-30 stays");
  // Ids are left alone (a UUID or a code that only looks like digits and dashes).
  const id = "Tour 2026abcd-1234-4000-8000-000000000001 retired";
  assert.equal(localizeIsoDates(id), id);
  assert.equal(localizeIsoDates("Ref 12026-10-01x"), "Ref 12026-10-01x");
  // An instant with a zone is shown in the viewer's zone (America/Los_Angeles under npm run test:unit).
  assert.equal(localizeIsoDates("Paid at 2026-10-27T09:30:00Z"), "Paid at 27 Oct 2026, 02:30");
});
