// ui/booking/agencyFilter.ts
// AA-1037 (root ADR 0019 d9) — the agency filter of Agency billing → Invoices. CIS Tenant 360 opens
// `/agency-billing?tab=invoices&tenant=<CIS tenant uuid>` ("Open in Booking"); the page reads `tenant`, passes it to
// GET /booking/agency-invoices as `tenant_id` (the API filters on `party_tenant_id`) and shows a removable chip with
// the agency's name. Kept free of React and of path aliases, so Node's test runner loads it as it is.

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * The tenant id of the `tenant` URL key, lower-cased, or null when it is missing or not a UUID. A malformed value is
 * dropped rather than sent: the API types `tenant_id` as a UUID and would answer 422, which would hide every invoice
 * behind an error instead of showing the unfiltered list.
 */
export function tenantFilter(raw: string | null | undefined): string | null {
  const s = (raw ?? "").trim().toLowerCase();
  return UUID_RE.test(s) ? s : null;
}

/**
 * The name the filter chip shows: the agency's name from the billing status rows (every partner has one), else from
 * a listed invoice of that agency, else a neutral label (never the raw id: staff do not read UUIDs).
 */
export function agencyFilterLabel(
  tenantId: string,
  statusRows: readonly { tenant_id: string; agency_name: string }[] | undefined,
  invoices: readonly { party_tenant_id: string; agency_name: string }[] | undefined,
): string {
  const id = tenantId.toLowerCase();
  const fromStatus = statusRows?.find((r) => r.tenant_id.toLowerCase() === id)?.agency_name;
  if (fromStatus) return fromStatus;
  const fromInvoice = invoices?.find((r) => r.party_tenant_id.toLowerCase() === id)?.agency_name;
  return fromInvoice || "One agency";
}
