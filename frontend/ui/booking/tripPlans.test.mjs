// ui/booking/tripPlans.test.mjs — AA-996 audit slice 2: trip plan list text and the price matrix editor rules.
// Run: node --test ui/booking/tripPlans.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { daysNights, draftsFrom, draftsToBody, marginLabel, newDraft, paxBandLabel, planSubtitle } from "./tripPlans.ts";

test("daysNights formats the Figma 'N Days / N-1 Nights' duration", () => {
  assert.equal(daysNights(7), "7 days / 6 nights");
  assert.equal(daysNights(2), "2 days / 1 night");
  assert.equal(daysNights(1), "1 day");
  assert.equal(daysNights(0), null);
  assert.equal(daysNights(null), null);
  assert.equal(daysNights(undefined), null);
});

test("planSubtitle prefers places, then the SKU, then a short CIS id", () => {
  assert.equal(planSubtitle({ destinations: ["Hanoi", " ", "Ha Giang"], sku: "VN-5D", source_id: "x" }), "Hanoi · Ha Giang");
  assert.equal(planSubtitle({ destinations: [], sku: "VN-5D", source_id: "x" }), "VN-5D");
  assert.equal(planSubtitle({ sku: null, source_id: "aa996032-0000-4000" }), "CIS aa996032");
});

test("paxBandLabel and marginLabel", () => {
  assert.equal(paxBandLabel(2, 2), "2 pax");
  assert.equal(paxBandLabel(3, 4), "3–4 pax");
  assert.equal(paxBandLabel(11, null), "11+ pax");
  assert.equal(marginLabel("0.2850"), "28.5 %");
  assert.equal(marginLabel("0.3319"), "33.2 %");
  assert.equal(marginLabel(null), null);
  assert.equal(marginLabel("x"), null);
});

const row = (over = {}) => ({ ...newDraft(undefined, "2026-10-28"), amount: "1450", ...over });

test("a fresh row is next year's 2-pax B2C USD row; the next row copies band and channel", () => {
  const first = newDraft(undefined, "2026-10-28");
  assert.equal(first.valid_from, "2027-01-01");
  assert.equal(first.valid_to, "2027-12-31");
  assert.equal(first.pax_min, "2");
  assert.equal(first.channel, "b2c_tailor_made");
  const next = newDraft({ ...first, channel: "b2b", amount: "9", season: "Low" });
  assert.equal(next.channel, "b2b");
  assert.equal(next.amount, "");
  assert.equal(next.season, "");
  assert.notEqual(next.key, first.key);
});

test("draftsToBody builds the PUT rows", () => {
  const { items, errors } = draftsToBody([
    row({ season: " Low ", pax_max: "" }),
    row({ channel: "b2b", amount: "", net_amount: "670", net_currency: "VND" }),
  ]);
  assert.deepEqual(errors, []);
  assert.equal(items[0].season, "Low");
  assert.equal(items[0].pax_max, null);
  assert.equal(items[0].amount, "1450");
  assert.equal(items[0].net_currency, null);
  assert.equal(items[1].amount, null);
  assert.equal(items[1].net_amount, "670");
  assert.equal(items[1].net_currency, "VND");
});

test("draftsToBody refuses what the API refuses", () => {
  const fields = (drafts) => draftsToBody(drafts).errors.map((e) => `${e.row}:${e.field}`);
  assert.deepEqual(fields([row({ valid_to: "2026-12-31" })]), ["0:valid_to"]);
  assert.deepEqual(fields([row({ pax_min: "0" })]), ["0:pax_min"]);
  assert.deepEqual(fields([row({ pax_min: "4", pax_max: "3" })]), ["0:pax_max"]);
  assert.deepEqual(fields([row({ amount: "" })]), ["0:amount"]);
  assert.deepEqual(fields([row({ amount: "0" })]), ["0:amount"]);
  assert.deepEqual(fields([row({ amount: "12.345" })]), ["0:amount"]);
  assert.deepEqual(fields([row({ valid_from: "" })]), ["0:valid_from"]);
  assert.deepEqual(fields([row(), row({ pax_min: "1", pax_max: "2" })]), ["1:row"]);
  assert.deepEqual(fields([row(), row({ pax_min: "3", pax_max: "" })]), []);
  assert.deepEqual(fields([row(), row({ currency: "EUR" })]), []);
  assert.deepEqual(fields([row(), row({ valid_from: "2028-01-01", valid_to: "2028-12-31" })]), []);
});

test("draftsFrom blanks a computed price (computed again on save) and keeps an override", () => {
  const base = { position: 1, channel: "b2c_tailor_made", season: null, peak: false, valid_from: "2027-01-01", valid_to: "2027-04-30", pax_min: 2, pax_max: null, currency: "USD" };
  const [hand, priced, override] = draftsFrom([
    { ...base, amount: "1450.00", net_amount: null, net_currency: null, priced: false },
    { ...base, position: 2, amount: "1000.00", net_amount: "670.00", net_currency: "USD", priced: true, overridden: false },
    { ...base, position: 3, amount: "950.00", net_amount: "670.00", net_currency: "USD", priced: true, overridden: true },
  ]);
  assert.equal(override.amount, "950.00");
  assert.equal(hand.amount, "1450.00");
  assert.equal(hand.pax_max, "");
  assert.equal(hand.season, "");
  assert.equal(priced.amount, "");
  assert.equal(priced.net_amount, "670.00");
});
