// e2e/signed-in/helpers/env.ts
// AA-1073 / AA-1074 — where the signed-in smoke runs and writes. Everything it writes lives under e2e/results/
// (gitignored): the storageState of the session (a live refresh token: never commit, never upload) and the PNGs.
import path from "node:path";

/** The deployed staff admin; the only host registered as a Cognito callback (booking-staff client, AA-911). */
export const DEFAULT_BASE_URL = "https://booking.lumiguides.it.com";

export function baseUrl(): string {
  return (process.env.BOOKING_E2E_BASE_URL || DEFAULT_BASE_URL).replace(/\/+$/, "");
}

export const RESULTS_DIR = process.env.BOOKING_E2E_SHOTS_DIR || path.join(__dirname, "..", "..", "results", "signed-in");
export const STATE_FILE = path.join(__dirname, "..", "..", "results", ".auth", "booking-staff.json");
