# AA-1097 (lane l15, S279): complexity and file-size ratchets

Parts 1-3 of AA-1097 in this repo. The same tool (`.github/scripts/code_health.py` + `code_health_eslint.mjs`) ships to
AA-CIS-App, AA-Partner and AA-TripPlanner-Web; only `.github/code-health.json` and the baseline differ.

## Decisions

- **Complexity limit 10, Python and TypeScript.** Measured on 13/11/2026, product code only:
  - Python (ruff C901, `backend/` minus tests): 2,249 functions, p90=6, p95=8, p97=9, p99=13, max 33. 45 functions in
    the baseline.
  - TypeScript under `frontend/` (ESLint `complexity`, classic; e2e tests and the vendored kit core excluded): 4,181
    functions, p90=5, p95=8, p97=12, p99=23, max 101. 150 functions in the baseline.
  - One number for both languages and all four repos (10 is between p95 and p97 everywhere, and the mccabe default).
- **File size 800 lines, tests included** (`.py .ts .tsx .js .jsx .mjs .cjs`): 704 files, 16 in the baseline (largest
  `backend/tests/unit/test_aa682_supplier_bills.py`, 1,120). Exempt with a reason: the vendored kit core
  (`frontend/ui/core/`) and vendored CIS copies (checked by "Vendored CIS copies" and ratcheted in CIS), the layout-check
  fixture `frontend/e2e/layout/fixtures.ts` (invented worst-case API data), `*/fixtures/*`, agent skill folders.
- **CI**: Python complexity + file size in **Lint** (ruff is already installed there from `backend/requirements-dev.txt`);
  TypeScript complexity in **Frontend Build** after `npm ci`. Both pass the PR / queue base so the baseline only
  shrinks. Both jobs are required checks: no branch-protection change.
- **Pre-push**: check id `code-health` for any source-file change (TypeScript part when `frontend/` or the ratchet files
  changed, always in `--full` / `--ci`); a missing ruff / `frontend/node_modules` is a visible skip outside `--ci`.
- Function identity, the shrink-only rules (NEW / WORSE / STALE / TIGHT, `update` never adds or raises), moves and
  splits: as in the AA-CIS-App notes of the same name; `code_health.py --help` has the summary.

## Changed

- Nothing beyond the issue; no file was refactored.

## Tradeoffs

- The limits live in a committed config, not in the Pricing > Policy settings: they are CI lint limits, which CI cannot
  read from the database; raising one fails `check --base`.

## Should know

- `python3 .github/scripts/code_health.py check` (all kinds) needs ruff 0.16.9 and `frontend/node_modules`.
- Tests: `backend/tests/unit/test_aa1097_code_health.py` (the Python end-to-end case needs ruff, the TypeScript names case
  needs `frontend/node_modules`; both skip otherwise).
