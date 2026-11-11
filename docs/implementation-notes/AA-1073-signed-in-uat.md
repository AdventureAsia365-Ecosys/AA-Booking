# AA-1073 / AA-1074 — signed-in UAT of the staff admin (lane l30, S277)

The tech lead asked that the agent check the Booking staff UI of the departures (AA-1073) and finance (AA-1074) work
itself with Playwright instead of handing the check back. This lane builds the signed-in smoke designed in
`frontend/e2e/SIGNED-IN-SMOKE.md` (AA-910) and runs it against Dev. E2E only: no app code changed.

## Decisions

- **Helpers copied, not imported.** `frontend/e2e/signed-in/helpers/mailbox.ts` and `cognitoSignIn.ts` are copies of the
  AA-CIS-App helpers (CIS commit named in each header). The recipient / address is a required parameter: the mailbox
  holds the CIS and CRM test users' mail too, so a default address would be a trap. The CIS-only address constants are
  dropped; `E2E_BOOKING_EMAIL` equals `backend/app/auth/staff.py` `E2E_TEST_STAFF_EMAIL`.
- **Two projects.** `signed-in-setup` (`auth.setup.ts`, `retries: 0` so a retry never asks Cognito for a second code)
  and `signed-in` (`dependencies` on the setup, `storageState`, `retries: 1`, `workers: 1` from the config). Both have
  trace and video off: a trace records request headers and cookies, which carry the session, and the setup types a
  sign-in code. Failure screenshots stay on for `signed-in` (staff pages only, never the managed-login host).
- **No local server.** The npm script `test:signed-in` sets `BOOKING_E2E_BASE_URL` (default the production host, the
  only registered callback); the config skips `webServer` when it is set, like `SMOKE_BASE_URL` / `VISUAL_BASE_URL`.
- **One sign-in per run, reusable.** `BOOKING_E2E_REUSE_STATE=1` keeps a saved session that still answers
  `GET staff/me` 200, so a rerun (or the orchestrator after an agent run) sends no new code mail.
- **All output under `frontend/e2e/results/`** (one gitignore line): `results/.auth/booking-staff.json` (a live refresh
  token) and `results/signed-in/*.png` + `report/*.json`.
- **Screenshots: one test re-renders each page four times** (1440 / 390, light / dark) through `capture()`, instead of
  four projects: the sign-in, the data lookup and the functional checks run once, and every PNG has the same data.
  Staff pages scroll inside `<main>`, so a full-page shot stops at the fold; `capture()` takes a `focus` locator that is
  scrolled into view before the shot (the first table row, the USD line, the allotments section).
- **Live data, not fixtures.** Each spec finds its subject through the BFF with the page's cookie (`page.request`, never
  a response body around a navigation, lesson S243): a case with supplier bookings (profit list, else recent cases),
  a departure (B2B desk list: at risk first, then with allotments, then any; else a plan with departures). When Dev has
  none, the test skips with the reason instead of failing.
- **Checks on every test** (`assertClean`): no uncaught page error, no same-origin 5xx, and no non-GET request at all
  (the walk is read-only; the backend would answer 403 `e2e_read_only`). Console errors and 4xx are written to
  `report/<slug>__errors.json` and the test annotations.
- **(d) agency booking operations**: the B2B desk "Agency bookings" tab and the manifest are not on main yet; the spec
  skips when the tab is absent, so it turns on by itself once that release is deployed.

## Changed

- `frontend/playwright.config.ts` (two projects, `webServer` skip), `frontend/package.json` (`test:signed-in`),
  `frontend/.gitignore`, `frontend/e2e/SIGNED-IN-SMOKE.md` (status: built; how to run), `CONTEXT.md` (one paragraph).

## Tradeoffs

- No CI job: the mailbox reader role does not trust this repo yet (SIGNED-IN-SMOKE.md last section lists the steps).
- `waitForSignInCode` keeps the CIS 90 s cap and the 60 s clock-skew window; the baseline key set is the real guard.

## Should know

- Run of 11/11/2026 on Dev (admin build `2d70c96`): 6 passed, 1 skipped (agency booking pages not deployed), 0
  console errors, 0 HTTP >= 400, 0 overflow in 36 shots. Shots and findings: root
  `docs/audits/2026-11-11-S277-booking-uat/README.md`.
- Findings left for the finance owner: the Departure costs tab shows the raw catalog id in its Tour column (the desk
  shows the tour title); the case Cost vs billed panel prints two USD summary lines when USD is the only currency.
