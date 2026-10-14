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
};

export function auditEntityLabel(entityType: string): string {
  return AUDIT_ENTITY_LABEL[entityType] ?? entityType.replace(/_/g, " ");
}

/** Spec §1.3 tone of an action such as `trip_case.status_changed` or `customer.blocked`. */
export function auditActionTone(action: string): BookingTone {
  const verb = action.split(".").pop() ?? action;
  // Anchored on the verb, so `unblocked` / `unassigned` stay updates (info).
  if (/^(archived|blocked|deleted|cancel|deactivat)/.test(verb)) return "danger";
  if (/^(status_changed|sent|approved)/.test(verb)) return "warning";
  if (/^(created|provisioned|opened)/.test(verb)) return "success";
  return "info";
}

/** `trip_case_version.items_replaced` → `items replaced` (rendered upper-case by the Pill). */
export function auditActionLabel(action: string): string {
  return (action.split(".").pop() ?? action).replace(/_/g, " ");
}

/** The page of the row's entity, when the label or id is enough to build it; otherwise null. */
export function auditEntityHref(row: Pick<AuditLogItem, "entity_type" | "entity_id" | "entity_label">): string | null {
  const label = row.entity_label ?? "";
  if (row.entity_type === "trip_case" && /^TC-\d+$/.test(label)) return `/cases/${encodeURIComponent(label)}`;
  if (row.entity_type === "trip_case_version") {
    const m = /^(TC-\d+) V(\d+)$/.exec(label);
    if (m) return `/cases/${encodeURIComponent(m[1])}/proposals/${m[2]}`;
  }
  if (row.entity_type === "customer" && row.entity_id) return `/customers/${encodeURIComponent(row.entity_id)}`;
  if (row.entity_type === "staff_user" && row.entity_id) return `/staff/${encodeURIComponent(row.entity_id)}`;
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
