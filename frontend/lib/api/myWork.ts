// lib/api/myWork.ts
// AA-1159 — the My work band of the staff dashboard (backend routers/my_work.py, app/my_work.py).
//
//   GET /booking/my-work/summary       the caller's tiles (by capability); 403 forbidden for a role with no tile
//   GET /booking/my-work/price-setup   AA-1190: the trip plans per next step of the price workbench (price setup
//                                      makers: price_version.write + pricing.read)

import { bkGet } from "./client";
import type { IsoDate, IsoDateTime } from "./types";
import type { MyWorkGroup } from "../myWork";

export interface MyWorkTile {
  key: string;
  capability: string;
  group: MyWorkGroup;
  /** The target list's query string for exactly the counted items ({} = the page's default view). */
  filter: Record<string, string>;
  /** false: the count could not be read (a table not migrated yet, no policy value in force). */
  available: boolean;
  count: number | null;
  /** When the oldest counted item entered the queue (null for a tile counted by due date). */
  oldest_at: IsoDateTime | null;
  /** The earliest due date among the counted items (null for a tile counted by age). */
  earliest_due: IsoDate | null;
  /** The "due soon" window in days (supplier_bill_due_soon_days), on that tile only. */
  days: number | null;
}

export interface MyWorkSummary {
  role: string;
  tiles: MyWorkTile[];
  generated_at: IsoDateTime;
}

export interface PriceSetupProgress {
  steps: string[];
  by_next_step: Record<string, number>;
  total: number;
  degraded: boolean;
  generated_at: IsoDateTime;
}

export const myWorkApi = {
  summary: () => bkGet<MyWorkSummary>("my-work/summary"),
  priceSetup: () => bkGet<PriceSetupProgress>("my-work/price-setup"),
};
