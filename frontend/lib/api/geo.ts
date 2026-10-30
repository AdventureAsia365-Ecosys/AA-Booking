// lib/api/geo.ts
// AA-996 — typed calls for Catalog → Countries / Destinations (backend routers/catalog.py + app/catalog/geo.py,
// bk-029). Place content (name, country, coordinates, photos) is the CIS catalog read live (catalog.v_destination,
// v_destination_photo, v_tour) and is read-only here; Booking owns only the status, area and internal notes.
//
//   GET   /booking/catalog/geo/destinations?q=&country=&status=&visibility=&sort=&limit=&offset= (catalog.read)
//   GET   /booking/catalog/geo/destinations/{destination_id}                          (catalog.read)
//   PATCH /booking/catalog/geo/destinations/{destination_id}                          (catalog.admin: admin)
//   GET   /booking/catalog/geo/countries?q=&status=&area=                             (catalog.read)
//   GET   /booking/catalog/geo/countries/{iso}                                        (catalog.read)
//   PATCH /booking/catalog/geo/countries/{iso}                                        (catalog.admin: admin)

import { bkGet, bkSend } from "./client";
import type { IsoDateTime, OffsetPage } from "./types";

const enc = encodeURIComponent;

export type GeoStatus = "active" | "inactive";
/** AA-997: CIS keeps the place off customer maps (shown here to staff, with its coordinates, for review). */
export type HiddenReason = "location_unconfirmed" | "merged";
export type CustomerVisibility = "shown" | "hidden";

/** Booking state, shared by countries and destinations. */
export interface GeoState {
  status: GeoStatus;
  status_reason: string | null;
  internal_notes: string | null;
  profile_updated_at: IsoDateTime | null;
  profile_updated_by: string | null;
  profile_updated_by_name: string | null;
}

export interface DestinationItem extends GeoState {
  destination_id: string;
  name: string;
  /** CIS country name (text, not ISO). */
  country: string | null;
  /** ISO-2 when the name maps to Booking's country list. */
  country_code: string | null;
  lat: number | null;
  lng: number | null;
  cover_photo_path: string | null;
  cover_image_url: string | null;
  photo_count: number;
  /** AA-997: true when CIS hides the place from customers (its point is unconfirmed, or it was merged). */
  hidden_from_customers: boolean;
  hidden_reason: HiddenReason | null;
  /** The CIS place a merged place now points to. */
  merged_into: string | null;
}

export interface DestinationPhoto {
  photo_id: string;
  path: string;
  position: number | null;
}

export interface DestinationDetail extends DestinationItem {
  active_suppliers: number;
  active_hotels: number;
  active_services: number;
  photos: DestinationPhoto[];
  changed?: string[];
}

export interface DestinationSummary {
  total: number;
  active: number;
  inactive: number;
  countries: number;
  /** AA-997: places CIS keeps off customer maps. */
  hidden: number;
}

export interface DestinationPage extends OffsetPage<DestinationItem> {
  summary: DestinationSummary;
}

export interface DestinationQuery {
  q?: string;
  country?: string;
  status?: GeoStatus;
  visibility?: CustomerVisibility;
  sort?: string;
  limit?: number;
  offset?: number;
}

export interface CountryItem extends GeoState {
  country_code: string;
  name: string;
  destinations: number;
  /** Distinct CIS photo files of its places and of its tours' place-less photos. */
  photos: number;
  /** CIS photo path of the country cover (first photo of its best-covered place, else tour); null = no CIS photo. */
  cover_photo_path: string | null;
  tours: number;
  active_suppliers: number;
  area: string | null;
  /** In Booking's own country list (booking.countries): only then can Booking keep state (status, area, notes). */
  booking_reference: boolean;
}

export interface CountryTour {
  source_id: string;
  title: string | null;
  sku: string | null;
  duration_days: number | null;
  cover_photo_path: string | null;
  sellable: boolean;
}

/** One CIS photo of a country (gallery order: the cover first). Build the URL with `cisPhotoUrl(path)`. */
export interface CountryPhoto {
  path: string;
  /** Place name, else the tour photo's place label or the tour title. */
  caption: string | null;
  /** The CIS place the photo belongs to; null for a tour photo. */
  destination_id: string | null;
}

export interface CountryDetail extends CountryItem {
  destination_items: DestinationItem[];
  tour_items: CountryTour[];
  /** The first photos of the country (capped by the API; `photos` is the true count). */
  photo_items: CountryPhoto[];
  changed?: string[];
}

export interface CountryList {
  items: CountryItem[];
  total: number;
  areas: string[];
  /** CIS place country texts that CIS could not resolve to an ISO code (their places cannot be grouped). */
  unmapped: { name: string; destinations: number }[];
}

export interface GeoPatch {
  status?: GeoStatus;
  status_reason?: string | null;
  internal_notes?: string | null;
  area?: string | null;
}

export const geoApi = {
  destinations: (q: DestinationQuery = {}) => bkGet<DestinationPage>("catalog/geo/destinations", { ...q }),
  destination: (id: string) => bkGet<DestinationDetail>(`catalog/geo/destinations/${enc(id)}`),
  updateDestination: (id: string, body: Omit<GeoPatch, "area">) =>
    bkSend<DestinationDetail>(`catalog/geo/destinations/${enc(id)}`, { method: "PATCH", body }),
  countries: (q: { q?: string; status?: GeoStatus; area?: string } = {}) => bkGet<CountryList>("catalog/geo/countries", { ...q }),
  country: (iso: string) => bkGet<CountryDetail>(`catalog/geo/countries/${enc(iso)}`),
  updateCountry: (iso: string, body: GeoPatch) =>
    bkSend<CountryDetail>(`catalog/geo/countries/${enc(iso)}`, { method: "PATCH", body }),
};
