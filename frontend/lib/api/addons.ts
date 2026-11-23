// lib/api/addons.ts
// AA-1151 (bk-068, PR-29) — add-on offers: optional activities AA sells per place and day (backend routers/addons.py,
// app/supply/addons.py). Every staff role reads them (addon.read, no net shown); content creators and contracting
// write drafts (addon.write); a second person publishes (addon.approve, never one of the draft's writers: 403
// maker_checker). The activity catalogue import creates services, rates and a draft contract too (supply.write +
// addon.write). The expiring contracts list needs supply.read.
//
//   GET   /booking/add-ons?status=&q=&approvable=me&limit=&offset=    the offers, newest change first
//   GET   /booking/add-ons/{id}                                        one offer + actions.publish + audit
//   PATCH /booking/add-ons/{id}                                        rules of a draft; display order of a published one
//   POST  /booking/add-ons/{id}/publish | reopen | retire
//   GET   /booking/activity-sheets/template                            the empty workbook
//   POST  /booking/suppliers/{ref}/activity-sheets[?dry_run=true]      {data_base64, filename}
//   GET   /booking/contracts/expiring                                  published contracts ending within the window

import { BFF_BASE, bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type AddonStatus = "draft" | "published" | "retired";
export type AddonMissing = "no_place" | "no_price" | "service_inactive" | "supplier_inactive" | "no_content";

export interface AddonPlace {
  destination_id: string;
  role: "operates" | "pickup";
  name: string | null;
  country: string | null;
}

export interface AddonOffer {
  id: string;
  component_id: string;
  service_id: string;
  service_code: string;
  service_title: string;
  service_type: string;
  unit: string;
  supplier_code: string;
  supplier_name: string;
  status: AddonStatus;
  min_pax: number;
  cutoff_hours: number;
  operating_weekdays: number[];
  start_times: string[];
  child_pct: string | null;
  sort_order: number;
  featured: boolean;
  writers: string[];
  published_by: string | null;
  published_at: IsoDateTime | null;
  owner_self_approved: boolean;
  updated_at: IsoDateTime;
  places: AddonPlace[];
  missing: AddonMissing[];
  content_published: boolean | null;
}

export interface AddonPage {
  items: AddonOffer[];
  total: number;
}

export interface ExpiringContract {
  id: string;
  title: string;
  supplier_code: string;
  supplier_name: string;
  valid_to: IsoDate;
  days_left: number;
  announced: boolean;
}

export interface ActivitySheetProblem {
  sheet: string;
  row: number | null;
  column: string | null;
  code: string;
  message: string;
}

export interface ActivitySheetResult {
  dry_run: boolean;
  contract: { title: string | null; currency?: string | null; status?: string };
  activities: { row: number; title: string; places: { destination_id: string; name: string }[] }[];
  imported?: number;
  ignored_sheets: string[];
}

const WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "Mon, Thu" for ISO weekdays (1 = Monday), "Daily" for all seven; `tr` translates each word (the page's t). */
export function weekdaysLabel(days: number[], tr: (word: string) => string = (w) => w): string {
  if (days.length === 7) return tr("Daily");
  return days.map((d) => tr(WEEKDAY[d - 1] ?? String(d))).join(", ");
}

/** Who may publish: never a writer of the draft (the business owner's self-approval is decided by the API). */
export function wroteIt(offer: AddonOffer, staffId: string | undefined): boolean {
  return !!staffId && offer.writers.includes(staffId);
}

async function template(): Promise<Blob> {
  const res = await fetch(`${BFF_BASE}/activity-sheets/template`, { cache: "no-store" });
  if (!res.ok) throw new Error("The template could not be downloaded.");
  return res.blob();
}

export const addonsApi = {
  list: (q: { status?: AddonStatus; q?: string; approvable?: "me"; limit?: number; offset?: number } = {}) =>
    bkGet<AddonPage>("add-ons", q),
  publish: (id: string) => bkSend<AddonOffer>(`add-ons/${enc(id)}/publish`),
  reopen: (id: string) => bkSend<AddonOffer>(`add-ons/${enc(id)}/reopen`),
  retire: (id: string) => bkSend<AddonOffer>(`add-ons/${enc(id)}/retire`),
  expiring: () => bkGet<{ window_days: number; items: ExpiringContract[] }>("contracts/expiring"),
  template,
  importSheet: (supplierRef: string, dataBase64: string, filename: string, dryRun: boolean) =>
    bkSend<ActivitySheetResult>(`suppliers/${enc(supplierRef)}/activity-sheets${dryRun ? "?dry_run=true" : ""}`, {
      body: { data_base64: dataBase64, filename },
    }),
};
