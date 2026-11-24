// lib/api/supply.ts
// AA-823 (S237) — typed calls for hotels, room types, services and supplier net rates (backend routers/supply.py,
// bk-010). Facts only: there is no customer-facing description field (ADR 0004a d2, Q17); CIS writes that.
// AA-835 S4 (bk-023): supplier photos (upload with a usage-rights flag CIS obeys) and the read-only `content` CIS
// published for a hotel / service (null until CIS serves catalog.v_supply_content; the panel then stays hidden).
//
//   GET   /booking/hotels?q=&status=&supplier=&country=&destination_id=&star_rating=&sort=&limit=&offset=  (supply.read)
//   POST  /booking/hotels · GET/PATCH /booking/hotels/{id|HTL-code} · POST …/deactivate | activate     (write: supply.write)
//   POST  /booking/hotels/{ref}/room-types · PATCH /booking/room-types/{id}
//   GET   /booking/hotels/{ref}/rates · /booking/services/{ref}/rates · /booking/contracts/{id}/rates
//   GET   /booking/services?q=&status=&type=&supplier=&country=&sort=&limit=&offset=
//   POST  /booking/services · GET/PATCH /booking/services/{id|SVC-code} · POST …/deactivate | activate
//   POST  /booking/contracts/{id}/rates · PATCH/DELETE /booking/rates/{id}       (draft contracts only: 409 contract_locked)
//   GET   /booking/supply/options?q=&kind=&supplier_id=                            room types + services to pick
//   GET   /booking/supply/rate-in-force?on=&hotel_room_type_id=|service_id=&pax=   the rate an item would take
//   GET   /booking/{hotels|services}/{ref}/media?include_removed=   photos + presigned preview URLs (supply.read)
//   POST  /booking/{hotels|services}/{ref}/media   {content_type, data_base64, usage_rights, filename}  (supply.write)
//   PATCH /booking/media/{id}   {usage_rights} | {status: "removed"} (removed is final)                  (supply.write)
//   GET   /booking/supply/restrictions?contract_id=&kind=   a contract's terms (AA-1145; AA-1153 part 2 inventory)
//   POST  /booking/contracts/{id}/restrictions · DELETE /booking/supply/restrictions/{id}   draft only (supply.write)

import { bkGet, bkSend } from "./client";
import type { SupplierActivity, SupplierPlace } from "./suppliers";
import type { DecimalString, IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export type RecordStatus = "active" | "inactive";
export const SERVICE_TYPES = ["activity", "transfer", "guide", "entrance", "meal"] as const;
export type ServiceType = (typeof SERVICE_TYPES)[number];
export const SERVICE_UNITS = ["person", "group", "vehicle", "day"] as const;
export type ServiceUnit = (typeof SERVICE_UNITS)[number];
export const MEAL_PLANS = ["RO", "BB", "HB", "FB", "AI"] as const;
export type MealPlan = (typeof MEAL_PLANS)[number];
export type RateUnit = ServiceUnit | "room_night";

interface SupplyRecord {
  id: string;
  code: string;
  supplier_id: string;
  supplier_code: string;
  supplier_name: string;
  supplier_status: RecordStatus;
  destination_id: string | null;
  /** The CIS place (catalog.v_destination); null when unset or the catalog cannot be read right now. */
  destination: SupplierPlace | null;
  country_code: string | null;
  country_name: string | null;
  internal_notes: string | null;
  status: RecordStatus;
  rates_in_force: number;
  created_by: string | null;
  updated_by: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface HotelLink {
  label: string;
  url: string;
}

export interface Hotel extends SupplyRecord {
  name: string;
  hotel_style: string | null;
  star_rating: number | null;
  address: string | null;
  latitude: DecimalString | number | null;
  longitude: DecimalString | number | null;
  check_in_time: string | null;
  check_out_time: string | null;
  amenities: string[];
  links: HotelLink[];
  room_type_count: number;
  /** List rows only (the photo grid): active supplier photos, and the first one as a presigned URL (15 minutes; null
   *  when the hotel has none or storage cannot sign). */
  photo_count?: number;
  cover_photo_url?: string | null;
}

export interface RoomType {
  id: string;
  hotel_id: string;
  name: string;
  max_occupancy: number | null;
  bed_types: string[];
  internal_notes: string | null;
  status: RecordStatus;
  rates_in_force: number;
  /** AA-1153: the names DMCs use for this room type (the rate-sheet import matches them); detail only. */
  aliases?: RoomTypeAlias[];
}

export interface RoomTypeAlias {
  id: string;
  room_type_id: string;
  supplier_id: string | null;
  supplier_code: string | null;
  alias: string;
}

/** AA-1153 (bk-069): a DMC that sells the hotel to AA, its own code for it, and whether it is the preferred one. */
export interface HotelSupplierLink {
  supplier_id: string;
  supplier_code: string;
  supplier_name: string;
  supplier_status: string;
  supplier_hotel_code: string | null;
  is_preferred: boolean;
  status: RecordStatus;
  first_listed: boolean;
  published_contracts: number;
}

export type FactsSource = "dmc_sheet" | "hotel_website" | "site_visit";

/** AA-1153: a hotel a new or edited one may duplicate (``block`` refuses, ``warn`` asks for a reason). */
export interface HotelCandidate {
  id: string;
  code: string;
  name: string;
  status: RecordStatus;
  similarity: number;
  distance_m: number | null;
  same_place: boolean;
  verdict: "block" | "warn";
}

/** CIS content status (CIS owns it); `none` = CIS holds nothing for this record yet. */
export type ContentStatus = "none" | "draft" | "in_review" | "published" | "stale" | "unpublished";

/** The customer content CIS published for a hotel / service (catalog.v_supply_content, read live). Text and photos are
 *  set only while `published`; `edit_url` is the CIS admin path, used as-is after CIS_ADMIN_URL. */
export interface SupplyContent {
  content_status: ContentStatus;
  content_version: number | null;
  description: string | null;
  highlights: string[];
  photo_urls: string[];
  edit_url: string | null;
  /** AA-1040: the AI mark of the published text; null while unpublished or before CIS serves it. */
  ai?: import("@/ui/booking/aiMark").AiMark | null;
}

export interface HotelDetail extends Hotel {
  room_types: RoomType[];
  /** AA-1153 (bk-069): the DMCs that sell the hotel, the facts confirmation and the codes of other systems. */
  suppliers?: HotelSupplierLink[];
  external_codes?: Record<string, string>;
  facts_source?: FactsSource | null;
  facts_source_supplier_id?: string | null;
  facts_confirmed_on?: string | null;
  facts_confirmed_by?: string | null;
  facts_reconfirm_due?: string | null;
  recent_activity: SupplierActivity[];
  /** null: the CIS read-back is off (CIS 278 not served yet, or unreadable right now). */
  content?: SupplyContent | null;
}

export interface Service extends SupplyRecord {
  service_type: ServiceType;
  title: string;
  unit: ServiceUnit;
  source_system: "cis" | null;
  source_id: string | null;
  max_group_size: number | null;
  duration_minutes: number | null;
  inclusions: string[];
}

export interface ServiceDetail extends Service {
  recent_activity: SupplierActivity[];
  content?: SupplyContent | null;
}

export const MEDIA_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;
export type MediaType = (typeof MEDIA_TYPES)[number];

export interface SupplyMedia {
  id: string;
  entity_type: "hotel" | "service";
  entity_id: string;
  content_type: MediaType;
  bytes: number;
  width: number | null;
  height: number | null;
  sha256: string;
  /** Contracting confirmed AA may publish the photo; CIS never uses it while false. */
  usage_rights: boolean;
  status: "active" | "removed";
  uploaded_by: { id: string; name: string | null };
  updated_by: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  /** Presigned preview URL (15 minutes); null for a removed photo or when storage is not configured. */
  url: string | null;
}

export interface MediaList {
  items: SupplyMedia[];
  max_bytes: number;
  max_active: number;
}

export interface MediaUpload {
  content_type: MediaType;
  data_base64: string;
  usage_rights?: boolean;
  filename?: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface HotelBody {
  supplier_id?: string;
  name?: string;
  hotel_style?: string | null;
  star_rating?: number | null;
  address?: string | null;
  destination_id?: string | null;
  country_code?: string | null;
  latitude?: string | null;
  longitude?: string | null;
  check_in_time?: string | null;
  check_out_time?: string | null;
  amenities?: string[];
  links?: { label?: string | null; url: string }[];
  internal_notes?: string | null;
  /** AA-1153: why a hotel that looks like another one nearby is a different hotel (10-300 characters, audited). */
  not_duplicate_reason?: string | null;
}

export interface ServiceBody {
  supplier_id?: string;
  service_type?: ServiceType;
  title?: string;
  unit?: ServiceUnit;
  destination_id?: string | null;
  country_code?: string | null;
  source_id?: string | null;
  max_group_size?: number | null;
  duration_minutes?: number | null;
  inclusions?: string[];
  internal_notes?: string | null;
}

export interface RoomTypeBody {
  name?: string;
  max_occupancy?: number | null;
  bed_types?: string[];
  internal_notes?: string | null;
  status?: RecordStatus;
}

export interface SupplierRate {
  id: string;
  contract_id: string;
  contract_title: string;
  contract_status: "draft" | "published" | "expired";
  supplier_id: string;
  hotel_room_type_id: string | null;
  room_type_name: string | null;
  hotel_id: string | null;
  hotel_code: string | null;
  hotel_name: string | null;
  service_id: string | null;
  service_code: string | null;
  service_title: string | null;
  season: string | null;
  valid_from: IsoDate;
  valid_to: IsoDate | null;
  unit: RateUnit;
  meal_plan: MealPlan | null;
  pax_min: number | null;
  pax_max: number | null;
  net_amount: DecimalString;
  currency: string;
  notes: string | null;
  /** Published contract and both validities cover today (UTC). */
  in_force: boolean;
  created_by: string;
  updated_by: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface RateBody {
  hotel_room_type_id?: string;
  service_id?: string;
  season?: string | null;
  valid_from?: IsoDate | null;
  valid_to?: IsoDate | null;
  meal_plan?: MealPlan | null;
  pax_min?: number | null;
  pax_max?: number | null;
  net_amount?: string;
  notes?: string | null;
}

export interface SupplyOption {
  kind: "room_type" | "service";
  id: string;
  code: string;
  label: string;
  supplier_name: string;
  destination_id: string | null;
  country_code: string | null;
  unit: RateUnit;
}

type Changed<T> = T & { changed: string[] };
export type ListQuery = {
  q?: string;
  status?: string;
  supplier?: string;
  country?: string;
  sort?: string;
  limit?: number;
  offset?: number;
};

export const supplyApi = {
  hotels: (q: ListQuery & { star_rating?: string } = {}) => bkGet<Page<Hotel>>("hotels", q),
  hotel: (ref: string) => bkGet<HotelDetail>(`hotels/${enc(ref)}`),
  createHotel: (body: HotelBody) => bkSend<HotelDetail>("hotels", { body }),
  updateHotel: (ref: string, body: HotelBody) => bkSend<Changed<HotelDetail>>(`hotels/${enc(ref)}`, { method: "PATCH", body }),
  setHotelStatus: (ref: string, active: boolean, reason?: string) =>
    bkSend<Changed<HotelDetail>>(`hotels/${enc(ref)}/${active ? "activate" : "deactivate"}`, { body: { reason: reason || undefined } }),
  createRoomType: (hotelRef: string, body: RoomTypeBody) => bkSend<RoomType>(`hotels/${enc(hotelRef)}/room-types`, { body }),
  updateRoomType: (id: string, body: RoomTypeBody) => bkSend<Changed<RoomType>>(`room-types/${enc(id)}`, { method: "PATCH", body }),
  hotelRates: (ref: string) => bkGet<{ items: SupplierRate[] }>(`hotels/${enc(ref)}/rates`),
  // AA-1153: the property master (DMC links, facts confirmation, room type aliases, duplicates).
  linkSupplier: (ref: string, body: { supplier_id: string; supplier_hotel_code?: string | null; is_preferred?: boolean }) =>
    bkSend<{ items: HotelSupplierLink[] }>(`hotels/${enc(ref)}/suppliers`, { body }),
  updateSupplierLink: (
    ref: string,
    supplierId: string,
    body: { supplier_hotel_code?: string | null; is_preferred?: boolean; status?: RecordStatus },
  ) => bkSend<{ items: HotelSupplierLink[] }>(`hotels/${enc(ref)}/suppliers/${enc(supplierId)}`, { method: "PATCH", body }),
  confirmFacts: (ref: string, body: { source: FactsSource; supplier_id?: string | null; confirmed_on?: string | null }) =>
    bkSend<HotelDetail>(`hotels/${enc(ref)}/facts-confirmation`, { body }),
  addAlias: (roomTypeId: string, body: { alias: string; supplier_id?: string | null }) =>
    bkSend<{ items: RoomTypeAlias[] }>(`room-types/${enc(roomTypeId)}/aliases`, { body }),
  removeAlias: (id: string) => bkSend<void>(`room-type-aliases/${enc(id)}`, { method: "DELETE" }),

  services: (q: ListQuery & { type?: string } = {}) => bkGet<Page<Service>>("services", q),
  service: (ref: string) => bkGet<ServiceDetail>(`services/${enc(ref)}`),
  createService: (body: ServiceBody) => bkSend<ServiceDetail>("services", { body }),
  updateService: (ref: string, body: ServiceBody) =>
    bkSend<Changed<ServiceDetail>>(`services/${enc(ref)}`, { method: "PATCH", body }),
  setServiceStatus: (ref: string, active: boolean, reason?: string) =>
    bkSend<Changed<ServiceDetail>>(`services/${enc(ref)}/${active ? "activate" : "deactivate"}`, { body: { reason: reason || undefined } }),
  serviceRates: (ref: string) => bkGet<{ items: SupplierRate[] }>(`services/${enc(ref)}/rates`),

  contractRates: (contractId: string) => bkGet<{ items: SupplierRate[] }>(`contracts/${enc(contractId)}/rates`),
  createRate: (contractId: string, body: RateBody) => bkSend<SupplierRate>(`contracts/${enc(contractId)}/rates`, { body }),
  updateRate: (id: string, body: RateBody) => bkSend<Changed<SupplierRate>>(`rates/${enc(id)}`, { method: "PATCH", body }),
  deleteRate: (id: string) => bkSend<void>(`rates/${enc(id)}`, { method: "DELETE" }),

  media: (kind: "hotel" | "service", ref: string, includeRemoved = false) =>
    bkGet<MediaList>(`${kind}s/${enc(ref)}/media`, { include_removed: includeRemoved || undefined }),
  uploadMedia: (kind: "hotel" | "service", ref: string, body: MediaUpload) =>
    bkSend<SupplyMedia>(`${kind}s/${enc(ref)}/media`, { body }),
  updateMedia: (id: string, body: { usage_rights?: boolean; status?: "removed" }) =>
    bkSend<Changed<SupplyMedia>>(`media/${enc(id)}`, { method: "PATCH", body }),

  options: (q: { q?: string; kind?: string; supplier_id?: string; limit?: number }) =>
    bkGet<{ items: SupplyOption[] }>("supply/options", q),

  // AA-1153 part 2 (B8): a contract's inventory terms (allotment / free sale / on request; stop-sales have their page).
  contractTerms: (contractId: string, kind: InventoryKind) =>
    bkGet<{ items: InventoryTerm[]; total: number }>("supply/restrictions", { contract_id: contractId, kind, limit: 100 }),
  createTerm: (contractId: string, body: InventoryTermBody) =>
    bkSend<InventoryTerm>(`contracts/${enc(contractId)}/restrictions`, { body }),
  deleteTerm: (id: string) => bkSend<void>(`supply/restrictions/${enc(id)}`, { method: "DELETE" }),
};

export const INVENTORY_KINDS = ["allotment", "free_sale", "on_request"] as const;
export type InventoryKind = (typeof INVENTORY_KINDS)[number];

/** One allotment / free-sale / on-request term of a contract (booking.supply_restrictions). */
export interface InventoryTerm {
  id: string;
  contract_id: string;
  component_id: string;
  hotel_code: string | null;
  hotel_name: string | null;
  room_type_name: string | null;
  service_code: string | null;
  service_title: string | null;
  inventory_kind: InventoryKind;
  date_from: IsoDate;
  date_to: IsoDate;
  units: number | null;
  release_days: number | null;
  max_pax: number | null;
  note: string | null;
  in_effect: boolean;
}

export interface InventoryTermBody {
  component_id: string;
  inventory_kind: InventoryKind;
  date_from: IsoDate;
  date_to: IsoDate;
  units?: number | null;
  /** empty = the pricing policy default (allotment only) */
  release_days?: number | null;
  max_pax?: number | null;
  note?: string | null;
}
