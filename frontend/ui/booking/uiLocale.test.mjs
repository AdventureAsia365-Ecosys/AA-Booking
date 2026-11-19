// ui/booking/uiLocale.test.mjs — guard (AA-1128, vi review): days, months and numbers are formatted in the UI language.
// Run: npm run test:unit (Node's built-in runner).
//
// A page that hard-codes an English locale ("en-GB", "en-US") shows "9 Oct 2026" and "1,234.50" next to the kit
// <DateText> / <Money>, which follow the UI language, so a Vietnamese page mixes two formats. Format with the kit
// helpers or with `useBookingFormat()` (ui/local/useBookingFormat.ts), or pass `lang` to the ui/booking/dates.ts helpers.
// The only files allowed to name a locale are the ones that map a UI language to it.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SCAN = ["app", "ui", "lib"];
const SOURCE = /\.(tsx?)$/;
const SKIP = /\.test\.(mjs|ts|tsx)$/;
const LOCALE = /["'`]en-(GB|US)["'`]|\.toLocale(Date|Time)?String\(\s*\)/;
/** Files that map a UI language to its locale (the kit copies are checked against CIS byte for byte). */
const ALLOWED = new Set(["ui/booking/dates.ts", "ui/booking/caseFacts.ts"]);
const ALLOWED_DIR = "ui/core/";
/** Pages still on a fixed locale (none left: keep it empty). */
const PENDING = new Set([]);

function sourceFiles(path) {
  const abs = join(ROOT, path);
  const st = statSync(abs);
  if (st.isFile()) return SOURCE.test(path) && !SKIP.test(path) ? [path] : [];
  return readdirSync(abs).flatMap((name) => (name === "node_modules" ? [] : sourceFiles(join(path, name))));
}

test("no page formats with a fixed English locale", () => {
  const hits = [];
  for (const file of SCAN.flatMap(sourceFiles)) {
    if (ALLOWED.has(file) || file.startsWith(ALLOWED_DIR) || PENDING.has(file)) continue;
    readFileSync(join(ROOT, file), "utf8")
      .split("\n")
      .forEach((line, i) => {
        if (LOCALE.test(line) && !line.trim().startsWith("//") && !line.trim().startsWith("*")) hits.push(`${file}:${i + 1}: ${line.trim()}`);
      });
  }
  assert.deepEqual(hits, [], "format with useBookingFormat() or pass the UI language");
});

test("the pending list names only files that still need it", () => {
  const stale = [...PENDING].filter((file) => !LOCALE.test(readFileSync(join(ROOT, file), "utf8")));
  assert.deepEqual(stale, [], "remove these files from PENDING");
});
