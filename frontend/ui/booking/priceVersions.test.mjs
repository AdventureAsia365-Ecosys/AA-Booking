// ui/booking/priceVersions.test.mjs — AA-1091: price versions, seasons and holiday events helpers (which buttons a
// person sees: the maker never approves their own change; diff text; event codes).
// Run: node --test ui/booking/priceVersions.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { changeActions, changeText, eventCode, needsReason, personText, versionActions } from "./priceVersions.ts";

const who = (id, name = null) => ({ id, name });

test("version buttons follow maker-checker", () => {
  const draft = { status: "draft", created_by: who("maker") };
  assert.deepEqual(versionActions(draft, "maker", true, true), ["edit", "submit", "withdraw"]);
  assert.deepEqual(versionActions(draft, "checker", true, true), []);
  const pending = { status: "pending", created_by: who("maker") };
  assert.deepEqual(versionActions(pending, "maker", true, true), ["withdraw"]);
  assert.deepEqual(versionActions(pending, "checker", true, true), ["approve", "reject"]);
  assert.deepEqual(versionActions(pending, "checker", true, false), []);
  const published = { status: "published", created_by: who("maker") };
  assert.deepEqual(versionActions(published, "maker", true, true), []);
  assert.deepEqual(versionActions(published, "checker", false, true), ["withdraw"]);
  assert.deepEqual(versionActions({ status: "superseded", created_by: who("maker") }, "checker", true, true), []);
});

test("range and event buttons follow maker-checker", () => {
  const item = (state, by = "maker", end = null) => ({ state, created_by: who(by), end_requested_by: end && who(end) });
  assert.deepEqual(changeActions(item("pending"), "maker", true, true), ["withdraw"]);
  assert.deepEqual(changeActions(item("pending"), "checker", true, true), ["approve", "reject"]);
  assert.deepEqual(changeActions(item("approved"), "anyone", true, false), ["end"]);
  assert.deepEqual(changeActions(item("ending", "maker", "asker"), "asker", true, true), []);
  assert.deepEqual(changeActions(item("ending", "maker", "asker"), "checker", true, true), ["end_approve", "end_reject"]);
  assert.deepEqual(changeActions(item("ended"), "checker", true, true), []);
});

test("diff text and event codes", () => {
  assert.equal(changeText({ kind: "added", change_pct: null }), "new");
  assert.equal(changeText({ kind: "changed", change_pct: "12.0" }), "+12.0 %");
  assert.equal(changeText({ kind: "changed", change_pct: "-3.5" }), "-3.5 %");
  assert.equal(changeText({ kind: "unchanged", change_pct: "0.0" }), "same");
  assert.equal(eventCode("Invented Lantern Night"), "invented_lantern_night");
  assert.equal(eventCode("Fête des lumières"), "fete_des_lumieres");
  assert.equal(eventCode("2027 gala"), "e_2027_gala");
});

test("people are shown by name, markers by a label, the id only as a fallback", () => {
  const labels = { migration: "Migrated prices", system: "Catalog sync", seed: "Initial value", nobody: "—" };
  assert.equal(personText(who("u-1", "Invented Person"), labels), "Invented Person");
  assert.equal(personText(who("migration:bk-054"), labels), "Migrated prices");
  assert.equal(personText(who("system:bk_trip_plan_sync"), labels), "Catalog sync");
  assert.equal(personText(who("seed:bk-050"), labels), "Initial value");
  assert.equal(personText(who("u-9"), labels), "u-9");
  assert.equal(personText(null, labels), "—");
});

test("every decision but an approval needs a reason", () => {
  assert.deepEqual(
    ["approve", "reject", "withdraw", "end", "end_approve", "end_reject"].filter(needsReason),
    ["reject", "withdraw", "end", "end_reject"],
  );
  assert.equal(needsReason("approve_plan_calendar"), false);
});
