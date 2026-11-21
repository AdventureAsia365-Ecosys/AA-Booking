// lib/rbacPermissions.test.mjs — AA-1155 T7 (root spec R4.5): the staff admin opens pages by the capabilities in
// GET /booking/staff/me `permissions`, never by the role name.
// 1. Every built-in role opens exactly the pages it opened when the rules listed role names (the table below is the
//    rule set before T7, frozen here), with its capabilities taken from the backend matrix
//    (lib/rbac.permissions.fixture.json; backend tests/unit/test_aa1155_frontend_fixture.py keeps it equal).
// 2. The seeded custom role "Product, ops & accounting" opens the pages its capabilities serve (the live check's
//    /suppliers, /billing, /agency-billing among them) and none of the admin pages.
// Run: node --test lib/rbacPermissions.test.mjs (`npm run test:unit` runs every lib/*.test.mjs).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const { PAGE_RULES, canSeePage, roleName } = await import(new URL("./rbac.ts", import.meta.url).href);
const MATRIX = JSON.parse(readFileSync(new URL("./rbac.permissions.fixture.json", import.meta.url), "utf8"));

const ALL = ["admin", "sales", "contracting", "content_creator", "finance"];
const [A, S, C, F] = ["admin", "sales", "contracting", "finance"];
// The role-name rules of lib/rbac.ts before AA-1155 T7 (S286 main), plus the role editor (new: admin, as /staff).
const BEFORE = {
  "/": ALL,
  "/notifications": ALL,
  "/settings": ALL,
  "/cases/new": [A, S],
  "/cases": [A, S, C, F],
  "/cases/*": [A, S, C, F],
  "/proposals": [A, S, C, F],
  "/pricing/calculator": [A, S, C, F],
  "/staff": [A],
  "/staff/:id": ALL,
  "/audit-log": [A],
  "/roles": [A],
  "/suppliers/new": [A, C],
  "/suppliers/rate-sheets/*": [A, C],
  "/suppliers/:id/edit": [A, C],
  "/suppliers": [A, C, S, F],
  "/suppliers/*": [A, C, S, F],
  "/hotels": [A, C, S, F],
  "/services": [A, C, S, F],
  "/services/*": [A, C, S, F],
  "/pricing/rules": [A, C],
  "/pricing/agency-tiers": [A, S, C, F],
  "/pricing/agency-tiers/tariff": [A, S, C, F],
  "/pricing/readiness": [A, S, C, F],
  "/pricing/workbench": [A, S, C, F],
  "/pricing/policy": [A, S, C, F],
  "/pricing/seasons": [A, S, C, F],
  "/pricing/holiday-events": [A, S, C, F],
  "/stop-sales": [A, S, C, F],
  "/catalog/trip-plans/:id/commercial": [A, C],
  "/catalog/*": ALL,
  "/billing": [A, S, C, F],
  "/agency-billing": [A, F],
  "/agency-billing/*": [A, F],
  "/trip-checks": [A, S, C, F],
  "/b2b-desk": [A, S, C],
  "/b2b-desk/manifest/:id": [A, S, C, F],
  "/departures": [A, S, C],
  "/finance": [A, S, C, F],
};

/** A concrete path for a pattern (`:x` and a trailing `*` take an invented segment). */
const sample = (pattern) =>
  pattern === "/" ? "/" : pattern.split("/").map((p) => (p.startsWith(":") ? "X-000001" : p === "*" ? "item" : p)).join("/");

test("the rule table and the frozen role table name the same pages", () => {
  assert.deepEqual(PAGE_RULES.map((r) => r.pattern).sort(), Object.keys(BEFORE).sort());
});

test("every built-in role opens exactly the pages it opened by role name", () => {
  for (const role of ALL) {
    const perms = MATRIX.builtin[role];
    assert.ok(Array.isArray(perms) && perms.length > 0, role);
    for (const [pattern, roles] of Object.entries(BEFORE)) {
      assert.equal(canSeePage(perms, sample(pattern)), roles.includes(role), `${role} ${pattern}`);
    }
  }
});

test("every capability a rule names exists in the backend matrix", () => {
  const known = new Set(Object.values(MATRIX.builtin).flat());
  for (const rule of PAGE_RULES) for (const p of rule.perms) assert.ok(known.has(p), `${rule.pattern}: ${p}`);
});

test("the seeded custom role opens its pages and no admin page", () => {
  const perms = MATRIX.custom.custom_product_ops_accounting;
  for (const path of ["/suppliers", "/billing", "/agency-billing", "/suppliers/new", "/pricing/rules", "/cases",
    "/b2b-desk", "/departures", "/trip-checks", "/finance", "/stop-sales", "/catalog/trip-plans", "/"]) {
    assert.ok(canSeePage(perms, path), path);
  }
  for (const path of ["/staff", "/audit-log", "/roles", "/pricing/policy"]) assert.ok(!canSeePage(perms, path), path);
});

test("no capabilities or an unknown page open nothing but the open pages", () => {
  assert.equal(canSeePage(null, "/"), false);
  assert.equal(canSeePage(undefined, "/suppliers"), false);
  assert.equal(canSeePage([], "/"), true);
  assert.equal(canSeePage([], "/suppliers"), false);
  assert.equal(canSeePage(MATRIX.builtin.admin, "/no-such-page"), false);
});

test("a custom role is named by its label, a built-in role by the staff admin", () => {
  assert.equal(roleName("contracting"), "Product & operations");
  assert.equal(roleName("custom_quay_desk", "Quay desk"), "Quay desk");
  assert.equal(roleName("custom_quay_desk", null), "custom_quay_desk");
  assert.equal(roleName("custom_quay_desk", "  "), "custom_quay_desk");
});
