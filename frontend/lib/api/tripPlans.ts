// lib/api/tripPlans.ts
// AA-679 (S237 decision D4) — typed calls for Catalog → Trip Plans (backend routers/catalog.py +
// app/catalog/trip_plans.py, bk-009). Tour content (title, days, photos) is the CIS catalog read live
// (catalog.v_tour / v_tour_day / v_tour_photo) and is read-only here; Booking owns only the commercial row.
//
//   GET  /booking/catalog/trip-plans?q=&country=&sellable=&trip_style=&sort=&limit=&offset=   (catalog.read)
//   GET  /booking/catalog/trip-plans?orphaned=true&q=<source_id>&limit=&offset=  (AA-986 L5, catalog.read)
//   GET  /booking/catalog/trip-plans/facets                                                     (catalog.read)
//   GET  /booking/catalog/trip-plans/{source_id}                                                (catalog.read)
//   PUT  /booking/catalog/trip-plans/{source_id}/commercial                                     (trip_plan.write)
//   POST /booking/catalog/trip-plans/{source_id}/trip-cases                (trip_case.write + version.write)
//
// `source_id` is the stable CIS catalog key (contract catalog.tour.v1), never the CIS `tour_id`.

import { bkGet, bkSend } from "./client";
import type { Channel } from "./proposals";
import type { CatalogKeyState, DecimalString, IsoDateTime, OffsetPage, TripCaseDetail, VersionWithItems } from "./types";

const enc = encodeURIComponent;

export interface PriceHint {
  amount: DecimalString;
  currency: string;
  basis: "per_person" | "per_group";
}

export interface TripPlanListItem {
  source_system: "cis";
  source_id: string;
  tour_id: string;
  sku: string | null;
  title: string;
  country: string | null;
  duration_days: number | null;
  cover_photo_path: string | null;
  photo_count: number;
  lifecycle_stage: string | null;
  updated_at: IsoDateTime;
  plan_id: string | null;
  has_commercial_row: boolean;
  sellable: boolean;
  trip_style: string | null;
  /** null for staff without pricing.read (content creators). */
  default_channel: Channel | null;
  price_hint: PriceHint | null;
}

export interface TripPlanDay {
  day_number: number;
  title: string | null;
  body: string | null;
  places: string[];
}

export interface TripPlanPhoto {
  photo_id: string;
  path: string;
  place_label: string | null;
  position: number;
}

export interface TripPlanDetail extends TripPlanListItem {
  source_version: number | null;
  slug: string | null;
  subtitle: string | null;
  summary: string | null;
  description: string | null;
  highlights: unknown;
  duration_raw: string | null;
  inclusions: string | null;
  exclusions: string | null;
  best_time_to_go: string | null;
  group_size: string | null;
  itinerary_text: string | null;
  published_at: IsoDateTime;
  content_hash: string | null;
  commercial_notes: string | null;
  days: TripPlanDay[];
  photos: TripPlanPhoto[];
  cases_started: number;
  commercial: {
    created_at: IsoDateTime;
    updated_at: IsoDateTime;
    created_by: { id: string; name: string | null } | null;
    updated_by: { id: string; name: string | null } | null;
  } | null;
  /** Only on a PUT answer: the commercial fields that changed ([] = nothing saved). */
  changed?: string[];
}

/** AA-986 L5 (root ADR 0015): a Booking trip plan whose tour left the CIS catalog. No CIS content is left to show:
 *  the key, the commercial fields, the served state with its reason, and what is still attached (all kept). */
export interface OrphanedPlanItem {
  source_system: "cis";
  source_id: string;
  plan_id: string;
  orphaned: true;
  sellable: boolean;
  trip_style: string | null;
  default_channel: Channel | null;
  price_hint: PriceHint | null;
  updated_at: IsoDateTime;
  catalog: CatalogKeyState;
  open_cases: number;
  future_departures: number;
  confirmed_holds: number;
}

export interface TripPlanFacets {
  total: number;
  sellable: number;
  /** AA-986 L5: trip plans whose tour is no longer in the catalog. */
  orphaned: number;
  countries: { country: string; tours: number; sellable: number }[];
  trip_styles: string[];
}

export interface CommercialBody {
  sellable: boolean;
  trip_style: string | null;
  default_channel: Channel | null;
  price_hint: { amount: string; currency: string; basis: "per_person" | "per_group" } | null;
  commercial_notes: string | null;
}

export interface UseForTripCaseResult {
  mode: "new_case" | "new_version";
  trip_case: TripCaseDetail;
  version: VersionWithItems;
}

export type TripPlanQuery = {
  q?: string;
  country?: string;
  sellable?: boolean;
  trip_style?: string;
  sort?: string;
  limit?: number;
  offset?: number;
};

export const tripPlansApi = {
  list: (q: TripPlanQuery = {}) => bkGet<OffsetPage<TripPlanListItem>>("catalog/trip-plans", q),
  orphaned: (q: { q?: string; limit?: number; offset?: number } = {}) =>
    bkGet<OffsetPage<OrphanedPlanItem>>("catalog/trip-plans", { ...q, orphaned: true }),
  facets: () => bkGet<TripPlanFacets>("catalog/trip-plans/facets"),
  get: (sourceId: string) => bkGet<TripPlanDetail>(`catalog/trip-plans/${enc(sourceId)}`),
  saveCommercial: (sourceId: string, body: CommercialBody) =>
    bkSend<TripPlanDetail>(`catalog/trip-plans/${enc(sourceId)}/commercial`, { method: "PUT", body }),
  useForTripCase: (sourceId: string, body: Record<string, unknown>) =>
    bkSend<UseForTripCaseResult>(`catalog/trip-plans/${enc(sourceId)}/trip-cases`, { body }),
};
