// lib/api/tripChecks.ts
// AA-682 S2 (bk-027) — typed calls for trip checks and the case lifecycle runs (backend routers/trip_checks.py,
// app/ops/trip_checks.py, app/ops/lifecycle.py). Read ops.read, write ops.write (admin, sales, contracting).
//
//   GET   /booking/trip-checks?status&due_before&assignee&code&case&q&limit&offset   checks across open sold cases
//   GET   /booking/trip-cases/{ref}/checks                                         one case: checks, board, derived
//   POST  /booking/trip-cases/{ref}/checks                                         add a custom check
//   PATCH /booking/trip-checks/{id}                                                status, assignee, due, note, contact
//   GET   /booking/case-lifecycle/runs                                             latest lifecycle / reminder runs
//
// Three standard checks are seeded when a case is confirmed (re-check bookings, emergency contact, insurance); the
// emergency contact is B2C only (a B2B trip keeps no traveller data in Booking). "Suppliers confirmed" and "Payment
// verified" are derived, never stored; payment arrives with invoices (S3).

import { bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type TripCheckStatus = "open" | "done" | "not_applicable";
export type TripCheckCode = "pre_trip_recheck" | "emergency_contact" | "insurance" | "custom";

export const TRIP_CHECK_CODE_LABEL: Record<TripCheckCode, string> = {
  pre_trip_recheck: "Re-check bookings",
  emergency_contact: "Emergency contact",
  insurance: "Insurance",
  custom: "Custom",
};

export interface TripCheck {
  id: string;
  code: TripCheckCode;
  title: string;
  status: TripCheckStatus;
  due_date: IsoDate | null;
  assignee_ref: string | null;
  assignee: { id: string; name: string | null } | null;
  done_by: { id: string; name: string | null } | null;
  done_at: IsoDateTime | null;
  note: string | null;
  emergency_contact_name: string | null;
  emergency_contact_phone: string | null;
  cleared_at: IsoDateTime | null;
  created_by: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  standard: boolean;
  contact_allowed: boolean;
  trip_case: {
    id: string;
    code: string;
    status: string;
    channel: "b2c" | "b2b";
    start_date: IsoDate | null;
    end_date: IsoDate | null;
    customer_name: string | null;
    /** AA-996, /trip-checks rows only: the trip's stored checks (progress) and its sales owner. */
    checks?: { total: number; done: number; not_applicable: number };
    sales_name?: string | null;
  };
}

export interface CheckBoard {
  checks_total: number;
  checks_open: number;
  checks_done: number;
  checks_not_applicable: number;
  checks_overdue: number;
  next_due_date: IsoDate | null;
  supply_state: "none" | "confirming" | "booked";
  suppliers_confirmed: boolean;
  payment_verified: boolean | null;
}

export interface DerivedCheck {
  code: "suppliers_confirmed" | "payment_verified";
  title: string;
  status: "open" | "done" | null;
  detail: string | null;
  available: boolean;
}

export interface CaseChecks {
  trip_case: { id: string; code: string; status: string; channel: "b2c" | "b2b"; start_date: IsoDate | null; end_date: IsoDate | null };
  board: CheckBoard | null;
  items: TripCheck[];
  derived: DerivedCheck[];
  contact_allowed: boolean;
  writable: boolean;
}

/** AA-996: the /trip-checks tab counts under the current owner / check / search filters (every status). */
export interface TripCheckCounts {
  open: number;
  overdue: number;
  done: number;
  not_applicable: number;
  all: number;
}

export interface TripCheckPage {
  items: TripCheck[];
  total: number;
  limit: number;
  offset: number;
  /** Optional: an API before AA-996 does not send it (the tabs then show no counts). */
  counts?: TripCheckCounts;
}

export interface CheckChanges {
  status?: TripCheckStatus;
  title?: string;
  due_date?: IsoDate | null;
  assignee_ref?: string | null;
  note?: string | null;
  emergency_contact_name?: string | null;
  emergency_contact_phone?: string | null;
}

export interface LifecycleRun {
  id: string;
  kind: "bk_case_lifecycle" | "bk_ops_reminders";
  status: string;
  created_by: string | null;
  created_at: IsoDateTime;
  started_at: IsoDateTime | null;
  finished_at: IsoDateTime | null;
  attempt: number;
  result: Record<string, unknown> | null;
  error: string | null;
}

export const TRIP_CHECK_STATUSES: TripCheckStatus[] = ["open", "done", "not_applicable"];

export const tripChecksApi = {
  list: (q: {
    status?: string;
    due_before?: string;
    assignee?: string;
    code?: string;
    case?: string;
    q?: string;
    limit?: number;
    offset?: number;
  }) => bkGet<TripCheckPage>("trip-checks", q),
  forCase: (ref: string) => bkGet<CaseChecks>(`trip-cases/${enc(ref)}/checks`),
  create: (ref: string, body: { title: string; due_date?: IsoDate | null; assignee_ref?: string | null; note?: string | null }) =>
    bkSend<TripCheck>(`trip-cases/${enc(ref)}/checks`, { body }),
  update: (id: string, body: CheckChanges) =>
    bkSend<TripCheck & { changed: string[] }>(`trip-checks/${enc(id)}`, { method: "PATCH", body }),
  runs: (limit = 20) => bkGet<{ items: LifecycleRun[]; scheduled_mode: "dry_run" | "live"; schedule_utc_hour: number }>(
    "case-lifecycle/runs",
    { limit },
  ),
};
