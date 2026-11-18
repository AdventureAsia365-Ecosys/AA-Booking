// frontend/lib/shellA11y.test.mjs
// AA-1131 (S283 UI audit, batch K1) — the Booking shell parts that are app code, not kit core:
//   1. the sidebar footer (Settings, staff card, Collapse) is pinned: the <aside> does not scroll, the nav does
//      (min-height 0), so with the Supply group open at 1440 x 900 Collapse is not cut at the bottom edge;
//   2. the PageHeader layout rules are the kit's (ui/core/PageHeaderStyles.tsx PAGE_HEADER_CSS), not a copy in shell.css.
// Run: npm run test:unit. Source checks (no DOM): the layout itself is measured by the layout-mock run.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const read = (p) => readFileSync(new URL(`../${p}`, import.meta.url), "utf8");

test("the sidebar scrolls its nav, not the whole aside, and the footer stays in view", () => {
  const src = read("ui/local/Sidebar.tsx");
  const aside = src.slice(src.indexOf("<aside"), src.indexOf("{/* Brand"));
  assert.match(aside, /overflow: "hidden"/);
  assert.doesNotMatch(aside, /overflowY: "auto"/);
  const nav = src.slice(src.indexOf("<nav"), src.indexOf(">", src.indexOf("gap: 18")));
  assert.match(nav, /minHeight: 0/);
  assert.match(nav, /overflowY: "auto"/);
  const footer = src.indexOf("{/* Settings, staff card, collapse");
  assert.ok(footer > src.indexOf("</nav>"), "the footer comes after the nav, outside it");
  assert.match(src.slice(footer, footer + 200), /flex: "none"/);
});

test("the PageHeader layout comes from the kit, not from a shell.css copy", () => {
  const css = read("ui/local/shell.css");
  assert.doesNotMatch(css, /^\.aa-page-header-text\s*\{/m);
  assert.match(read("ui/core/PageHeaderStyles.tsx"), /PAGE_HEADER_CSS/);
  assert.match(read("ui/core/primitives.tsx"), /<PageHeaderStyles \/>/);
});
