// lib/api/types.ts
// AA-683 — response shapes of the Booking API as built on feat/aa-681-staff-auth-trip-case-versions
// (bk-003 + bk-005; backend app/routers/*, app/trip_cases/*, app/customers/*). The backend returns plain
// dicts, so these types are read off the SQL column lists; keep them in step with those files.
// Dates and timestamps arrive as ISO strings; money as decimal strings (never floats).

import type { StaffRole } from "../rbac";

export type IsoDate = string; // "2026-10-14"
export type IsoDateTime = string; // "2026-10-14T09:15:00+00:00"
export type DecimalString = string; // "1234.50"

/** Offset pages: trip cases, versions, customers. */
export interface OffsetPage<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

/** Keyset pages: activity, notifications, audit log. Pass `next_before` as `before`. */
export interface CursorPage<T> {
  items: T[];
  next_before: string | null;
}

// ── Staff ────────────────────────────────────────────────────────────────────
export interface StaffProfile {
  cis_user_id: string;
  email: string | null;
  name: string | null;
  role: StaffRole;
  active: boolean;
  cis_role: string | null;
  created_at: IsoDateTime | null;
  last_seen_at: IsoDateTime | null;
}

export interface StaffMe extends StaffProfile {
  permissions: string[];
}

export interface StaffDirectoryEntry {
  cis_user_id: string;
  name: string | null;
  role: StaffRole;
}

export interface StaffAssignmentRow {
  role: string;
  assigned_at: IsoDateTime;
  trip_case_id: string;
  code: string;
  status: TripCaseStatus;
  start_date: IsoDate | null;
  customer_name: string | null;
}

export interface StaffDetail extends StaffProfile {
  updated_at: IsoDateTime | null;
  open_assignments: StaffAssignmentRow[];
  counts: { open_assignments: number; assignments: number };
}

export interface StaffPreferences {
  notify: Record<string, boolean>;
}

// ── Trip cases ───────────────────────────────────────────────────────────────
export const TRIP_CASE_STATUSES = [
  "new_inquiry",
  "discovery",
  "building",
  "proposal_sent",
  "confirmed",
  "supplier_confirming",
  "invoice_sent",
  "payment_pending",
  "paid",
  "pre_departure",
  "on_trip",
  "completed",
  "cancelled",
] as const;
export type TripCaseStatus = (typeof TRIP_CASE_STATUSES)[number];

export type SourceSystem = "tripplanner" | "manual" | "cis";

export interface CaseAssignee {
  cis_user_id: string;
  role: string;
  name: string | null;
  assigned_at: IsoDateTime;
}

export interface TripShape {
  trip_title: string | null; // null = custom trip
  leg_titles: string[];
  leg_count: number;
  duration_days: number | null;
  trip_end_date: IsoDate | null;
}

export interface TripCaseListItem extends TripShape {
  id: string;
  code: string;
  status: TripCaseStatus;
  customer_id: string;
  trip_style: string | null;
  start_date: IsoDate | null;
  end_date: IsoDate | null;
  pax: number | null;
  pax_adults: number | null;
  pax_children: number | null;
  currency: string | null;
  notes: string | null;
  source_system: SourceSystem;
  source_id: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  customer_code: string | null;
  customer_name: string | null;
  customer_email: string | null;
  customer_phone: string | null;
  customer_verified: boolean;
  customer_needs_merge_review: boolean;
  assignees: CaseAssignee[];
  countries: string[];
  latest_version: number | null;
}

export interface TripCaseSummary {
  open: number;
  new_inquiry: number;
  mine: number;
  proposals_awaiting: number;
  by_status: Record<TripCaseStatus, number>;
  mom_delta: { created_this_month: number; created_last_month: number; delta: number };
}

export interface TripCaseLeg {
  position: number;
  source_tour_id: string;
  day_from: number;
  day_to: number;
}

export type VersionBadge = "draft" | "sent" | "approved" | "superseded";

export interface Version {
  id: string;
  version_number: number;
  title: string | null;
  notes: string | null;
  total_price: DecimalString | null;
  currency: string | null;
  channel: string | null;
  valid_until: IsoDate | null;
  fx_locked: { rate: DecimalString; as_of: IsoDateTime | null } | null;
  is_sent_to_customer: boolean;
  sent_at: IsoDateTime | null;
  sent_by: string | null;
  is_approved: boolean;
  approved_at: IsoDateTime | null;
  approved_by: string | null;
  approval_note: string | null;
  created_by: string | null;
  created_at: IsoDateTime;
  updated_by: string | null;
  updated_at: IsoDateTime;
  badge: VersionBadge;
}

export interface CaseVersion extends Version {
  item_count: number;
}

export interface StatusHistoryRow {
  from_status: TripCaseStatus | null;
  to_status: TripCaseStatus;
  changed_by: string | null;
  note: string | null;
  changed_at: IsoDateTime;
}

export interface AssignmentRow {
  id: string;
  staff_user_id: string;
  staff_name: string | null;
  role: string;
  assigned_by: string | null;
  assigned_at: IsoDateTime;
  unassigned_at: IsoDateTime | null;
}

export interface TripCaseDetail extends Omit<TripCaseListItem, "latest_version"> {
  inquiry_snapshot: Record<string, unknown> | null;
  inbox_event_id: string | null;
  legs: TripCaseLeg[];
  versions: CaseVersion[];
  status_history: StatusHistoryRow[];
  assignments: AssignmentRow[];
  allowed_transitions: TripCaseStatus[];
}

export interface ActivityItem {
  id: string;
  kind: "status" | "audit";
  at: IsoDateTime;
  actor: { id: string | null; name: string | null };
  action: string;
  entity_label: string | null;
  summary: string | null;
  before: unknown;
  after: unknown;
}

/** One supplier net cost line of a version item (bk-004 version_item_costs, entry order). */
export interface VersionItemCost {
  id: string;
  net_amount: DecimalString;
  net_currency: string;
  supplier_note: string | null;
}

export interface VersionItem {
  id: string;
  position: number;
  title: string;
  day_from: number;
  day_to: number;
  source_tour_id: string | null;
  notes: string | null;
  /** Absent on an API without the pricing branch (bk-004 / AA-810). */
  costs?: VersionItemCost[];
}

export interface VersionWithItems extends Version {
  items: VersionItem[];
}

/** A row of the cross-case list GET /booking/versions. */
export interface VersionListItem extends Omit<Version, "updated_by"> {
  trip_case_id: string;
  trip_case_code: string;
  trip_case_status: TripCaseStatus;
  start_date: IsoDate | null;
  end_date: IsoDate | null;
  sales_id: string | null;
  customer: { id: string; name: string | null };
  /** In this list `updated_by` is an object (versions.list_versions), not the bare CIS user id. */
  updated_by: { id: string | null; name: string | null };
  leg_titles: string[];
  trip_title: string | null;
  duration_days: number | null;
}

// ── Customers ────────────────────────────────────────────────────────────────
export type CustomerStatus = "lead" | "active" | "past" | "blocked";
export type CustomerSource = "manual" | "otp" | "tripplanner_lead";

export interface CustomerSummary {
  id: string;
  code: string | null;
  name: string | null;
  email: string | null;
  phone: string | null;
  address: string | null;
  verified: boolean;
  is_blocked: boolean;
  needs_merge_review: boolean;
  merge_candidate_id: string | null;
  source: CustomerSource | null;
  contact_consent: boolean;
  contact_consent_at: IsoDateTime | null;
  case_count: number;
  open_case_count: number;
  last_activity_at: IsoDateTime | null;
  status: CustomerStatus;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface CustomerDetail extends CustomerSummary {
  merge_candidate: { id: string; code: string | null; name: string | null; verified: boolean; archived: boolean } | null;
  cases: TripCaseListItem[];
  inquiries: Pick<
    TripCaseListItem,
    "id" | "code" | "status" | "created_at" | "trip_title" | "leg_titles" | "duration_days"
  >[];
  recent_activity: {
    id: string;
    at: IsoDateTime;
    action: string;
    entity_type: string;
    entity_label: string | null;
    summary: string | null;
    actor: { id: string | null; name: string | null };
  }[];
}

/** POST /booking/customers body (backend NewCustomer, extra=forbid). */
export interface NewCustomerBody {
  name: string;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  contact_consent: boolean;
}

/** PATCH /booking/customers/{ref} body (backend CustomerPatch, extra=forbid). */
export interface CustomerPatchBody {
  name?: string;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  is_blocked?: boolean;
}

/** PATCH answer: the customer row (not the summary) plus the fields that changed. */
export interface CustomerPatchResult {
  id: string;
  code: string | null;
  name: string | null;
  email: string | null;
  phone: string | null;
  address: string | null;
  verified: boolean;
  is_blocked: boolean;
  needs_merge_review: boolean;
  source: CustomerSource | null;
  changed: string[];
}

// ── Notifications ────────────────────────────────────────────────────────────
export interface StaffNotification {
  id: string;
  type: string;
  title: string | null;
  message: string | null;
  entity_type: string | null;
  entity_id: string | null;
  data: Record<string, unknown>;
  created_at: IsoDateTime;
  read: boolean;
}

export interface NotificationFeed extends CursorPage<StaffNotification> {
  unread_count: number;
}

// ── Reference + audit ────────────────────────────────────────────────────────
export interface SettingsResponse {
  values: Record<string, unknown>;
  items: { key: string; value: unknown; description: string | null; updated_at: IsoDateTime }[];
}

export interface Country {
  iso2: string;
  name: string;
}

export interface AuditLogItem {
  id: string;
  at: IsoDateTime;
  actor: { type: string; id: string | null; name: string | null };
  action: string;
  entity_type: string;
  entity_id: string | null;
  entity_label: string | null;
  summary: string | null;
  before: unknown;
  after: unknown;
}

export interface HealthResponse {
  status: string;
  service: string;
  git_sha: string;
}
