// playwright.config.ts
// Two projects:
//   • "layout-mock" (AA-910) — e2e/layout/*.spec.ts: every staff page that has a table, at 1440 x 900 (plus a 1280 squeeze
//     check), light and dark, against the PRODUCTION build (`npm run build` first) with the Booking API mocked:
//     browser calls to the BFF (/api/booking/*) are answered by route interception from e2e/layout/fixtures.ts, and
//     the one server-side call (GET /booking/staff/me from the (staff) layout) goes to e2e/layout/stub-api.mjs via
//     this server's BOOKING_API_URL. No real sign-in, no Dev data, no secret.
//
// Why a fake session cookie is safe: proxy.ts and the (staff) layout only DECODE the bk_access JWT (to route a
// missing / expiring token to the refresh or login page); they never trust it. Trust lives in the Booking API, which
// verifies the Cognito signature (backend app/auth). The test's unsigned token is accepted only by the stub above.
// Nothing in the app was changed or flagged for this check, so production auth is exactly as before.
//
// Run:   npm run build && npm run test:layout
// Shots: LAYOUT_SHOTS_DIR=<dir> (default e2e/layout/results), measurements.json + measurements.md land there too
//        (rebuilt from this run's tests only: e2e/layout/global-setup.ts clears the per-test files first).
// Clock: the spec freezes the browser clock at fixtures.ts CLOCK, so the result does not depend on the day it runs.
//
//   • "smoke-public" (AA-993 V5) — e2e/smoke-public/*.spec.ts: GET /login on a DEPLOYED admin (SMOKE_BASE_URL, the
//     production host in .github/workflows/deploy-admin-dev.yml job `smoke`): 200, renders, no console error, no failed
//     same-origin request, the next.config.ts security headers. Public page only: no sign-in, no secret.
//     With SMOKE_BASE_URL set no local server is started (nothing to build).
// Run:   npm run test:smoke-public   (defaults to https://booking.lumiguides.it.com; SMOKE_BASE_URL=<url> to change)
// Shots: SMOKE_SHOTS_DIR=<dir> (default e2e/smoke-public/results).
//
//   • "visual-1440" / "visual-390" (AA-1008, root ADR 0016 decision 9) — e2e/visual/visual.spec.ts: every selected page
//     at 1440 x 900 and 390 x 844, light and dark: no horizontal overflow (document and kit frame), 0 console errors,
//     no failed request, the kit frame, a PNG. Without VISUAL_BASE_URL it runs on the same local server and mocked API
//     as layout-mock (staff pages included); with it, the public pages of a deployed host. KIT_GALLERY=1 on the local
//     server serves the gated kit gallery route (the per-PR run and its planted-overflow self-test).
// Run:   npm run build && KIT_GALLERY=1 VISUAL_GALLERY=1 npx playwright test --project=visual-1440 --project=visual-390
//
//   • "signed-in" (AA-1073 / AA-1074, design e2e/SIGNED-IN-SMOKE.md) — e2e/signed-in/*.spec.ts: the DEPLOYED staff admin
//     signed in as the read-only E2E staff user (e2e-booking@…, booking-admin; the backend answers 403 e2e_read_only to
//     every write): shell + staff/me, Costs & profit, a trip plan's Departures tab and drawer, the B2B desk, and the
//     agency booking operations pages when deployed. Screenshots 1440 and 390, light and dark. Its setup project
//     "signed-in-setup" (e2e/signed-in/auth.setup.ts) asks for ONE email code, reads it from the SES test mailbox with
//     the runner's AWS credentials, and saves the session to e2e/results/.auth/ (gitignored). Agent-run, no CI job yet.
//     No local server (BOOKING_E2E_BASE_URL is set by the npm script; default the production host).
// Run:   AWS_PROFILE=aa365-admin AWS_REGION=us-west-1 npm run test:signed-in   (BOOKING_E2E_REUSE_STATE=1 reuses a live session)
// Shots: BOOKING_E2E_SHOTS_DIR=<dir> (default e2e/results/signed-in), one JSON per shot in report/.
import { defineConfig, devices } from "@playwright/test";
import { DEFAULT_BASE_URL, STATE_FILE } from "./e2e/signed-in/helpers/env";

const PORT = Number(process.env.LAYOUT_PORT || 3199);
const STUB_PORT = Number(process.env.LAYOUT_STUB_PORT || 55481);
// AA-993 V5: set → the smoke-public project runs against this deployed URL and no local server is started.
const SMOKE_BASE_URL = process.env.SMOKE_BASE_URL?.replace(/\/+$/, "");
// AA-1008: set → the visual projects capture this deployed host (public pages) and no local server is started.
const VISUAL_BASE_URL = process.env.VISUAL_BASE_URL?.replace(/\/+$/, "");
// AA-1073 / AA-1074: set (by `npm run test:signed-in`) → the signed-in projects run against this deployed host and no
// local server is started.
const SIGNED_IN_BASE_URL = process.env.BOOKING_E2E_BASE_URL?.replace(/\/+$/, "");
const SIGNED_IN_USE = {
  ...devices["Desktop Chrome"],
  baseURL: SIGNED_IN_BASE_URL || DEFAULT_BASE_URL,
  reducedMotion: "reduce" as const,
  // No trace or video: a trace records request headers and cookies (a live session), and the setup types a sign-in code.
  trace: "off" as const,
  video: "off" as const,
};
const VISUAL_USE = {
  ...devices["Desktop Chrome"],
  reducedMotion: "reduce" as const,
  screenshot: "off" as const,
  trace: "off" as const,
  // Scrollbars on (lesson S239): headless hides them, so a frame that fits by 2 px would pass here and scroll on screen.
  launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] },
};

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/layout/global-setup.ts",
  outputDir: "./e2e/layout/results/test-output",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1, // one Chromium at a time (WSL RAM budget); the whole run (88 tests) takes about 5 min
  retries: 0, // the API is mocked: a failure is a real layout change, not a flake
  reporter: process.env.CI ? [["list"], ["github"]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    headless: true,
    screenshot: "off", // the spec takes its own named screenshots
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "layout-mock",
      testMatch: /layout\/.*\.spec\.ts$/,
      use: {
        ...devices["Desktop Chrome"],
        // Headless Chromium hides scrollbars by default, which hides a real 6-17 px of width loss (lesson S239,
        // AA-683): turn them back on so the measurement matches a desktop browser.
        launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] },
      },
    },
    {
      name: "smoke-public",
      testMatch: /smoke-public\/.*\.spec\.ts$/,
      outputDir: "./e2e/smoke-public/results/test-output",
      // Against the live host a failure may be a network blip or the domain still moving to the new deployment.
      retries: process.env.CI ? 2 : 0,
      use: { ...devices["Desktop Chrome"], baseURL: SMOKE_BASE_URL },
    },
    {
      name: "visual-1440",
      testMatch: /visual\/.*\.spec\.ts$/,
      outputDir: "./e2e/visual/results/test-output",
      timeout: 120_000,
      use: { ...VISUAL_USE, viewport: { width: 1440, height: 900 } },
    },
    {
      name: "visual-390",
      testMatch: /visual\/.*\.spec\.ts$/,
      outputDir: "./e2e/visual/results/test-output",
      timeout: 120_000,
      use: { ...VISUAL_USE, viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
    },
    {
      // AA-1087 (root ADR 0006a A10e): the Dev-only E2E write identities, one write each on a synthetic agency's record
      // (e2e/e2e-writers/writes.spec.ts). Agent-only, never CI (codes in the MFA-admin-only mailbox prefix); every test
      // skips unless BOOKING_E2E_WRITERS=1; BOOKING_E2E_WRITERS_DRY_RUN=1 signs in and reads only.
      name: "e2e-writers",
      testMatch: /e2e-writers\/.*\.spec\.ts$/,
      outputDir: "./e2e/results/test-output",
      retries: 0, // a retry would ask for new codes and could repeat a write
      timeout: 240_000,
      use: { ...SIGNED_IN_USE, screenshot: "off" },
    },
    {
      name: "signed-in-setup",
      testMatch: /signed-in\/auth\.setup\.ts$/,
      outputDir: "./e2e/results/test-output",
      retries: 0, // a retry would ask Cognito for a second code
      use: { ...SIGNED_IN_USE, screenshot: "off" },
    },
    {
      name: "signed-in",
      testMatch: /signed-in\/.*\.spec\.ts$/,
      dependencies: ["signed-in-setup"],
      outputDir: "./e2e/results/test-output",
      timeout: 240_000, // four re-renders (1440 / 390, light / dark) of live pages per test
      retries: 1, // the live host: a network blip or a deployment rolling mid-run
      use: { ...SIGNED_IN_USE, storageState: STATE_FILE, screenshot: "only-on-failure", viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: SMOKE_BASE_URL || VISUAL_BASE_URL || SIGNED_IN_BASE_URL ? undefined : [
    {
      command: `node e2e/layout/stub-api.mjs ${STUB_PORT}`,
      url: `http://127.0.0.1:${STUB_PORT}/health`,
      reuseExistingServer: !process.env.CI,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: `npx next start --port ${PORT} --hostname 127.0.0.1`,
      url: `http://127.0.0.1:${PORT}/login`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: { BOOKING_API_URL: `http://127.0.0.1:${STUB_PORT}`, NEXT_TELEMETRY_DISABLED: "1", KIT_GALLERY: process.env.KIT_GALLERY ?? "" },
      stdout: "ignore",
      stderr: "pipe",
    },
  ],
});
