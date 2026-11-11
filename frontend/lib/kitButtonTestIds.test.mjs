// lib/kitButtonTestIds.test.mjs — guard (AA-1069): a kit button takes its test id through the `testId` prop, never a
// plain `data-testid` attribute. Run: npm run test:unit (Node's built-in runner).
//
// The kit's button family (Button, LinkButton, IconButton, ChipButton, Switch, ListButton, Pressable in
// ui/core/Buttons.tsx, a copy of the AA-CIS-App kit core) builds its DOM props with `domProps()`. Until AA-1069 that
// set `data-testid` from `testId` after the native props, so a plain `data-testid="..."` on one of them was silently
// dropped (nine CRM web call sites lost their ids; Playwright waited for controls that were on the page). The core now
// keeps the native attribute when `testId` is unset, and this test keeps the call sites on the one documented prop:
// the same AST scan as the CIS admin and CRM web guards. Every runtime .tsx of app/, ui/ and lib/ is parsed with the
// TypeScript compiler, kit imports are resolved by name and alias ("@/ui", "@/ui/core/...", "../core/..."), and a
// `data-testid` attribute on a kit button is refused. The vendored core (ui/core) is not scanned: it is edited and
// guarded in AA-CIS-App only.
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, sep } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCAN = ["app", "ui", "lib"];
const SKIP_DIRS = new Set(["node_modules", ".next"]);
const KIT_CORE = join("ui", "core");
const BUTTONS = join(ROOT, KIT_CORE, "Buttons.tsx");
// A kit import: the "@/ui" barrel, any "@/ui/core/..." file, or a relative path into the barrel or core.
const KIT_IMPORT = /^(@\/ui|@\/ui\/core(\/.*)?|(\.\.?\/)+(ui|core)(\/.*)?|(\.\.?\/)+ui\/core(\/.*)?)$/;

function sourceFiles(rel) {
  const abs = join(ROOT, rel);
  if (!existsSync(abs)) return [];
  if (statSync(abs).isFile()) return [rel];
  const out = [];
  for (const entry of readdirSync(abs, { withFileTypes: true })) {
    const child = join(rel, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name) && !entry.name.startsWith(".") && child !== KIT_CORE) out.push(...sourceFiles(child));
    } else if (entry.name.endsWith(".tsx") && !/\.test\.tsx$/.test(entry.name)) {
      out.push(child);
    }
  }
  return out;
}

function parse(file, code) {
  return ts.createSourceFile(file, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

/** The exported kit components whose DOM props come from `domProps()`. */
export function domPropsComponents(code = readFileSync(BUTTONS, "utf8")) {
  const sf = parse(BUTTONS, code);
  const names = new Set();
  for (const s of sf.statements) {
    if (!ts.isFunctionDeclaration(s) || !s.name || !s.body) continue;
    const exported = s.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
    if (exported && /\bdomProps\(/.test(s.body.getText(sf))) names.add(s.name.text);
  }
  return names;
}

/** `data-testid` attributes on kit button elements in one file, as "file:line <Tag>". */
export function plainTestIds(rel, kit, code) {
  const sf = parse(rel, code ?? readFileSync(join(ROOT, rel), "utf8"));
  const local = new Map(); // local name -> kit name
  for (const s of sf.statements) {
    if (!ts.isImportDeclaration(s) || !ts.isStringLiteral(s.moduleSpecifier) || !KIT_IMPORT.test(s.moduleSpecifier.text)) {
      continue;
    }
    const bindings = s.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const el of bindings.elements) {
      const original = (el.propertyName ?? el.name).text;
      if (kit.has(original)) local.set(el.name.text, original);
    }
  }
  if (!local.size) return [];
  const found = [];
  const visit = (node) => {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && local.has(node.tagName.getText(sf))) {
      for (const attr of node.attributes.properties) {
        if (ts.isJsxAttribute(attr) && attr.name.getText(sf) === "data-testid") {
          const line = sf.getLineAndCharacterOfPosition(attr.getStart(sf)).line + 1;
          found.push(`${rel.split(sep).join("/")}:${line} <${node.tagName.getText(sf)}>`);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

test("the kit's domProps family is found (Button, ChipButton, ListButton, Pressable)", () => {
  const kit = domPropsComponents();
  for (const name of ["Button", "ChipButton", "ListButton", "Pressable"]) assert.ok(kit.has(name), `${name} found`);
});

test("the scanner catches a data-testid on a kit button, aliased or not, and leaves testId and native tags alone", () => {
  const kit = domPropsComponents();
  const code = [
    'import { ChipButton, Button as KitButton } from "@/ui";',
    'import { Pressable } from "../core/primitives";',
    'import { Button as Other } from "@/ui/booking/fancy";',
    "export function X() {",
    "  return (<div>",
    '    <ChipButton selected data-testid="chip-a">A</ChipButton>',
    '    <KitButton data-testid="btn-b">B</KitButton>',
    '    <ChipButton selected testId="chip-c">C</ChipButton>',
    '    <button data-testid="native-d">D</button>',
    '    <Pressable data-testid="tile-e" />',
    '    <Other data-testid="other-f" />',
    "  </div>);",
    "}",
  ].join("\n");
  assert.deepEqual(plainTestIds(join("ui", "booking", "x.tsx"), kit, code), [
    "ui/booking/x.tsx:6 <ChipButton>",
    "ui/booking/x.tsx:7 <KitButton>",
    "ui/booking/x.tsx:10 <Pressable>",
  ]);
});

test("no kit button in the staff app takes a data-testid attribute (use the testId prop)", () => {
  const kit = domPropsComponents();
  const files = SCAN.flatMap(sourceFiles);
  assert.ok(files.length > 50, `scanned ${files.length} files`);
  assert.deepEqual(files.flatMap((rel) => plainTestIds(rel, kit)), []);
});
