// e2e/layout/paths.ts — AA-910: where the layout check writes its screenshots and measurements (shared by the spec
// and the global setup that clears the per-test measurement files before each run).
import path from "node:path";

export const OUT = process.env.LAYOUT_SHOTS_DIR ? path.resolve(process.env.LAYOUT_SHOTS_DIR) : path.join(__dirname, "results");
export const MEASURE_DIR = path.join(OUT, ".measurements");
