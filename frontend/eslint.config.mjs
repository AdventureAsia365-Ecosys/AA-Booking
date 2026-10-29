import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import { kitConfig } from "./ui/core/eslint-kit.mjs";

// AA-994 S4b (root ADR 0016 decision 7, plan guards G1-G11) — the UI kit conformance rules, vendored with the kit core
// (ui/core/eslint-kit.mjs, byte-identical in every app), `tailwind` preset at `error`:
//   no-raw-main, no-raw-table, no-raw-dialog, no-hex-colour, no-hand-styled-button, no-raw-aa-var,
//   no-aa-var-definition, no-legacy-kit.
// They replace the three AA-683 page rules (no raw <table>, no hand-rolled dialog, no raw hex). className stays allowed:
// Booking pages use Tailwind utilities generated from the same --aa-* variables (UI spec Q2).
// Scope: page code (app/**) and the Booking components (ui/booking/**). The kit core (ui/core, a byte copy of CIS) and
// the shell (ui/local, inline styles on K like the CIS shell) are not in scope. Inside ui/booking a component may style
// its own <button> (the plan's G5 applies outside ui/**).
// Violations that existed before S4b are frozen in eslint-suppressions.json (ESLint bulk suppressions); Frontend Build
// prunes it and fails when it is stale or grows (check-kit-copy.mjs --suppressions).
const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
  kitConfig({ preset: "tailwind", files: ["app/**/*.{ts,tsx}"] }),
  kitConfig({
    preset: "tailwind",
    files: ["ui/booking/**/*.{ts,tsx}"],
    rules: { "aa-kit/no-hand-styled-button": "off" },
  }),
  // The kit DataTable uses TanStack's useReactTable, which the React Compiler skips (same warning in CIS). The file is
  // the kit core copy, so the rule is silenced here instead of editing it.
  {
    files: ["ui/core/DataTable.tsx"],
    rules: {
      "react-hooks/incompatible-library": "off",
    },
  },
]);

export default eslintConfig;
