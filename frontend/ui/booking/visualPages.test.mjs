// ui/booking/visualPages.test.mjs
// AA-1008 (root ADR 0016 decision 9, guard G16) — the visual run's page list (e2e/visual/pages.ts) and its helpers.
// Run: npm run test:unit (CI: Frontend Build, "Unit tests (node --test)").
//   1. every app/**/page.tsx route is listed in PAGES or in EXCLUDED with a reason, and nothing else is;
//   2. slugs are unique and file-name safe; staff pages render the kit frame unless they say otherwise;
//   3. changed-pages.mjs: a page folder touches its pages, a private folder its parent's, app/layout.tsx / ui / the
//      visual files every page, a backend file none;
//   4. selectPages: public vs staff vs all, the gallery only in a gallery run, the planted pages only in the self-test;
//   5. expectedOutcome: the mocked run keeps the 404 page; a deployed host (signed out) expects the /login redirect.

import assert from "node:assert/strict";
import { readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const FRONTEND = fileURLToPath(new URL("../..", import.meta.url));
const VISUAL = join(FRONTEND, "e2e", "visual");
const { PAGES, EXCLUDED, selectPages, expectedOutcome } = await import(pathToFileURL(join(VISUAL, "pages.ts")).href);
const { changedSlugs } = await import(pathToFileURL(join(VISUAL, "changed-pages.mjs")).href);

function appRoutes(dir = join(FRONTEND, "app")) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === "node_modules" || name === "api") continue;
      out.push(...appRoutes(p));
    } else if (name === "page.tsx") {
      const rel = relative(join(FRONTEND, "app"), dir).split(sep).filter((s) => !/^\(.*\)$/.test(s)).join("/");
      out.push(`/${rel}`.replace(/\/$/, "") || "/");
    }
  }
  return out;
}

test("G16: every app route is listed or excluded with a reason, and nothing else is", () => {
  const routes = new Set(appRoutes());
  const listed = new Set(PAGES.map((p) => p.route).filter((r) => r.startsWith("/")));
  const missing = [...routes].filter((r) => !listed.has(r) && !EXCLUDED[r]);
  assert.deepEqual(missing, [], "add these routes to e2e/visual/pages.ts (or EXCLUDED with a reason)");
  const stale = [...listed, ...Object.keys(EXCLUDED)].filter((r) => !routes.has(r));
  assert.deepEqual(stale, [], "these listed routes have no page.tsx any more");
  for (const [r, why] of Object.entries(EXCLUDED)) assert.ok(why.length > 20, `${r} needs a real reason`);
});

test("slugs are unique and file-name safe; staff pages render the kit frame", () => {
  const slugs = PAGES.map((p) => p.slug);
  assert.equal(new Set(slugs).size, slugs.length);
  for (const p of PAGES) {
    assert.match(p.slug, /^[a-z0-9-]+$/);
    if (p.auth === "staff") assert.notEqual(p.frame, false, `${p.slug}: every staff page renders PageFrame`);
  }
  assert.equal(PAGES.filter((p) => p.selftest).length, 2);
});

test("changed-pages: a folder touches its pages, shared files every page, a backend file none", () => {
  assert.deepEqual(changedSlugs(["frontend/app/(staff)/hotels/page.tsx"]), ["hotels"]);
  assert.deepEqual(changedSlugs(["frontend/app/(staff)/agency-billing/invoices/[ref]/page.tsx"]), ["agency-invoice", "agency-settlement"]);
  assert.deepEqual(
    changedSlugs(["frontend/app/(staff)/suppliers/_parts/SupplierForm.tsx"]),
    PAGES.filter((p) => p.route.startsWith("/suppliers")).map((p) => p.slug).sort(),
  );
  assert.deepEqual(changedSlugs(["frontend/app/kit-gallery/GalleryView.tsx"]), ["kit-gallery", "selftest-document", "selftest-frame"]);
  assert.equal(changedSlugs(["frontend/app/layout.tsx"]), "all");
  assert.equal(changedSlugs(["frontend/app/(staff)/page.tsx"]), "all", "the dashboard folder is the app root");
  assert.equal(changedSlugs(["frontend/ui/core/ShellFrame.tsx"]), "all");
  assert.equal(changedSlugs(["frontend/ui/local/AdminShell.tsx"]), "all");
  assert.equal(changedSlugs(["frontend/e2e/visual/visual.spec.ts"]), "all");
  assert.equal(changedSlugs([".github/workflows/visual.yml"]), "all");
  assert.deepEqual(changedSlugs(["backend/app/main.py", "migrations/bk-001.sql", "docs/x.md"]), []);
});

test("selectPages: public / staff / all, the gallery only in a gallery run, the planted pages only in the self-test", () => {
  const pub = selectPages(PAGES, { auth: "public", slugs: "all", gallery: false, selftest: false }).map((p) => p.slug);
  assert.deepEqual(pub.sort(), ["login", "not-found"]);
  const pr = selectPages(PAGES, { auth: "all", slugs: "", gallery: true, selftest: false }).map((p) => p.slug);
  assert.deepEqual(pr, ["kit-gallery"], "a PR that changes no page still captures the gallery");
  const some = selectPages(PAGES, { auth: "all", slugs: "hotels,login", gallery: false, selftest: false }).map((p) => p.slug);
  assert.deepEqual(some.sort(), ["hotels", "login"]);
  const staff = selectPages(PAGES, { auth: "staff", slugs: "hotels,login", gallery: false, selftest: false }).map((p) => p.slug);
  assert.deepEqual(staff, ["hotels"]);
  const self = selectPages(PAGES, { auth: "all", slugs: "all", gallery: true, selftest: true }).map((p) => p.slug);
  assert.deepEqual(self, ["selftest-document", "selftest-frame"]);
});

test("expectedOutcome: the mock keeps 404, a deployed host (no session) lands on /login", () => {
  const bySlug = Object.fromEntries(PAGES.map((p) => [p.slug, p]));
  // Vercel Deploy runs 42006934840..42013253500: proxy.ts answers a signed-out unknown path with a 307 to
  // /login?next=<path>, and page.goto returns the final response, so 404 can never be the deployed answer.
  assert.deepEqual(expectedOutcome(bySlug["not-found"], false), { status: 404, finalPath: null });
  assert.deepEqual(expectedOutcome(bySlug["not-found"], true), { status: 200, finalPath: "/login" });
  assert.deepEqual(expectedOutcome(bySlug.login, true), { status: 200, finalPath: null });
  assert.deepEqual(expectedOutcome(bySlug.hotels, false), { status: 200, finalPath: null });
  for (const p of PAGES.filter((x) => x.deployed)) {
    assert.equal(p.auth, "public", `${p.slug}: only public pages are captured on a deployed host`);
    assert.ok(p.deployed.why.length > 20, `${p.slug}: the deployed expectation needs a real reason`);
    assert.match(p.deployed.finalPath, /^\/[a-z0-9/-]*$/, `${p.slug}: a path, no query`);
  }
  // Every public page has a defined answer on a host.
  for (const p of selectPages(PAGES, { auth: "public", slugs: "all", gallery: false, selftest: false })) {
    assert.ok(Number.isInteger(expectedOutcome(p, true).status), p.slug);
  }
});
