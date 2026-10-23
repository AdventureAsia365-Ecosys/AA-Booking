// lib/api/b2bDesk.ts
// AA-975 (bk-022) — typed calls for the B2B desk (backend routers/b2b_desk.py, app/b2b/desk.py). Read b2b_desk.read,
// write b2b_desk.write (admin, sales, contracting).
//
//   GET  /booking/b2b-desk/summary                                         open / overdue / due-soon lines, open incidents
//   GET  /booking/b2b-desk/on-request?state=open|answered|all&limit&offset  on-request lines of confirmed B2B bookings
//   POST /booking/b2b-desk/on-request/{hold_id}/lines/{line_ref}/accept | /decline {decline_code}
//   GET  /booking/b2b-desk/departures?from&to                              departures, seats, agencies, open incident
//   GET  /booking/b2b-desk/departures/{id}/incidents · POST (incident_code) = "Flag incident on departure"
//   POST /booking/b2b-desk/incidents/{id}/clear
//
// Agencies learn every answer, flag and clear as events on booking.b2b.v1 GET /events. No free text anywhere: an
// incident is a code, so it can never carry traveller data.

import { bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export const DECLINE_CODES = ["sold_out", "supplier_unavailable", "minimum_not_met", "other"] as const;
export type DeclineCode = (typeof DECLINE_CODES)[number];
export const DECLINE_LABEL: Record<DeclineCode, string> = {
  sold_out: "Sold out",
  supplier_unavailable: "Supplier unavailable",
  minimum_not_met: "Minimum group size not met",
  other: "Other",
};

export const INCIDENT_CODES = ["weather", "road", "health", "political", "supplier", "other"] as const;
export type IncidentCode = (typeof INCIDENT_CODES)[number];
export const INCIDENT_LABEL: Record<IncidentCode, string> = {
  weather: "Weather",
  road: "Road / access",
  health: "Health",
  political: "Political / security",
  supplier: "Supplier",
  other: "Other",
};

export type LineState = "open" | "answered" | "all";
export type Sla = "on_time" | "due_soon" | "overdue" | "answered" | "closed";

export interface PersonRef {
  id: string;
  name: string | null;
}

export interface OnRequestLine {
  hold_id: string;
  line_ref: string;
  status: "on_request" | "confirmed" | "declined" | "cancelled";
  sla: Sla;
  answer_due_at: IsoDateTime | null;
  requested_at: IsoDateTime;
  answered_at: IsoDateTime | null;
  answered_by: PersonRef | null;
  decline_code: DeclineCode | null;
  trip_case: { id: string; code: string };
  proposal_ref: string;
  partner: { id: string; agency_name: string; tenant_id: string };
  component: { id: string; kind: string; label: string | null; supplier: { id: string; name: string } | null };
  service_date: IsoDate;
  nights: number | null;
  pax: { adult: number; child: number; infant: number };
  seats: number;
  departure: { id: string; start_date: IsoDate } | null;
}

export interface OnRequestPage {
  items: OnRequestLine[];
  total: number;
  limit: number;
  offset: number;
  state: LineState;
}

export interface DeskSummary {
  on_request: { open: number; overdue: number; due_soon: number };
  open_incidents: number;
  due_soon_hours: number;
}

export interface Incident {
  id: string;
  departure_id: string;
  incident_code: IncidentCode;
  flagged_at: IsoDateTime;
  flagged_by: PersonRef;
  cleared_at: IsoDateTime | null;
  cleared_by: PersonRef | null;
  partners_notified?: number;
}

export interface DeskDeparture {
  id: string;
  trip_plan_id: string;
  source_id: string;
  tour_title: string | null;
  start_date: IsoDate;
  capacity: number;
  held: number;
  sold: number;
  available: number;
  status: "scheduled" | "guaranteed" | "cancelled";
  auto_confirm: boolean;
  partners: number;
  open_incident: Incident | null;
}

export interface DeskDepartures {
  items: DeskDeparture[];
  from: IsoDate;
  to: IsoDate;
}

export const b2bDeskApi = {
  summary: () => bkGet<DeskSummary>("b2b-desk/summary"),
  onRequest: (q: { state?: LineState; limit?: number; offset?: number }) =>
    bkGet<OnRequestPage>("b2b-desk/on-request", q),
  accept: (holdId: string, lineRef: string) =>
    bkSend<OnRequestLine>(`b2b-desk/on-request/${enc(holdId)}/lines/${enc(lineRef)}/accept`),
  decline: (holdId: string, lineRef: string, declineCode: DeclineCode) =>
    bkSend<OnRequestLine>(`b2b-desk/on-request/${enc(holdId)}/lines/${enc(lineRef)}/decline`, {
      body: { decline_code: declineCode },
    }),
  departures: (q: { from?: string; to?: string }) => bkGet<DeskDepartures>("b2b-desk/departures", q),
  incidents: (departureId: string) => bkGet<{ items: Incident[] }>(`b2b-desk/departures/${enc(departureId)}/incidents`),
  flag: (departureId: string, incidentCode: IncidentCode) =>
    bkSend<Incident>(`b2b-desk/departures/${enc(departureId)}/incidents`, { body: { incident_code: incidentCode } }),
  clear: (incidentId: string) => bkSend<Incident>(`b2b-desk/incidents/${enc(incidentId)}/clear`),
};
