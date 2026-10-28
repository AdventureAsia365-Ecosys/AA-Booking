// ui/booking/caseFacts.test.mjs — AA-996 audit slice 4: the case workspace / trip-check readers.
// Run: node --test ui/booking/caseFacts.test.mjs (Node >= 22.18 strips the TypeScript types of caseFacts.ts itself;
// `npm run test:unit` runs every ui/booking/*.test.mjs once the audit slice 1 script is on main).
import assert from "node:assert/strict";
import { test } from "node:test";
import { caseCountries, checkProgress, inclusiveDays, inquiryPrefs } from "./caseFacts.ts";

test("inquiryPrefs is null when the snapshot carries only the route", () => {
  assert.equal(inquiryPrefs(null), null);
  assert.equal(inquiryPrefs({ itinerary: { legs: [], days: [{ day: 1, name: "Pak Ou Caves" }], custom_days: [] } }), null);
  assert.equal(inquiryPrefs({ message: "   ", budget: null }), null);
});

test("inquiryPrefs reads the v1.2 fields wherever the snapshot groups them", () => {
  const top = inquiryPrefs({
    message: "  We travel slowly and love food markets.  ",
    budget: { min: 3000, max: 4500, currency: "USD" },
    travel_window: { start: "2027-03-02", end: "2027-03-20" },
    travel_style: "Boutique",
    pax: { adults: 2, children: 1 },
  });
  assert.deepEqual(top, {
    message: "We travel slowly and love food markets.",
    budget: "3,000 – 4,500 USD",
    travelWindow: { start: "2027-03-02", end: "2027-03-20", text: null },
    travelStyle: "Boutique",
    party: "2 adults, 1 child",
  });
  const nested = inquiryPrefs({ preferences: { budget: { amount: "2500", currency: "EUR", per_person: true } }, trip: { start_date: "2027-01-05T00:00:00Z" } });
  assert.equal(nested?.budget, "2,500 EUR per person");
  assert.deepEqual(nested?.travelWindow, { start: "2027-01-05", end: null, text: null });
  assert.equal(nested?.message, null);
  assert.equal(inquiryPrefs({ travel_window: "Early spring, flexible" })?.travelWindow?.text, "Early spring, flexible");
  assert.equal(inquiryPrefs({ pax: 3 })?.party, "3 travellers");
  assert.equal(inquiryPrefs({ budget: 1200 })?.budget, "1,200");
  assert.equal(inquiryPrefs({ budget: { max: 900, currency: "USD" } })?.budget, "up to 900 USD");
});

test("caseCountries prefers what staff set and falls back to the legs", () => {
  assert.deepEqual(caseCountries({ countries: ["VN"], leg_countries: ["LA"] }), { codes: ["VN"], fromLegs: false });
  assert.deepEqual(caseCountries({ countries: [], leg_countries: ["LA", "VN"] }), { codes: ["LA", "VN"], fromLegs: true });
  assert.deepEqual(caseCountries({ countries: [] }), { codes: [], fromLegs: false });
});

test("inclusiveDays counts both ends and refuses bad input", () => {
  assert.equal(inclusiveDays("2026-11-12", "2026-11-26"), 15);
  assert.equal(inclusiveDays("2026-12-31", "2027-01-01"), 2);
  assert.equal(inclusiveDays("2026-11-12", "2026-11-12"), 1);
  assert.equal(inclusiveDays("2026-11-12", "2026-11-01"), null);
  assert.equal(inclusiveDays(null, "2026-11-01"), null);
  assert.equal(inclusiveDays("12/11/2026", "2026-11-26"), null);
});

test("checkProgress counts not-applicable checks as resolved", () => {
  assert.deepEqual(checkProgress({ total: 9, done: 2, not_applicable: 1 }), { resolved: 3, total: 9, pct: 33, label: "3 of 9 checks done" });
  assert.deepEqual(checkProgress({ total: 3, done: 3, not_applicable: 0 }), { resolved: 3, total: 3, pct: 100, label: "3 of 3 checks done" });
  assert.equal(checkProgress({ total: 0, done: 0, not_applicable: 0 }), null);
  assert.equal(checkProgress(undefined), null);
});
