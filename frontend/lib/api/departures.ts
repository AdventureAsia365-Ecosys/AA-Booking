// lib/api/departures.ts
// AA-973 / AA-1073 (bk-020, bk-046) — typed calls for the fixed departures of a trip plan and their agency allotments
// (backend routers/catalog.py, app/b2b/departures.py, app/b2b/allotments.py, app/b2b/departure_watch.py).
//
//   GET   /booking/catalog/trip-plans/{source_id}/departures?from&to        departures with seats (supply.read)
//   POST  /booking/catalog/trip-plans/{source_id}/departures                one departure (trip_plan.write)
//   POST  /booking/catalog/trip-plans/{source_id}/departures:generate       weekly / monthly pattern, dry_run preview
//   PATCH /booking/catalog/departures/{id}                                  capacity, status, minimum, cutoff, decision
//   GET   /booking/catalog/departures/{id}/allotments                       allotments + agency options (b2b_desk.read)
//   POST  /booking/catalog/departures/{id}/allotments                       propose (b2b_desk.write, maker)
//   POST  /booking/catalog/allotments/{id}/approve | /end                   approve (another person) / reject or withdraw
//   POST  /booking/b2b-desk/departure-checks/runs {dry_run}                 run the watch + release now
//   GET   /booking/b2b-desk/departure-checks/runs                           latest runs
//
// A minimum of 0 means "no minimum" (the status is managed by hand). Sales close on start - cutoff (that day included).
// On the decision date (start - decision days) a scheduled departure short of its minimum is flagged at risk.

import { bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type DepartureStatus = "scheduled" | "guaranteed" | "cancelled";

export interface Departure {
  id: string;
  trip_plan_id: string;
  start_date: IsoDate;
  capacity: number;
  held: number;
  sold: number;
  /** Free for any agency: capacity - held - sold - unused allotments. */
  available: number;
  allotted: number;
  allotment_unused: number;
  status: DepartureStatus;
  auto_confirm: boolean;
  min_to_operate: number;
  /** null without a minimum; 0 once met or no longer scheduled. */
  seats_to_guarantee: number | null;
  sales_cutoff_days: number;
  decision_days_before: number;
  sales_close_date: IsoDate;
  decision_date: IsoDate;
  at_risk_at: IsoDateTime | null;
  at_risk: boolean;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

/** What a new departure gets when the form leaves a rule empty (the pricing policy in force today). */
export interface DepartureDefaults {
  decision_days_before: number;
}

export interface DepartureList {
  trip_plan_id: string;
  source_id: string;
  items: Departure[];
  defaults: DepartureDefaults;
}

export interface DepartureRules {
  capacity: number;
  min_to_operate: number;
  sales_cutoff_days: number;
  decision_days_before: number;
  auto_confirm: boolean;
}

export interface NewDeparture extends DepartureRules {
  start_date: IsoDate;
}

export type DeparturePatch = Partial<DepartureRules> & { status?: DepartureStatus };

export type GeneratePattern = { kind: "weekly"; weekdays: number[] } | { kind: "monthly"; day: number };

export interface GenerateRequest extends DepartureRules {
  date_from: IsoDate;
  date_to: IsoDate;
  pattern: GeneratePattern;
  dry_run: boolean;
}

export interface GenerateResult {
  dry_run: boolean;
  created: IsoDate[];
  skipped: IsoDate[];
  created_count: number;
  skipped_count: number;
}

export type AllotmentState = "pending" | "in_force" | "released" | "ended";

export interface PersonRef {
  id: string;
  name: string | null;
}

export interface Allotment {
  id: string;
  departure_id: string;
  partner: { id: string; agency_name: string };
  seats: number;
  release_days_before: number;
  release_date: IsoDate;
  state: AllotmentState;
  replaces_id: string | null;
  created_by: PersonRef | null;
  created_at: IsoDateTime;
  approved_by: PersonRef | null;
  approved_at: IsoDateTime | null;
  released_at: IsoDateTime | null;
  ended_by: PersonRef | null;
  ended_at: IsoDateTime | null;
  end_reason: "replaced" | "withdrawn" | "rejected" | "departure_cancelled" | null;
  used: number | null;
  unused: number | null;
}

export interface AllotmentList {
  departure: Departure;
  items: Allotment[];
  partner_options: { id: string; agency_name: string }[];
}

export interface CheckRun {
  id: string;
  kind: "bk_departure_watch" | "bk_allotment_release";
  status: string;
  created_by: string | null;
  created_at: IsoDateTime;
  finished_at: IsoDateTime | null;
  result: Record<string, unknown> | null;
  error: string | null;
}

export const WEEKDAYS: { value: number; label: string }[] = [
  { value: 1, label: "Mon" },
  { value: 2, label: "Tue" },
  { value: 3, label: "Wed" },
  { value: 4, label: "Thu" },
  { value: 5, label: "Fri" },
  { value: 6, label: "Sat" },
  { value: 7, label: "Sun" },
];

export const ALLOTMENT_STATE_LABEL: Record<AllotmentState, string> = {
  pending: "Waiting for approval",
  in_force: "In force",
  released: "Released",
  ended: "Ended",
};

export const END_REASON_LABEL: Record<NonNullable<Allotment["end_reason"]>, string> = {
  replaced: "replaced",
  withdrawn: "withdrawn",
  rejected: "rejected",
  departure_cancelled: "departure cancelled",
};

/** "3 of 4 sold" style label of the minimum, or null without one. */
export function minimumLabel(d: Pick<Departure, "min_to_operate" | "sold" | "status">): string | null {
  if (!d.min_to_operate) return null;
  if (d.status === "guaranteed") return `Minimum ${d.min_to_operate} met`;
  return `${d.sold} of ${d.min_to_operate} sold`;
}

export const departuresApi = {
  list: (sourceId: string, q: { from?: string; to?: string } = {}) =>
    bkGet<DepartureList>(`catalog/trip-plans/${enc(sourceId)}/departures`, q),
  create: (sourceId: string, body: NewDeparture) =>
    bkSend<Departure>(`catalog/trip-plans/${enc(sourceId)}/departures`, { body }),
  generate: (sourceId: string, body: GenerateRequest) =>
    bkSend<GenerateResult>(`catalog/trip-plans/${enc(sourceId)}/departures:generate`, { body }),
  update: (departureId: string, body: DeparturePatch) =>
    bkSend<Departure>(`catalog/departures/${enc(departureId)}`, { method: "PATCH", body }),
  allotments: (departureId: string) => bkGet<AllotmentList>(`catalog/departures/${enc(departureId)}/allotments`),
  proposeAllotment: (departureId: string, body: { partner_id: string; seats: number; release_days_before: number }) =>
    bkSend<Allotment>(`catalog/departures/${enc(departureId)}/allotments`, { body }),
  approveAllotment: (allotmentId: string) => bkSend<Allotment>(`catalog/allotments/${enc(allotmentId)}/approve`),
  endAllotment: (allotmentId: string) => bkSend<Allotment>(`catalog/allotments/${enc(allotmentId)}/end`),
  runChecks: (dryRun: boolean) =>
    bkSend<{ dry_run: boolean; jobs: Record<string, string | null> }>("b2b-desk/departure-checks/runs", {
      body: { dry_run: dryRun },
    }),
  checkRuns: () => bkGet<{ items: CheckRun[] }>("b2b-desk/departure-checks/runs", { limit: 10 }),
};
