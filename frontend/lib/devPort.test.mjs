// lib/devPort.test.mjs
// AA-1134 (S283 docs audit) — `npm run dev` listens on the port the booking-staff Cognito client registers for local
// sign-in (lib/cognito.ts DEV_ORIGIN, Infra identity callback) and the container listens on (Dockerfile PORT). It was
// 3100 against a 3001 callback, so a local sign-in failed with a redirect mismatch.
// Run: npm run test:unit (CI "Unit tests (node --test)"). Reads the source files only.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const here = new URL("../", import.meta.url);
const read = (rel) => readFileSync(new URL(rel, here), "utf8");

function devOriginPort() {
  const m = read("lib/cognito.ts").match(/const DEV_ORIGIN = "http:\/\/localhost:(\d+)"/);
  assert.ok(m, "lib/cognito.ts declares DEV_ORIGIN");
  return m[1];
}

test("npm run dev and npm start use the Cognito dev callback port", () => {
  const scripts = JSON.parse(read("package.json")).scripts;
  const port = devOriginPort();
  assert.match(scripts.dev, new RegExp(`--port ${port}(\\s|$)`));
  assert.match(scripts.start, new RegExp(`--port ${port}(\\s|$)`));
});

test("the container listens on the same port", () => {
  assert.match(read("Dockerfile"), new RegExp(`PORT=${devOriginPort()}\\b`));
});
