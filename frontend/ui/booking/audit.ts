// ui/booking/audit.ts
// AA-683 — how an audit row (booking.audit_logs, GET /booking/audit-logs) reads in the UI: action badge tone
// (UI spec §1.3: CREATE → success · UPDATE → info · STATUS → warning · DELETE/BLOCK → danger), the
// upper-case action label, entity type names, and the page a row's entity opens.

import type { AuditLogItem } from "@/lib/api/types";
import type { BookingTone } from "./StatusBadge";

/** The `entity_type` values the backend writes today (app/audit.py callers + inquiry intake). */
export const AUDIT_ENTITY_LABEL: Record<string, string> = {
  trip_case: "Trip case",
  trip_case_version: "Proposal version",
  customer: "Customer",
  staff_user: "Staff",
  supplier: "Supplier",
  supplier_contract: "Supplier contract",
  supplier_rate: "Supplier rate",
  hotel: "Hotel",
  hotel_room_type: "Room type",
  service: "Service",
  pricing_rule: "Pricing rule",
  fx_rate: "FX rate",
  trip_plan: "Trip plan",
  departure: "Departure",
  b2b_hold: "B2B hold",
  b2b_partner: "B2B partner",
  retention: "Retention",
};

export function auditEntityLabel(entityType: string): string {
  return AUDIT_ENTITY_LABEL[entityType] ?? entityType.replace(/_/g, " ");
}

type VerbRef = Pick<AuditLogItem, "action"> & Partial<Pick<AuditLogItem, "before" | "after">>;

/** The verb of a row: the last part of the action, except that a `staff.updated` row that switched `active` reads as
 *  what it did (AA-996: the Deactivate dialog writes `staff.updated` with `active` false and a reason). */
export function auditVerb(row: VerbRef): string {
  const verb = row.action.split(".").pop() ?? row.action;
  if (row.action === "staff.updated" && isRecord(row.before) && isRecord(row.after) && row.before.active !== row.after.active) {
    if (row.after.active === false) return "deactivated";
    if (row.after.active === true) return "activated";
  }
  return verb;
}

// UI spec §1.3 action classes (AA-996 Figma audit: the colour must tell the class). Anchored on the verb, so
// `unblocked` / `unassigned` stay updates (info).
const DANGER_RE = /^(archived|blocked|deleted|cancel|case_cancelled|voided|declined|deactivat|removed|personal_data_cleared|expired)/;
const STATUS_RE = /^(status_changed|sent|approved|verified|confirmed|published|accepted|activated|superseded|amended|reminded|extended|requested|rerequested|incident_flagged|held)/;
const CREATE_RE = /^(created|provisioned|opened|recorded|added|uploaded|placed|generated)/;

/** Spec §1.3 tone of a row: CREATE → success · UPDATE → info · STATUS → warning · DELETE / BLOCK / VOID → danger.
 *  Accepts a bare action string too (`trip_case.status_changed`). */
export function auditActionTone(row: VerbRef | string): BookingTone {
  const verb = auditVerb(typeof row === "string" ? { action: row } : row);
  if (DANGER_RE.test(verb)) return "danger";
  if (STATUS_RE.test(verb)) return "warning";
  if (CREATE_RE.test(verb)) return "success";
  return "info";
}

/** `trip_case_version.items_replaced` → `items replaced` (rendered upper-case by the Pill). */
export function auditActionLabel(row: VerbRef | string): string {
  return auditVerb(typeof row === "string" ? { action: row } : row).replace(/_/g, " ");
}

type AuditRef = Pick<AuditLogItem, "action" | "entity_type" | "entity_id" | "entity_label" | "after">;

/** The record's display label: the stored `entity_label`, else the `code` in `after` (inquiry intake rows,
 *  written by the system without a label, carry `after.code = TC-…`). */
export function auditRecordLabel(row: Pick<AuditLogItem, "entity_label" | "after">): string | null {
  if (row.entity_label) return row.entity_label;
  const code = isRecord(row.after) ? row.after.code : undefined;
  return typeof code === "string" && code ? code : null;
}

/** The page of the row's entity, when the label or id is enough to build it; otherwise null. A deleted
 *  version has no page left to open. */
export function auditEntityHref(row: AuditRef): string | null {
  const label = auditRecordLabel(row) ?? "";
  if (row.entity_type === "trip_case") {
    if (/^TC-\d+$/.test(label)) return `/cases/${encodeURIComponent(label)}`;
    // The case page also resolves a case by its UUID (GET /booking/trip-cases/{uuid}).
    return row.entity_id ? `/cases/${encodeURIComponent(row.entity_id)}` : null;
  }
  if (row.entity_type === "trip_case_version") {
    if (/\.deleted$/.test(row.action)) return null;
    const m = /^(TC-\d+) V(\d+)$/.exec(label);
    if (m) return `/cases/${encodeURIComponent(m[1])}/proposals/${m[2]}`;
  }
  if (row.entity_type === "customer" && row.entity_id) return `/customers/${encodeURIComponent(row.entity_id)}`;
  if (row.entity_type === "staff_user" && row.entity_id) return `/staff/${encodeURIComponent(row.entity_id)}`;
  // The supplier and service pages resolve a record by its UUID as well as its code.
  if (row.entity_type === "supplier" && row.entity_id) return `/suppliers/${encodeURIComponent(row.entity_id)}`;
  if (row.entity_type === "service" && row.entity_id) return `/services/${encodeURIComponent(row.entity_id)}`;
  return null;
}

export interface AuditChange {
  field: string;
  before: string;
  after: string;
}

function show(value: unknown): string {
  if (value === undefined) return "";
  if (value === null) return "null";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Field-by-field view of `before` / `after` (snapshots written at write time; nothing is re-derived). */
export function auditChanges(before: unknown, after: unknown): AuditChange[] {
  if (!isRecord(before) && !isRecord(after)) {
    if (before == null && after == null) return [];
    return [{ field: "value", before: show(before ?? undefined), after: show(after ?? undefined) }];
  }
  const b = isRecord(before) ? before : {};
  const a = isRecord(after) ? after : {};
  const fields = [...new Set([...Object.keys(b), ...Object.keys(a)])];
  return fields.map((field) => ({ field, before: show(b[field]), after: show(a[field]) }));
}
