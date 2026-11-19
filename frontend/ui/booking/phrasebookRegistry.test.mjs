// ui/booking/phrasebookRegistry.test.mjs — AA-1128 (B5): every message catalogue of the staff app is registered once
// in ui/local/phrases/*.ts, which the shell imports before it renders. A catalogue missing there would be known to the
// server (it keeps every page's modules) but not to a browser that never opened its page, so `tx()` would give the
// server and the browser different text: a hydration error and a page in two languages.
// Run: node --test ui/booking/phrasebookRegistry.test.mjs (`npm run test:unit` runs every ui/booking/*.test.mjs).
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const FRONTEND = fileURLToPath(new URL("../..", import.meta.url));
const PHRASES = join(FRONTEND, "ui", "local", "phrases");

function catalogues(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...catalogues(p));
    else if (name.endsWith(".messages.ts")) out.push(`@/${relative(FRONTEND, p).split(sep).join("/").replace(/\.ts$/, "")}`);
  }
  return out;
}

test("every *.messages.ts of the staff app is imported once by ui/local/phrases", () => {
  const all = ["app/(staff)", "ui/booking", "ui/local"].flatMap((d) => catalogues(join(FRONTEND, d))).sort();
  const listed = [];
  for (const file of readdirSync(PHRASES).filter((f) => f.endsWith(".ts") && f !== "index.ts")) {
    for (const m of readFileSync(join(PHRASES, file), "utf8").matchAll(/^import "([^"]+)";$/gm)) listed.push(m[1]);
  }
  const twice = listed.filter((m, i) => listed.indexOf(m) !== i);
  assert.deepEqual(twice, [], "imported more than once");
  assert.deepEqual(all.filter((m) => !listed.includes(m)), [], "catalogues missing from ui/local/phrases/*.ts");
  assert.deepEqual(listed.filter((m) => !all.includes(m)), [], "imports of catalogues that do not exist");
});

test("the index imports every group file, and the shell imports the index", () => {
  const groups = readdirSync(PHRASES).filter((f) => f.endsWith(".ts") && f !== "index.ts").map((f) => `./${f.replace(/\.ts$/, "")}`);
  const index = readFileSync(join(PHRASES, "index.ts"), "utf8");
  for (const g of groups) assert.ok(index.includes(`import "${g}";`), `index.ts imports ${g}`);
  assert.match(readFileSync(join(FRONTEND, "ui", "local", "AdminShell.tsx"), "utf8"), /^import "\.\/phrases";$/m);
});
