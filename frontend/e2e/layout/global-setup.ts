// e2e/layout/global-setup.ts — AA-910: start every run with an empty measurement folder, so measurements.{json,md}
// hold only this run's tests (not rows left by an earlier run, a --grep run, or a page since removed from PAGES).
import fs from "node:fs";
import { MEASURE_DIR } from "./paths";

export default function globalSetup() {
  fs.rmSync(MEASURE_DIR, { recursive: true, force: true });
}
