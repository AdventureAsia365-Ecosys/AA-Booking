import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// AA-683 — UI conventions for page code (app/**), mirroring the CIS kit rules (AA-732):
//   1. no raw <table>            → use the kit DataTable (ui/DataTable.tsx)
//   2. no hand-rolled dialog     → use the kit Modal / Drawer, or ui/booking ConfirmDialog / FormDrawer
//   3. no raw hex colour literal → read a token (ui/tokens.ts `K`, or a Tailwind utility mapped to --aa-*)
// Unlike CIS, className IS allowed: Booking pages use Tailwind utilities generated from the same
// --aa-* variables (UI spec Q2). The copied kit in ui/ is exempt (it is a byte copy of CIS).
const PAGE_RESTRICTED_SYNTAX = [
  {
    selector: "JSXOpeningElement[name.name='table']",
    message: "No raw <table> in page code: use the kit DataTable (ui/DataTable.tsx).",
  },
  {
    selector: "JSXAttribute[name.name='role'][value.value='dialog']",
    message: "No hand-rolled dialog in page code: use ui Modal / Drawer / ConfirmDialog / FormDrawer.",
  },
  {
    selector: "Literal[value=/^#[0-9A-Fa-f]{3,8}$/]",
    message: "No raw hex colour in page code: use a token (K.* or a Tailwind utility mapped to --aa-*).",
  },
];

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
  {
    files: ["app/**/*.tsx", "ui/booking/**/*.tsx"],
    rules: {
      "no-restricted-syntax": ["error", ...PAGE_RESTRICTED_SYNTAX],
    },
  },
  // The copied kit DataTable uses TanStack's useReactTable, which the React Compiler skips (same
  // warning in CIS). The file is a byte copy, so the rule is silenced here instead of editing it.
  {
    files: ["ui/DataTable.tsx"],
    rules: {
      "react-hooks/incompatible-library": "off",
    },
  },
]);

export default eslintConfig;
