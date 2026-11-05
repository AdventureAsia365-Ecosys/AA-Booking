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
//     production host in .github/workflows/vercel-deploy.yml job `smoke`): 200, renders, no console error, no failed
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
import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.LAYOUT_PORT || 3199);
const STUB_PORT = Number(process.env.LAYOUT_STUB_PORT || 55481);
// AA-993 V5: set → the smoke-public project runs against this deployed URL and no local server is started.
const SMOKE_BASE_URL = process.env.SMOKE_BASE_URL?.replace(/\/+$/, "");
// AA-1008: set → the visual projects capture this deployed host (public pages) and no local server is started.
const VISUAL_BASE_URL = process.env.VISUAL_BASE_URL?.replace(/\/+$/, "");
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
  ],
  webServer: SMOKE_BASE_URL || VISUAL_BASE_URL ? undefined : [
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
