// ui/booking/noIssueIds.test.mjs — guard: user-visible text never carries an internal issue ID (AA-994, rule R2).
// Run: npm run test:unit (Node's built-in runner).
//
// Every source file of the staff app (app/, ui/, lib/, proxy.ts) is parsed with the TypeScript compiler, and every
// string the app could render is checked: string literals, template literal parts and JSX text. Comments are not
// part of the syntax tree, so issue IDs stay allowed there (and in tests and commit text). Module specifiers of
// import / export are skipped. A hit means a label, tooltip, help text or message would show "AA-123" to staff:
// reword it, and keep the issue reference in a comment above the line instead.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SCAN = ["app", "ui", "lib", "proxy.ts"];
const SOURCE = /\.(tsx?|mjs|js)$/;
// Tests and lint configs are never rendered: eslint-kit.mjs messages are developer-facing (the kit core copy, AA-994).
const SKIP = /(\.test\.(mjs|ts|tsx)|(^|\/)eslint-[\w-]+\.mjs)$/;
const ISSUE_ID = /\bAA-\d+\b/;

function sourceFiles(path) {
  const abs = join(ROOT, path);
  let st;
  try {
    st = statSync(abs);
  } catch {
    return [];
  }
  if (st.isFile()) return SOURCE.test(path) && !SKIP.test(path) ? [path] : [];
  return readdirSync(abs).flatMap((name) => (name === "node_modules" ? [] : sourceFiles(join(path, name))));
}

function isModuleSpecifier(node) {
  const p = node.parent;
  return Boolean(p && (ts.isImportDeclaration(p) || ts.isExportDeclaration(p)) && p.moduleSpecifier === node);
}

/** Every renderable string in one file that names an issue ID, as "file:line: text". */
export function findIssueIds(file, text) {
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : file.endsWith(".ts") ? ts.ScriptKind.TS : ts.ScriptKind.JS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const hits = [];
  const visit = (node) => {
    let value = null;
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      if (!isModuleSpecifier(node)) value = node.text;
    } else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      value = node.text;
    } else if (ts.isJsxText(node)) {
      value = node.text;
    }
    if (value !== null && ISSUE_ID.test(value)) {
      const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
      hits.push(`${file}:${line}: ${value.trim().slice(0, 120)}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

test("the scanner flags strings and JSX text, not comments", () => {
  const sample = [
    "// AA-1 in a comment is fine",
    "/* AA-2 as well */",
    'const a = "see AA-3";',
    "const b = `wave ${1} of AA-4`;",
    "const c = <p title='AA-5'>Shipped in AA-6</p>;",
    'import x from "./AA-7";',
  ].join("\n");
  const hits = findIssueIds("sample.tsx", sample).map((h) => h.split(": ")[1]);
  assert.deepEqual(hits, ["see AA-3", "of AA-4", "AA-5", "Shipped in AA-6"]);
});

// Text files the app serves byte for byte (public/, and app/ icons or manifests): a comment in them reaches the
// browser too, so the whole file is checked, comments included.
const SERVED_AS_IS = /\.(svg|txt|json|xml|webmanifest|html)$/;

function servedFiles(path) {
  const abs = join(ROOT, path);
  let st;
  try {
    st = statSync(abs);
  } catch {
    return [];
  }
  if (st.isFile()) return SERVED_AS_IS.test(path) ? [path] : [];
  return readdirSync(abs).flatMap((name) => servedFiles(join(path, name)));
}

test("no file served as is (public/, app/ assets) names an internal issue ID", () => {
  const hits = ["public", "app"].flatMap(servedFiles).filter((f) => ISSUE_ID.test(readFileSync(join(ROOT, f), "utf8")));
  assert.deepEqual(hits, [], `issue IDs in files served as is:\n${hits.join("\n")}`);
});

test("no user-visible string in the staff app names an internal issue ID", () => {
  const files = SCAN.flatMap(sourceFiles);
  assert.ok(files.length > 50, `expected the app sources, found ${files.length} files`);
  const hits = files.flatMap((f) => findIssueIds(relative(ROOT, join(ROOT, f)), readFileSync(join(ROOT, f), "utf8")));
  assert.deepEqual(hits, [], `issue IDs in user-visible text:\n${hits.join("\n")}`);
});
