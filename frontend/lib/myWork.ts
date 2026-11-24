// lib/myWork.ts
// AA-1159 — the My work band of the staff dashboard: one tile per work queue, shown by capability (never by person).
// GET /booking/my-work/summary (backend app/my_work.py) answers the caller's tiles with the count, the oldest item or
// the earliest due date, and the target list's filter; this table keeps the page path, label and icon name of each key.
// The keys and capabilities must stay those of backend app/my_work.py TILES (tests/unit/test_aa1159_my_work.py reads
// this file). A tile opens `path` + the API's `filter` as the query string, the exact view its count was taken from.

export type MyWorkGroup = "work" | "approve";

export interface MyWorkTileSpec {
  key: string;
  capability: string;
  group: MyWorkGroup;
  /** The list page the tile opens (its query string comes from the API's `filter`). */
  path: string;
  label: string;
  /** Accessible name of the tile link. */
  aria: string;
  /** The hint when nothing waits. */
  empty: string;
}

export const MY_WORK_TILES: readonly MyWorkTileSpec[] = [
  {
    key: "my_open_cases",
    capability: "trip_case.read",
    group: "work",
    path: "/cases",
    label: "My open cases",
    aria: "My open cases: open my open cases",
    empty: "Nothing assigned to you",
  },
  {
    key: "trip_checks_overdue",
    capability: "ops.read",
    group: "work",
    path: "/trip-checks",
    label: "Trip checks overdue",
    aria: "Trip checks overdue: open the overdue checks",
    empty: "Nothing overdue",
  },
  {
    key: "b2b_on_request",
    capability: "b2b_desk.read",
    group: "work",
    path: "/b2b-desk",
    label: "On-request lines",
    aria: "On-request lines: open the lines waiting for an answer",
    empty: "Nothing waits for an answer",
  },
  {
    key: "supplier_bills_overdue",
    capability: "bill.read",
    group: "work",
    path: "/billing",
    label: "Supplier bills overdue",
    aria: "Supplier bills overdue: open the overdue bills",
    empty: "Nothing overdue",
  },
  {
    key: "supplier_bills_due_soon",
    capability: "bill.read",
    group: "work",
    path: "/billing",
    label: "Supplier bills due soon",
    aria: "Supplier bills due soon: open the bills falling due",
    empty: "Nothing due soon",
  },
  {
    key: "agency_invoices_overdue",
    capability: "agency_billing.read",
    group: "work",
    path: "/agency-billing",
    label: "Agency invoices overdue",
    aria: "Agency invoices overdue: open the overdue invoices",
    empty: "Nothing overdue",
  },
  {
    // AA-1177: published supplier contracts of every kind ending within Pricing > Policy contract_expiry_alert_days.
    key: "contracts_ending_soon",
    capability: "supply.write",
    group: "work",
    path: "/suppliers",
    label: "Contracts ending soon",
    aria: "Contracts ending soon: open the supplier contracts that end within the warning window",
    empty: "No contract ends soon",
  },
  {
    key: "supplier_bills_to_approve",
    capability: "bill.approve",
    group: "approve",
    path: "/billing",
    label: "Supplier bills to approve",
    aria: "Supplier bills to approve: open the bills waiting for approval",
    empty: "Nothing to approve",
  },
  {
    key: "supplier_bills_to_pay",
    capability: "payout.write",
    group: "approve",
    path: "/billing",
    label: "Supplier bills to pay",
    aria: "Supplier bills to pay: open the approved bills",
    empty: "Nothing to pay",
  },
  {
    key: "agency_payments_to_approve",
    capability: "agency_payment.approve",
    group: "approve",
    path: "/agency-billing",
    label: "Agency payments to approve",
    aria: "Agency payments to approve: open the recorded payments",
    empty: "Nothing to approve",
  },
  {
    key: "stop_sales_to_review",
    capability: "stop_sale.approve",
    group: "approve",
    path: "/stop-sales",
    label: "Stop-sales to review",
    aria: "Stop-sales to review: open the stop-sales waiting for a second person",
    empty: "Nothing to review",
  },
  {
    key: "suppliers_to_activate",
    capability: "supplier.activate",
    group: "approve",
    path: "/suppliers",
    label: "Suppliers to activate",
    aria: "Suppliers to activate: open the pending suppliers",
    empty: "Nothing to activate",
  },
  {
    key: "billing_changes_to_approve",
    capability: "billing_config.approve",
    group: "approve",
    path: "/agency-billing",
    label: "Billing changes to approve",
    aria: "Billing changes to approve: open the billing settings with a pending change",
    empty: "Nothing to approve",
  },
  {
    key: "supplier_bank_to_approve",
    capability: "supplier_bank.approve",
    group: "approve",
    path: "/suppliers",
    label: "Bank account changes to approve",
    aria: "Bank account changes to approve: open the suppliers with a pending bank account",
    empty: "Nothing to approve",
  },
  {
    key: "owner_self_approvals",
    capability: "audit.read",
    group: "work",
    path: "/audit-log",
    label: "Business-owner self-approvals",
    aria: "Business-owner self-approvals this month: open them in the audit log",
    empty: "None this month",
  },
];

export const MY_WORK_CAPABILITIES: readonly string[] = [...new Set(MY_WORK_TILES.map((t) => t.capability))];

/** Does a holder of `permissions` get a My work band at all (at least one tile capability)? */
export function hasMyWork(permissions: readonly string[]): boolean {
  return MY_WORK_CAPABILITIES.some((c) => permissions.includes(c));
}

export function tileSpec(key: string): MyWorkTileSpec | undefined {
  return MY_WORK_TILES.find((t) => t.key === key);
}

/** The tile's target: its page path and the API's filter as the query string (keys in the API's order). */
export function tileHref(path: string, filter: Record<string, string>): string {
  const qs = new URLSearchParams(Object.entries(filter).filter(([, v]) => v !== "")).toString();
  return qs ? `${path}?${qs}` : path;
}
