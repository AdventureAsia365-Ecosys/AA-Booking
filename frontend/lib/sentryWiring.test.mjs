// lib/sentryWiring.test.mjs
// AA-1111 (S282 Sentry coverage) — the Booking staff admin reports errors to Sentry (project aa-booking-admin) the way
// the AA-Partner web does, and only when a DSN is built in:
//   1. instrumentation.ts initialises the server SDK in the nodejs runtime only, through sentryInitOptions (null = no
//      DSN = no init), and exports onRequestError = Sentry.captureRequestError;
//   2. instrumentation-client.ts initialises the browser SDK only through the same options (one Sentry.init, guarded);
//   3. no legacy sentry.*.config.ts (a Turbopack build of Next 16 never loads them);
//   4. the image takes NEXT_PUBLIC_SENTRY_DSN as a build arg (empty by default) and Deploy Admin Dev feeds it from the
//      `dev` variable ADMIN_PUBLIC_SENTRY_DSN.
// Run: npm run test:unit. Reads source files only.

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Source without comments, so a comment that names a call never satisfies a check. */
function code(rel) {
  return readFileSync(join(ROOT, rel), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

test("the server hook initialises Sentry in the nodejs runtime only, with the shared options", () => {
  const src = code("instrumentation.ts");
  assert.match(src, /export\s+async\s+function\s+register\s*\(/);
  assert.match(src, /if\s*\(process\.env\.NEXT_RUNTIME\s*!==\s*"nodejs"\)\s*return\s*;/);
  assert.match(src, /sentryInitOptions\(/);
  assert.match(src, /process\.env\.SENTRY_DSN\s*\|\|\s*process\.env\.NEXT_PUBLIC_SENTRY_DSN/);
  assert.deepEqual([...src.matchAll(/Sentry\.init\(([^)]*)\)/g)].map((m) => m[1]), ["options"]);
  assert.match(src, /if\s*\(options\)\s*Sentry\.init\(options\)\s*;/);
  assert.match(src, /export\s+const\s+onRequestError\s*=\s*Sentry\.captureRequestError\s*;/);
});

test("the client hook initialises Sentry only with a DSN, with the shared options", () => {
  const src = code("instrumentation-client.ts");
  assert.match(src, /from "\.\/lib\/sentryConfig"/);
  assert.match(src, /dsn:\s*process\.env\.NEXT_PUBLIC_SENTRY_DSN,/);
  assert.deepEqual([...src.matchAll(/Sentry\.init\(([^)]*)\)/g)].map((m) => m[1]), ["options"]);
  assert.match(src, /if\s*\(options\)\s*Sentry\.init\(options\)\s*;/);
});

test("no legacy sentry.*.config.ts", () => {
  for (const name of ["sentry.client.config.ts", "sentry.server.config.ts", "sentry.edge.config.ts"]) {
    assert.equal(existsSync(join(ROOT, name)), false, name);
  }
});

test("the DSN is a build arg fed by the dev variable ADMIN_PUBLIC_SENTRY_DSN", () => {
  const dockerfile = readFileSync(join(ROOT, "Dockerfile"), "utf8");
  assert.match(dockerfile, /^ARG NEXT_PUBLIC_SENTRY_DSN=""$/m);
  assert.match(dockerfile, /NEXT_PUBLIC_SENTRY_DSN=\$\{NEXT_PUBLIC_SENTRY_DSN\}/);
  assert.match(dockerfile, /NEXT_PUBLIC_GIT_SHA=\$\{GIT_SHA\}/);
  const workflow = readFileSync(join(ROOT, "..", ".github", "workflows", "deploy-admin-dev.yml"), "utf8");
  assert.match(workflow, /PUBLIC_SENTRY_DSN: \$\{\{ vars\.ADMIN_PUBLIC_SENTRY_DSN \}\}/);
  assert.match(workflow, /--build-arg NEXT_PUBLIC_SENTRY_DSN="\$\{PUBLIC_SENTRY_DSN\}"/);
});
