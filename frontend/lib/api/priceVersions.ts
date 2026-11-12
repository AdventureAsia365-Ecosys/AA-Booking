// lib/api/priceVersions.ts — AA-1091 (bk-053 / bk-054, root ADR 0005b d1, d2, d4, d9): price list versions of a trip
// plan, season calendars and holiday events (backend app/routers/price_versions.py). A hand price is never overwritten:
// a maker drafts a version, submits it with a reason (the diff and impact are frozen), a second person approves it with
// the SHA-256 of the diff they saw. Seasons, holiday events and a plan's calendar have the same maker-checker, with a
// reason on every proposal, withdrawal, removal request and rejection. Makers and checkers are people ({id, name}).
// Dates are inclusive here.
//
//   GET  /booking/catalog/trip-plans/{source_id}/price-versions        versions, which prices which dates, calendar
//   POST /booking/catalog/trip-plans/{source_id}/price-versions        new draft {copy_from, window, label...}
//   GET|PUT /booking/catalog/trip-plans/{source_id}/season-calendar    the plan's calendar / ask for a change (reason)
//   POST /booking/catalog/trip-plans/{source_id}/season-calendar/approve|reject|withdraw   a second person decides
//   GET  /booking/catalog/price-versions?status=pending                every version waiting for approval
//   GET|PUT /booking/catalog/price-versions/{id}                       one version / the maker saves a draft
//   POST /booking/catalog/price-versions/{id}/submit|approve|reject|withdraw, GET …/withdraw-preview
//   GET|POST /booking/pricing/season-calendars, GET …/{id}?year=, POST …/{id}/ranges, …/{id}/default[/approve|reject]
//   POST /booking/pricing/season-ranges/{id}/{action}
//   GET|POST /booking/pricing/holiday-events, POST /booking/pricing/holiday-events/{id}/{action}

import { bkGet, bkSend } from "./client";
import type { PriceRowBody } from "@/ui/booking/tripPlans";
import type { PriceRow } from "./tripPlans";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

import type { Person } from "@/ui/booking/priceVersions";
export type { Person };

export const VERSION_STATUSES = ["draft", "pending", "published", "superseded", "rejected", "withdrawn"] as const;
export type VersionStatus = (typeof VERSION_STATUSES)[number];
export type VersionSource = "staff" | "import" | "cost_sheet" | "provisional";
export const SEASON_CODES = ["low", "high", "peak", "festive"] as const;
export type SeasonCode = (typeof SEASON_CODES)[number];

export interface PriceVersion {
  id: string;
  trip_plan_id: string;
  source_id: string;
  version_no: number;
  label: string;
  tariff_year: number;
  travel_from: IsoDate;
  /** Inclusive: the last departure date the version prices. */
  travel_to: IsoDate;
  sale_from: IsoDate | null;
  sale_to: IsoDate | null;
  status: VersionStatus;
  source: VersionSource;
  reason: string | null;
  replaces_id: string | null;
  created_by: Person;
  created_at: IsoDateTime;
  submitted_at: IsoDateTime | null;
  approved_by: Person | null;
  approved_at: IsoDateTime | null;
  rejected_by: Person | null;
  reject_reason: string | null;
  withdrawn_by: Person | null;
  withdraw_reason: string | null;
  diff_sha256: string | null;
  row_count: number;
}

export interface DiffRow {
  position: number;
  channel: string;
  currency: string;
  pax_min: number;
  pax_max: number | null;
  season: string | null;
  valid_from: IsoDate;
  valid_to: IsoDate;
  old_amount: DecimalString | null;
  new_amount: DecimalString;
  change_pct: DecimalString | null;
  kind: "added" | "changed" | "unchanged";
  highlight: boolean;
  below_floor: boolean;
}

export interface VersionDiff {
  rows: DiffRow[];
  removed: { channel: string; currency: string; pax_min: number; pax_max: number | null; valid_from: IsoDate; valid_to: IsoDate; old_amount: DecimalString }[];
  summary: { added: number; changed: number; unchanged: number; removed: number; up: number; down: number; highlighted: number };
  highlight_pct: DecimalString;
}

export interface VersionImpact {
  open_quotes: number;
  departures: number;
  departure_dates: IsoDate[];
  b2b_rows_changed: number;
  rows_removed: number;
}

export interface VersionDetail extends PriceVersion {
  items: (PriceRow & { valid_to: IsoDate })[];
  diff: VersionDiff | null;
  impact: VersionImpact | null;
}

export interface InForceSpan {
  version_id: string;
  version_no: number;
  from: IsoDate;
  to: IsoDate;
}

export interface VersionList {
  source_id: string;
  items: PriceVersion[];
  in_force: InForceSpan[];
  calendar: { id: string; name: string | null; default_season: SeasonCode } | null;
}

export interface WithdrawPreview {
  priced_by: InForceSpan[];
  unquotable: { from: IsoDate; to: IsoDate }[];
}

export interface DraftFacts {
  label?: string | null;
  tariff_year?: number | null;
  travel_from: IsoDate;
  travel_to: IsoDate;
  sale_from?: IsoDate | null;
  sale_to?: IsoDate | null;
  reason?: string | null;
}

export const priceVersionsApi = {
  list: (sourceId: string) => bkGet<VersionList>(`catalog/trip-plans/${enc(sourceId)}/price-versions`),
  create: (sourceId: string, body: Partial<DraftFacts> & { copy_from?: string }) =>
    bkSend<VersionDetail>(`catalog/trip-plans/${enc(sourceId)}/price-versions`, { body }),
  pending: () => bkGet<{ items: PriceVersion[] }>("catalog/price-versions", { status: "pending" }),
  get: (id: string) => bkGet<VersionDetail>(`catalog/price-versions/${enc(id)}`),
  save: (id: string, body: DraftFacts & { items: PriceRowBody[]; below_floor_reason?: string | null }) =>
    bkSend<VersionDetail>(`catalog/price-versions/${enc(id)}`, { method: "PUT", body }),
  submit: (id: string, reason: string) => bkSend<VersionDetail>(`catalog/price-versions/${enc(id)}/submit`, { body: { reason } }),
  approve: (id: string, diffSha256: string) =>
    bkSend<VersionDetail>(`catalog/price-versions/${enc(id)}/approve`, { body: { diff_sha256: diffSha256 } }),
  reject: (id: string, reason: string) => bkSend<VersionDetail>(`catalog/price-versions/${enc(id)}/reject`, { body: { reason } }),
  withdraw: (id: string, reason: string) =>
    bkSend<VersionDetail>(`catalog/price-versions/${enc(id)}/withdraw`, { body: { reason } }),
  withdrawPreview: (id: string) => bkGet<WithdrawPreview>(`catalog/price-versions/${enc(id)}/withdraw-preview`),
  planCalendar: (sourceId: string) => bkGet<PlanCalendar>(`catalog/trip-plans/${enc(sourceId)}/season-calendar`),
  requestCalendar: (sourceId: string, calendarId: string | null, reason: string) =>
    bkSend<PlanCalendar>(`catalog/trip-plans/${enc(sourceId)}/season-calendar`, {
      method: "PUT",
      body: { season_calendar_id: calendarId, reason },
    }),
  decideCalendar: (sourceId: string, action: "approve" | "reject" | "withdraw", reason?: string) =>
    bkSend<PlanCalendar>(`catalog/trip-plans/${enc(sourceId)}/season-calendar/${action}`, {
      body: { reason: reason ?? null },
    }),
};

/** A plan's sell-side calendar and the change a second person still has to approve. */
export interface PlanCalendar {
  source_id: string;
  season_calendar_id: string | null;
  season_calendar_name: string | null;
  request: {
    season_calendar_id: string | null;
    season_calendar_name: string | null;
    requested_by: Person;
    requested_at: IsoDateTime;
    reason: string;
  } | null;
}

// ── Seasons and holiday events ──────────────────────────────────────────────

import type { ChangeAction, ChangeState } from "@/ui/booking/priceVersions";
export type { ChangeAction, ChangeState };

export interface SeasonCalendar {
  id: string;
  owner_kind: "destination" | "supplier_contract";
  destination_id: string | null;
  supplier_contract_id: string | null;
  name: string;
  default_season: SeasonCode;
  pending_default_season: SeasonCode | null;
  default_requested_by: Person | null;
  default_request_reason: string | null;
  created_by: Person;
  ranges: number;
  waiting: number;
  plans: number;
}

export interface SeasonRange {
  id: string;
  calendar_id: string;
  kind: "season" | "stop_sale";
  season_code: SeasonCode | null;
  date_from: IsoDate | null;
  date_to: IsoDate | null;
  holiday_country: string | null;
  holiday_code: string | null;
  days_before: number;
  days_after: number;
  /** The maker's reason for the range (required). */
  note: string;
  status: "pending" | "approved" | "rejected" | "withdrawn";
  state: ChangeState;
  created_by: Person;
  decided_by: Person | null;
  reject_reason: string | null;
  withdraw_reason: string | null;
  end_requested_by: Person | null;
  end_reason: string | null;
  ended_by: Person | null;
}

export interface CalendarDetail extends SeasonCalendar {
  ranges_list?: never;
  year?: number;
  segments?: { from: IsoDate; to: IsoDate; season: SeasonCode }[];
  stop_sales?: { from: IsoDate; to: IsoDate }[];
}

export interface HolidayEvent {
  id: string;
  country_iso2: string;
  code: string;
  name: string;
  year: number;
  date_from: IsoDate;
  date_to: IsoDate;
  /** The maker's reason for the event row (required). */
  reason: string;
  status: "pending" | "approved" | "rejected" | "withdrawn";
  state: ChangeState;
  created_by: Person;
  decided_by: Person | null;
  reject_reason: string | null;
  withdraw_reason: string | null;
  end_requested_by: Person | null;
  end_reason: string | null;
  ended_by: Person | null;
  calendars: number;
}

export interface RangeBody {
  kind: "season" | "stop_sale";
  season_code?: SeasonCode | null;
  date_from?: IsoDate | null;
  date_to?: IsoDate | null;
  holiday_country?: string | null;
  holiday_code?: string | null;
  days_before?: number;
  days_after?: number;
  note: string;
}

export const seasonsApi = {
  calendars: (ownerKind?: string) =>
    bkGet<{ items: SeasonCalendar[]; season_codes: SeasonCode[] }>("pricing/season-calendars", { owner_kind: ownerKind }),
  create: (body: { owner_kind: string; destination_id?: string | null; supplier_contract_id?: string | null; name: string }) =>
    bkSend<CalendarDetail>("pricing/season-calendars", { body }),
  calendar: (id: string, year?: number) =>
    bkGet<CalendarDetail & { ranges: SeasonRange[] }>(`pricing/season-calendars/${enc(id)}`, { year }),
  proposeRange: (id: string, body: RangeBody) => bkSend<SeasonRange>(`pricing/season-calendars/${enc(id)}/ranges`, { body }),
  proposeDefault: (id: string, code: SeasonCode, reason: string) =>
    bkSend<CalendarDetail>(`pricing/season-calendars/${enc(id)}/default`, { body: { default_season: code, reason } }),
  decideDefault: (id: string, decision: "approve" | "reject", reason?: string) =>
    bkSend<CalendarDetail>(`pricing/season-calendars/${enc(id)}/default/${decision}`, { body: { reason: reason ?? null } }),
  decideRange: (id: string, action: ChangeAction, reason?: string) =>
    bkSend<SeasonRange>(`pricing/season-ranges/${enc(id)}/${action}`, { body: { reason: reason ?? null } }),
  events: (country?: string, year?: number) =>
    bkGet<{ items: HolidayEvent[]; countries: { iso2: string; name: string }[] }>("pricing/holiday-events", { country, year }),
  proposeEvent: (body: { country_iso2: string; code: string; name: string; date_from: IsoDate; date_to: IsoDate; reason: string }) =>
    bkSend<HolidayEvent>("pricing/holiday-events", { body }),
  decideEvent: (id: string, action: ChangeAction, reason?: string) =>
    bkSend<HolidayEvent>(`pricing/holiday-events/${enc(id)}/${action}`, { body: { reason: reason ?? null } }),
};
