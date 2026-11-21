// lib/rbacNoRoleNameGates.test.mjs — AA-1155 T7 (root spec R4.5): a page decides what the signed-in person may do
// from the capabilities in GET /booking/staff/me `permissions` (useCan), never by comparing me.role with a built-in
// role name. A custom role (role = custom_<slug>) holds capabilities, not a built-in name, so a role-name gate hides
// an action its capabilities grant (the S287 review found the proposal Price section and the pricing-rules link).
// The few uses below are listed with their reason; anything new fails here.
// Run: node --test lib/rbacNoRoleNameGates.test.mjs (`npm run test:unit` runs every lib/*.test.mjs).
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SCAN = ["app", "ui", "lib"];

// me.role compared with a string, or a role list / set asked whether it holds me.role.
const ROLE_GATE = /me\??\.role\s*[!=]==?\s*["'`]|\.(?:includes|has)\(\s*me\??\.role\s*\)/g;

// Exact snippets allowed, each with the reason it is not a Booking action gate.
const ALLOWED = new Map([
  // A link to the CIS editor: CIS authorises the edit itself, and no Booking capability describes CIS editing.
  ["CIS_EDITOR_ROLES.has(me.role)", "link into CIS, gated by CIS"],
  // The convert dialog's default sales rep (the person themself when they are a sales rep): a pre-filled value,
  // not an access gate; trip_case.assign gates the picker and the API checks the assignment.
  ['me.role === "sales" ? me.cis_user_id', "default value, not a gate"],
]);

function* sources(dir) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name !== "node_modules" && !e.name.startsWith(".")) yield* sources(p);
    } else if (/\.(tsx?|mjs)$/.test(e.name) && !e.name.endsWith(".test.mjs")) yield p;
  }
}

/** Every role-name gate in `text` that no ALLOWED snippet covers. */
export function roleNameGates(text) {
  const found = [];
  for (const m of text.matchAll(ROLE_GATE)) {
    const allowed = [...ALLOWED.keys()].some((snippet) => {
      const at = text.lastIndexOf(snippet, m.index + m[0].length);
      return at >= 0 && at <= m.index && m.index + m[0].length <= at + snippet.length;
    });
    if (!allowed) {
      const line = text.slice(0, m.index).split("\n").length;
      found.push(`${line}: ${m[0]}`);
    }
  }
  return found;
}

test("the matcher sees role-name gates and lets the listed uses through", () => {
  assert.equal(roleNameGates('const a = me.role === "admin";').length, 1);
  assert.equal(roleNameGates('const a = me.role !== "finance";').length, 1);
  assert.equal(roleNameGates('const a = ["admin", "sales"].includes(me.role);').length, 1);
  assert.equal(roleNameGates("const a = EDITORS.has(me.role);").length, 1);
  assert.equal(roleNameGates('const a = useCan("pricing.read");').length, 0);
  assert.equal(roleNameGates('const s = c.assignees.find((a) => a.role === "sales");').length, 0);
  assert.equal(roleNameGates("const e = CIS_EDITOR_ROLES.has(me.role);").length, 0);
  assert.equal(roleNameGates('useState(current || (me.role === "sales" ? me.cis_user_id : ""));').length, 0);
});

test("no page gates an action by comparing me.role with a role name", () => {
  const bad = [];
  for (const dir of SCAN) {
    for (const file of sources(join(ROOT, dir))) {
      for (const hit of roleNameGates(readFileSync(file, "utf8"))) bad.push(`${relative(ROOT, file)}:${hit}`);
    }
  }
  assert.deepEqual(bad, [], "gate on a capability with useCan(...) instead of me.role");
});
