# AA-1111 — lane l10 frontend dependency hygiene: Booking staff admin (frontend/)

S294. Dependency hygiene for the staff admin before the business-owner demo of 26/11. Before this change
`npm audit` in `frontend/` reported 1 high: zod 4.6.5 (GHSA-mrhx-c7xc-m3w5, fixed in 4.6.6). After it: 0
vulnerabilities.

## Decisions

- `zod` 4.6.5 -> 4.6.7. It is a dev-only dependency (`eslint-config-next` -> `eslint-plugin-react-hooks`), within
  the range its parent asks for, so package.json does not change. The lockfile diff is the one `zod` entry: npm 10
  also dropped the `libc` fields of four optional native packages when it rewrote the file; those hunks were put
  back by hand, so a newer npm in CI does not flip them again.
- `next` 16.4.6 and `@sentry/nextjs` 11.11.0 were already in place (the CIS admin catches up in its own PR).
- The npm audit gate of the AA-Partner CRM web is ported as-is: `.github/scripts/npm_audit_gate.py` (+ its unittest,
  run in Workflow Lint), `.github/npm-audit-allow.json` (empty: nothing needs accepting today) and a step "npm audit
  (staff admin, failing on high)" in the Security Audit job, which runs on every pull request.

## Changed

- Nothing beyond the brief.

## Tradeoffs

- The audit step runs on every pull request, backend-only ones too: a newly published high advisory in the admin's
  tree turns every Booking pull request red until it is fixed or accepted in the allowlist (at most 90 days). That is
  the point of the gate (the CRM web made the same trade-off).
- The 90-day ceiling is a constant in a CI script, not an admin setting: a CI policy, not an app threshold, and
  identical to the AA-Partner copy.

## Should know

- Verified locally: `npm ci`, `tsc --noEmit`, `eslint --max-warnings 0 --prune-suppressions` (suppressions file
  unchanged), `npm run test:unit`, `next build`, `npm audit` (0 vulnerabilities), the gate on the real report, and
  the mocked-API layout and visual runs (staff pages, login, kit gallery).
- `backend/tests/unit/test_aa1111_frontend_deps.py` pins the floor (patched zod, exact next / Sentry pins, the CI gate
  step).
- No change to `proxy.ts`, middleware, auth or session code.
