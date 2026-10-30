// lib/api/suppliers.ts
// AA-822 (S237) — typed calls for Suppliers and supplier contracts (backend routers/suppliers.py, bk-008) and the
// CIS place picker (routers/catalog.py over catalog.v_destination, CIS 225).
//
//   GET   /booking/suppliers?q=&status=&category=&country=&sort=&limit=&offset=   list         (supply.read)
//   GET   /booking/suppliers/summary                                              KPI strip    (supply.read)
//   POST  /booking/suppliers                                                      create       (supply.write)
//   GET   /booking/suppliers/{id|SUP-code}                                        detail       (supply.read)
//   PATCH /booking/suppliers/{id|SUP-code}                                        edit         (supply.write)
//   POST  /booking/suppliers/{ref}/deactivate | activate  {reason?}               409 supplier_has_contracts
//   GET   /booking/suppliers/{ref}/contracts                                      contracts    (supply.read)
//   POST  /booking/suppliers/{ref}/contracts                                      new draft    (supply.write)
//   PATCH /booking/contracts/{id}   terms of a draft, or {status: published|expired} on its own
//                                   (publish by a second person: 403 maker_checker)
//   GET   /booking/catalog/destinations?q=&country=&limit=                        CIS places   (any staff)

import { bkGet, bkSend } from "./client";
import type { IsoDate, IsoDateTime } from "./types";

const enc = encodeURIComponent;

export const SUPPLIER_CATEGORIES = ["hotel", "transport", "guide", "entrance", "activity", "meal"] as const;
export type SupplierCategory = (typeof SUPPLIER_CATEGORIES)[number];
export type SupplierStatus = "active" | "inactive";

export interface SupplierPlace {
  id: string;
  name: string;
  country: string;
}

export interface Supplier {
  id: string;
  code: string;
  company_name: string;
  categories: SupplierCategory[];
  website: string | null;
  internal_notes: string | null;
  contact_name: string | null;
  contact_title: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  address: string | null;
  region: string | null;
  postal_code: string | null;
  destination_id: string | null;
  /** The CIS place (catalog.v_destination); null when unset or the catalog cannot be read right now. */
  destination: SupplierPlace | null;
  country_code: string | null;
  country_name: string | null;
  status: SupplierStatus;
  contracts_in_force: number;
  created_by: string | null;
  updated_by: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface SupplierActivity {
  id: string;
  at: IsoDateTime;
  action: string;
  entity_type: string;
  entity_label: string | null;
  summary: string | null;
  actor: { id: string | null; name: string | null };
}

export interface SupplierDetail extends Supplier {
  contract_counts: { total: number; drafts: number; in_force: number };
  recent_activity: SupplierActivity[];
}

export interface SupplierList {
  items: Supplier[];
  total: number;
  limit: number;
  offset: number;
}

export interface SupplierSummary {
  total: number;
  active: number;
  inactive: number;
  countries: number;
  contracts_ending_30d: number;
}

/** Fields a form sends; `null` clears an optional field. */
export interface SupplierBody {
  company_name?: string;
  categories?: SupplierCategory[];
  website?: string | null;
  internal_notes?: string | null;
  contact_name?: string | null;
  contact_title?: string | null;
  contact_email?: string | null;
  contact_phone?: string | null;
  address?: string | null;
  region?: string | null;
  postal_code?: string | null;
  destination_id?: string | null;
  country_code?: string | null;
}

export type ContractStatus = "draft" | "published" | "expired";
/** Computed on the UTC day: published contracts are upcoming, in force or ended. */
export type ContractPhase = "draft" | "upcoming" | "in_force" | "ended" | "expired";

export interface SupplierContract {
  id: string;
  supplier_id: string;
  title: string;
  reference: string | null;
  valid_from: IsoDate;
  valid_to: IsoDate | null;
  currency: string;
  payment_terms: string | null;
  cancellation_policy: string | null;
  notes: string | null;
  status: ContractStatus;
  phase: ContractPhase;
  created_by: string;
  created_by_name: string | null;
  updated_by: string | null;
  updated_by_name: string | null;
  published_by: string | null;
  published_by_name: string | null;
  published_at: IsoDateTime | null;
  expired_by: string | null;
  expired_by_name: string | null;
  expired_at: IsoDateTime | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface ContractBody {
  title?: string;
  reference?: string | null;
  valid_from?: IsoDate;
  valid_to?: IsoDate | null;
  currency?: string;
  payment_terms?: string | null;
  cancellation_policy?: string | null;
  notes?: string | null;
}

export interface CatalogPlace {
  destination_id: string;
  name: string;
  country: string;
  country_code: string | null;
  /** AA-997: the picker never lists a merged place; an unconfirmed point stays pickable. */
  hidden_from_customers?: boolean;
  hidden_reason?: "location_unconfirmed" | "merged" | null;
  merged_into?: string | null;
}

export type Changed<T> = T & { changed: string[] };

export const suppliersApi = {
  list: (q: { q?: string; status?: string; category?: string; country?: string; sort?: string; limit?: number; offset?: number } = {}) =>
    bkGet<SupplierList>("suppliers", q),
  summary: () => bkGet<SupplierSummary>("suppliers/summary"),
  get: (ref: string) => bkGet<SupplierDetail>(`suppliers/${enc(ref)}`),
  create: (body: SupplierBody) => bkSend<SupplierDetail>("suppliers", { body }),
  update: (ref: string, body: SupplierBody) => bkSend<Changed<Supplier>>(`suppliers/${enc(ref)}`, { method: "PATCH", body }),
  deactivate: (ref: string, reason?: string) =>
    bkSend<Changed<Supplier>>(`suppliers/${enc(ref)}/deactivate`, { body: { reason: reason || undefined } }),
  activate: (ref: string) => bkSend<Changed<Supplier>>(`suppliers/${enc(ref)}/activate`, { body: {} }),
  contracts: (ref: string) => bkGet<{ items: SupplierContract[] }>(`suppliers/${enc(ref)}/contracts`),
  createContract: (ref: string, body: ContractBody) => bkSend<SupplierContract>(`suppliers/${enc(ref)}/contracts`, { body }),
  updateContract: (id: string, body: ContractBody | { status: "published" | "expired" }) =>
    bkSend<Changed<SupplierContract>>(`contracts/${enc(id)}`, { method: "PATCH", body }),
  places: (q: { q?: string; country?: string; limit?: number }) => bkGet<{ items: CatalogPlace[] }>("catalog/destinations", q),
};
