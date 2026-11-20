// lib/approveSplit.test.mjs — AA-1155 T4.1 (make / approve split): the decision buttons follow the approval
// capabilities, not the write ones. The API refuses a maker 403 (backend tests/unit/test_aa1155_approve_split.py);
// these checks keep the page from offering a button that can only fail:
// - price adjustment Approve / Reject / Withdraw on the departure board: pricing.approve (proposing, and withdrawing
//   one's own while pending: pricing.write);
// - agency allotment Approve / Reject / Withdraw on a trip plan's departure: b2b_desk.approve (proposing, and
//   withdrawing one's own while pending: b2b_desk.write);
// - hotel and service Activate: supplier.activate (Deactivate: supply.write);
// - pricing rules, FX rates, cancellation policies, tier discounts and rate cards follow the API's per-row
//   can_approve / can_reject, which the API computes from pricing.approve.
// The layout fixture admin (e2e/layout/staff-me.json) holds the new capabilities, as the real admin does.
// Run: node --test lib/approveSplit.test.mjs (`npm run test:unit` runs every lib/*.test.mjs).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { endAction } from "./approveSplit.ts";

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

test("the price adjustment decisions need pricing.approve", () => {
  const src = read("app/(staff)/departures/_parts/DepartureBoardDrawer.tsx");
  assert.match(src, /const canDecide = useCan\("pricing\.approve"\)/);
  assert.match(src, /\{canDecide && a\.state === "pending" && \(/);
  assert.match(src, /endAction\(a\.state, \{ canDecide, canWrite: canPrice, isMaker: a\.created_by\?\.id === me\.cis_user_id \}\)/);
  assert.match(src, /\{endOf\(a\) && \(\s*<Button size="sm" variant="ghost"[^\n]*action: "end"/);
});

test("the allotment decisions need b2b_desk.approve", () => {
  const src = read("app/(staff)/catalog/trip-plans/_parts/DeparturesPanel.tsx");
  assert.match(src, /const canDecide = useCan\("b2b_desk\.approve"\)/);
  assert.match(src, /canDecide=\{canDecide && !cancelled\}/);
  assert.match(src, /canWithdrawOwn=\{canWrite && !cancelled\}/);
  assert.match(src, /const end = endAction\(a\.state, \{ canDecide, canWrite: canWithdrawOwn, isMaker \}\)/);
  assert.doesNotMatch(src, /canWrite && a\.state === "pending"/);
});

test("the End button follows the API: the maker withdraws its own pending row, any other end is a decision", () => {
  const maker = { canDecide: false, canWrite: true, isMaker: true };
  const otherMaker = { canDecide: false, canWrite: true, isMaker: false };
  const checker = { canDecide: true, canWrite: true, isMaker: false };
  const checkerOwn = { canDecide: true, canWrite: true, isMaker: true };
  assert.equal(endAction("pending", maker), "withdraw");
  assert.equal(endAction("pending", otherMaker), null);
  assert.equal(endAction("in_force", maker), null);
  assert.equal(endAction("pending", checker), "reject");
  assert.equal(endAction("pending", checkerOwn), "withdraw");
  assert.equal(endAction("in_force", checker), "withdraw");
  assert.equal(endAction("released", checker), null);
  assert.equal(endAction("ended", checker), null);
  assert.equal(endAction("pending", { canDecide: false, canWrite: false, isMaker: true }), null);
});

test("activating a hotel or a service needs supplier.activate", () => {
  const hook = read("app/(staff)/_parts/useRecordStatus.tsx");
  assert.match(hook, /useCan\("supplier\.activate"\)/);
  assert.match(hook, /return canWrite && \(active \|\| canActivate\)/);
  assert.match(read("app/(staff)/_parts/RecordStatusButton.tsx"), /if \(!status\.canToggle\(active\)\) return null/);
  for (const menu of ["app/(staff)/hotels/_parts/HotelsView.tsx", "app/(staff)/services/_parts/ServicesView.tsx"]) {
    assert.match(read(menu), /status\.canToggle\(row\.status === "active"\)/, menu);
  }
  for (const page of ["app/(staff)/hotels/_parts/HotelDrawer.tsx", "app/(staff)/services/_parts/ServiceDetailView.tsx"]) {
    assert.match(read(page), /<RecordStatusButton status=\{status\}/, page);
  }
});

test("the layout fixture admin holds the approval capabilities", () => {
  const me = JSON.parse(read("e2e/layout/staff-me.json"));
  for (const cap of ["pricing.approve", "b2b_desk.approve", "supplier.activate"]) {
    assert.ok(me.permissions.includes(cap), cap);
  }
});
