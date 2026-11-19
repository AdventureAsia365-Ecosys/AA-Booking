// ui/booking/txDisplayOnly.test.mjs — guard (AA-1128, vi review): the phrasebook `tx()` is for display only.
// Run: npm run test:unit (Node's built-in runner).
//
// `tx(value)` shows a stored value in the UI language when it equals a catalogue key ("Transfer" → "Đưa đón"). Put
// into form state, a code or a payload, that translation is saved back: a Vietnamese member of staff who opens a
// proposal item titled "Transfer" and presses Apply writes "Đưa đón" into the customer's proposal. A form starts from
// the stored value; only the rendered text goes through tx().
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SCAN = ["app", "ui", "lib"];
const SOURCE = /\.tsx?$/;
const SKIP = /\.test\.(mjs|ts|tsx)$/;
/** Data fields of a form or a payload filled from tx(), a state seeded with tx(), or a code passed on translated. */
const BAD = [
  /\b(title|body|notes|note|reason|description|name|code|value|amount): tx\(/,
  /useState(<[^>]*>)?\(tx\(/,
  /\b(value|defaultValue|initial)=\{tx\(/,
];

function sourceFiles(path) {
  const st = statSync(join(ROOT, path));
  if (st.isFile()) return SOURCE.test(path) && !SKIP.test(path) ? [path] : [];
  return readdirSync(join(ROOT, path)).flatMap((name) => (name === "node_modules" ? [] : sourceFiles(join(path, name))));
}

test("tx() never fills form state, a payload field or a code", () => {
  const hits = [];
  for (const file of SCAN.flatMap(sourceFiles)) {
    readFileSync(join(ROOT, file), "utf8")
      .split("\n")
      .forEach((line, i) => {
        if (BAD.some((re) => re.test(line))) hits.push(`${file}:${i + 1}: ${line.trim()}`);
      });
  }
  assert.deepEqual(hits, [], "start the form from the stored value; translate only where it is rendered");
});
