# AA-1096 — settings-lint over the whole Booking backend, as its own "Settings Lint" check

S279 thresholds audit (root `docs/audits/2026-11-13-S279-session/thresholds-audit/`, section 5.4): each app repo has a
`settings-lint` guard so a new hard-coded threshold, limit or cap fails CI, and it is a required check. Lane L3 added
`backend/tests/unit/test_aa1096_settings_lint.py` over the governed packages (b2b, ops, retention, pricing,
agency_billing; every pre-rule constant classified with a reason), run inside "Unit Tests". Lane notes:
`AA-1096.md`.

## Decisions

- **Rule 6, the rest of `app/` as a ratchet.** The same `lint()` now also scans every module outside the governed
  packages. Its 14/11/2026 findings (88) are a JSON baseline, `backend/tests/unit/snapshots/
  aa1096_settings_lint_rest_baseline.json`, unclassified and shrink-only. A new policy-looking constant or numeric env
  read anywhere in `app/` now fails unless it is a pricing policy parameter or carries `# technical-constant: <why>`.
- **The governed packages keep their classified dict baseline** (each entry says why: technical bound, job mechanics
  or known gap). Rule 6 does not touch them; classifying the 88 rest entries the same way is left to the PRs that
  touch those modules.
- **Keys without line numbers:** `<path>:<NAME>` for a constant, `<path>:env:<expression>` for an env number, so an
  edit above a constant does not churn the baseline.
- **The vendored CIS identity verifier is excluded** (`app/auth/identity_verifier.py`): AA-CIS-App lints it, and a marker
  added here would break the byte copy.
- **Own check name "Settings Lint"** (job `settings-lint` in `.github/workflows/ci.yml`), the same name in AA-Partner
  and AA-TripPlanner-Web, so branch protection can require it. Gated like Unit Tests (`needs: lint`, skipped on a
  docs-only PR, which counts as passed for a required check).

## Changed

- `backend/tests/unit/test_aa1096_settings_lint.py`: rule 6 (`rest_modules`, `rest_key`, `rest_findings`, three tests)
  and a `__main__` that rewrites the rule 6 baseline.
- `backend/tests/unit/snapshots/aa1096_settings_lint_rest_baseline.json` (new, 88 entries).
- `.github/workflows/ci.yml`: job "Settings Lint"; the header lists it as the ninth, non-required check.
- `backend/tests/unit/test_s271_ci_scope.py`: the job list, and a test that the job keeps its name and Unit Tests' gate.

## Tradeoffs

- **Duplicate run.** The lint file also runs inside "Unit Tests" (under a second). A job of its own costs about a
  minute of runner time per PR (dependency install from the pip cache) and buys a check name that says what failed and
  that can be required independently.
- **Booking's name pattern is wider** than CIS / CRM / TripPlanner (it also matches `DEFAULT`, `WINDOW`, `MINUTES`,
  `SECONDS`, `STEPS`, `BANDS`), so the rest baseline holds page defaults and timeouts too. Kept as is: L3 chose it, and
  each such entry is one marker away from leaving the list.

## Should know

- Making "Settings Lint" a required check is a ruleset / branch protection change on `main` (orchestrator step).
- Rewrite the rule 6 baseline only to remove entries: `python -m tests.unit.test_aa1096_settings_lint` from `backend/`.
