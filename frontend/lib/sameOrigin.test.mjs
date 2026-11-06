// lib/sameOrigin.test.mjs — AA-911: the same-origin check behind the ALB (staff web on ECS), where the Next
// standalone server's nextUrl names its own bind address, not the public host.
// Run: npm run test:unit (Node 22 strips the TypeScript types of sameOrigin.ts). Host names are invented.

import assert from "node:assert/strict";
import { test } from "node:test";

import { isSameOrigin, requestOrigin } from "./sameOrigin.ts";

const BIND = "https://0.0.0.0:3001"; // what Next standalone puts in nextUrl behind the ALB

function req(headers, { origin = BIND } = {}) {
  const h = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return { headers: { get: (n) => h.get(n.toLowerCase()) ?? null }, nextUrl: { origin } };
}

test("behind the ALB the public origin comes from Host when the request came over HTTPS", () => {
  assert.equal(requestOrigin(req({ host: "desk.example-staff.test", "x-forwarded-proto": "https" })),
    "https://desk.example-staff.test");
  assert.equal(requestOrigin(req({ host: "Desk.Example-Staff.test", "x-forwarded-proto": "https, http" })),
    "https://desk.example-staff.test");
});

test("without X-Forwarded-Proto https (next dev, plain HTTP) nextUrl stays the origin", () => {
  assert.equal(requestOrigin(req({ host: "localhost:3100" }, { origin: "http://localhost:3100" })), "http://localhost:3100");
  assert.equal(requestOrigin(req({ host: "evil.example.test", "x-forwarded-proto": "http" })), BIND);
});

test("a write from the page's own host passes, one from a sibling host does not", () => {
  const own = req({ host: "desk.example-staff.test", "x-forwarded-proto": "https", origin: "https://desk.example-staff.test" });
  assert.equal(isSameOrigin(own), true);
  const sibling = req({ host: "desk.example-staff.test", "x-forwarded-proto": "https", origin: "https://preview.example-staff.test" });
  assert.equal(isSameOrigin(sibling), false);
  const plain = req({ host: "desk.example-staff.test", "x-forwarded-proto": "https", origin: "http://desk.example-staff.test" });
  assert.equal(isSameOrigin(plain), false);
});

test("Vercel and next dev keep the old rule (Origin equals nextUrl.origin)", () => {
  const vercel = req({ host: "desk.example-staff.test", "x-forwarded-proto": "https", origin: "https://desk.example-staff.test" },
    { origin: "https://desk.example-staff.test" });
  assert.equal(isSameOrigin(vercel), true);
  const dev = req({ host: "localhost:3100", origin: "http://localhost:3100" }, { origin: "http://localhost:3100" });
  assert.equal(isSameOrigin(dev), true);
});

test("Sec-Fetch-Site and strict keep their meaning", () => {
  assert.equal(isSameOrigin(req({ "sec-fetch-site": "same-origin" })), true);
  assert.equal(isSameOrigin(req({ "sec-fetch-site": "same-site" })), false);
  assert.equal(isSameOrigin(req({}), { strict: true }), false);
  assert.equal(isSameOrigin(req({})), true);
});
