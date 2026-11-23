// lib/rbac.ts
// AA-683 — Booking staff roles and which pages each role may open (UI spec §2.2 and §3).
//
// The roles are `booking.staff_users.role` (bk-003). The Booking API enforces its own permission
// matrix on every call (backend app/auth/staff.py); this table only decides what the shell shows,
// so a page a role cannot use is never offered and, if typed in, shows a "no access" state instead
// of a wall of 403 errors. Same pattern as CIS lib/rbac.ts (PAGE_RULES + canSeePage).

// AA-1035 R2 (root ADR 0019 d3 / d9): finance issues invoices and records payments; the role comes only from the
// WORKFORCE group booking-finance (backend app/auth/staff.py, bk-038).
export const STAFF_ROLES = ["admin", "sales", "contracting", "content_creator", "finance"] as const;
export type StaffRole = (typeof STAFF_ROLES)[number];

export const ROLE_LABEL: Record<StaffRole, string> = {
  admin: "Admin",
  sales: "Sales",
  // AA-1150 (lean staff roles): the built-in contracting role is shown as "Product & operations".
  contracting: "Product & operations",
  content_creator: "Content creator",
  finance: "Finance",
};

// AA-1128 follow-up (S285): the CIS role a staff row carries as information (backend app/auth/staff.py CIS_GROUP_ROLES,
// the bridge token's cis_role), named as the CIS admin names it (AA-CIS-App lib/rbac.messages.ts). `reviewer` is the
// old alias of `content`. Show it with `tx(cisRoleLabel(role))`: the Vietnamese is in ui/booking/roles.messages.ts.
export const CIS_ROLE_LABEL: Record<string, string> = {
  admin: "Administrator",
  content_lead: "Content Lead",
  ai_ops: "AI Operations",
  customer_success: "Customer Success",
  finance: "Finance",
  content: "Content Team",
  reviewer: "Content Team",
  tenant_qa: "Tenant Content QA",
  support: "Support",
  viewer: "Viewer (read-only)",
};

/** The English name of a CIS role; an unknown role shows as it is (a new CIS role never breaks the page). */
export function cisRoleLabel(role: string): string {
  return Object.prototype.hasOwnProperty.call(CIS_ROLE_LABEL, role) ? CIS_ROLE_LABEL[role] : role;
}

export function isStaffRole(value: unknown): value is StaffRole {
  return typeof value === "string" && (STAFF_ROLES as readonly string[]).includes(value);
}

/** AA-1155: the English name of a Booking role: a built-in role's label, else the custom role's label from the API
 * (`role_label`), else the code as it is (a custom role never breaks a page). Show it with `tx()`. */
export function roleName(role: string, customLabel?: string | null): string {
  return isStaffRole(role) ? ROLE_LABEL[role] : customLabel?.trim() || role;
}

// AA-1159 (S286 F4): GET /booking/staff/directory needs this permission (backend app/auth/staff.py: admin, sales,
// contracting). A role without it (finance) never calls the directory: the staff picker shows disabled instead of a
// 403 and a console error.
export const STAFF_DIRECTORY_PERMISSION = "staff.directory";

/** May the signed-in staff member list the staff directory (the assignee pickers)? */
export function canPickStaff(permissions: readonly string[] | null | undefined): boolean {
  return Array.isArray(permissions) && permissions.includes(STAFF_DIRECTORY_PERMISSION);
}

// AA-1155 T7 (root spec R4.5): the pages follow the capabilities in GET /booking/staff/me `permissions`, never the role
// name, so a custom role (booking-custom, built from capabilities on the role editor) opens exactly the pages its
// capabilities serve, and a built-in role opens the same pages as before (lib/rbacPermissions.test.mjs proves it per
// role against the backend matrix, lib/rbac.permissions.fixture.json, which a backend unit test keeps equal to
// app/auth/staff.py PERMISSIONS).
/** Any signed-in staff member (no capability needed). */
const ANY: readonly string[] = [];

export interface PageRule {
  /** Path pattern; `:x` matches one segment, a trailing `/*` matches the rest. */
  pattern: string;
  /** The page opens for a holder of ANY of these capabilities (empty: every signed-in staff member). */
  perms: readonly string[];
}

// Most specific first: the first match wins. Read-only access ("r" in spec §3) is a page concern: a page opens with the
// read capability and hides the write actions the profile lacks (useCan).
export const PAGE_RULES: readonly PageRule[] = [
  { pattern: "/", perms: ANY },
  { pattern: "/notifications", perms: ANY },
  { pattern: "/settings", perms: ANY },
  // Sales (W1)
  { pattern: "/cases/new", perms: ["trip_case.write"] },
  // AA-1150 (L7a): finance reads trip cases (API trip_case.read) and prices (pricing.read); it writes neither.
  { pattern: "/cases", perms: ["trip_case.read"] },
  { pattern: "/cases/*", perms: ["trip_case.read"] },
  { pattern: "/proposals", perms: ["trip_case.read"] },
  { pattern: "/pricing/calculator", perms: ["pricing.read"] },
  // Admin (W1 / W2)
  { pattern: "/staff", perms: ["staff.admin"] },
  { pattern: "/staff/:id", perms: ANY }, // admin, or a staff member reading their own profile (API-enforced)
  { pattern: "/audit-log", perms: ["audit.read"] },
  // AA-1155 T7: the role editor (API GET /booking/staff-roles: staff.admin or the CIS owner marker; writes owner only).
  { pattern: "/roles", perms: ["staff.admin"] },
  // Supply (W2)
  { pattern: "/suppliers/new", perms: ["supply.write"] },
  { pattern: "/suppliers/rate-sheets/*", perms: ["supply.write"] },
  { pattern: "/suppliers/:id/edit", perms: ["supply.write"] },
  // AA-1150 (L7a): finance reads supply (API supply.read); the edit pages above need supply.write.
  { pattern: "/suppliers", perms: ["supply.read"] },
  { pattern: "/suppliers/*", perms: ["supply.read"] },
  { pattern: "/hotels", perms: ["supply.read"] },
  { pattern: "/services", perms: ["supply.read"] },
  { pattern: "/services/*", perms: ["supply.read"] },
  { pattern: "/pricing/rules", perms: ["pricing.write"] },
  // AA-1030: agency tiers and rate cards (API pricing.read; writes pricing.write, approvals pricing.approve).
  { pattern: "/pricing/agency-tiers", perms: ["pricing.read"] },
  // AA-1030 G3: one agency's tariff (pricing.read).
  { pattern: "/pricing/agency-tiers/tariff", perms: ["pricing.read"] },
  // AA-1029: price readiness of the catalog (API pricing.read).
  { pattern: "/pricing/readiness", perms: ["pricing.read"] },
  // AA-1091: the price setup workbench (API pricing.read; actions and the workbook import trip_plan.write).
  { pattern: "/pricing/workbench", perms: ["pricing.read"] },
  // AA-1091: the pricing policy parameters (API pricing_policy.read; changes pricing_policy.write, approvals by group).
  { pattern: "/pricing/policy", perms: ["pricing_policy.read"] },
  // AA-1091 (bk-055): season calendars and holiday events (API pricing.read; changes season.write, approvals
  // season.approve).
  { pattern: "/pricing/seasons", perms: ["pricing.read"] },
  { pattern: "/pricing/holiday-events", perms: ["pricing.read"] },
  // AA-1159: stop-sales on supplier contracts waiting for a second person (API supply.read; decisions stop_sale.approve).
  { pattern: "/stop-sales", perms: ["supply.read"] },
  // AA-1151 (bk-068): add-on offers (API addon.read; drafts addon.write, publishing addon.approve).
  { pattern: "/add-ons", perms: ["addon.read"] },
  // Catalog from CIS (W2): everyone reads (catalog.read); the commercial fields need trip_plan.write.
  { pattern: "/catalog/trip-plans/:id/commercial", perms: ["trip_plan.write"] },
  { pattern: "/catalog/*", perms: ["catalog.read"] },
  // Operations (W3)
  // AA-682 S4: /billing is the supplier bills (API bill.read).
  // AA-1046: the customer invoices and payments (and their detail pages) are retired; finance works agency billing.
  { pattern: "/billing", perms: ["bill.read"] },
  // AA-1037 R4 (root ADR 0019 d9): agency billing, every invoice from AA to an agency (software fee + tour
  // settlement), payments with maker-checker and the billing settings (API agency_billing.*, billing_config.write).
  { pattern: "/agency-billing", perms: ["agency_billing.read"] },
  { pattern: "/agency-billing/*", perms: ["agency_billing.read"] },
  // Trip checks (AA-682 S2): every operational role (API ops.read / ops.write).
  { pattern: "/trip-checks", perms: ["ops.read"] },
  // B2B desk (AA-975): on-request lines and departure incidents (API b2b_desk.*).
  { pattern: "/b2b-desk", perms: ["b2b_desk.read"] },
  // AA-1075 (bk-13): the operations manifest of a departure (API ops.read).
  { pattern: "/b2b-desk/manifest/:id", perms: ["ops.read"] },
  // AA-1073 rest (bk-048): the departure board across trip plans (API b2b_desk.read).
  { pattern: "/departures", perms: ["b2b_desk.read"] },
  // AA-1074 (bk-045): costs & profit. Departure costs and purchase orders (API bill.read / ops.read + pricing.read),
  // profit (API profit.read); the page shows the tabs the profile may use.
  { pattern: "/finance", perms: ["bill.read", "ops.read", "profit.read"] },
];

function matches(pattern: string, pathname: string): boolean {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (pattern === "/") return path === "/";
  const pat = pattern.split("/").filter(Boolean);
  const seg = path.split("/").filter(Boolean);
  for (let i = 0; i < pat.length; i++) {
    const p = pat[i];
    if (p === "*" && i === pat.length - 1) return seg.length > i;
    if (i >= seg.length) return false;
    if (p.startsWith(":")) continue;
    if (p !== seg[i]) return false;
  }
  return seg.length === pat.length;
}

export function pageRule(pathname: string): PageRule | null {
  return PAGE_RULES.find((r) => matches(r.pattern, pathname)) ?? null;
}

/** May a staff member with these capabilities (GET /booking/staff/me `permissions`) open `pathname`? Unknown paths are
 * denied (allow-list, as in CIS AA-252). */
export function canSeePage(permissions: readonly string[] | null | undefined, pathname: string): boolean {
  if (!Array.isArray(permissions)) return false;
  const rule = pageRule(pathname);
  return !!rule && (rule.perms.length === 0 || rule.perms.some((p) => permissions.includes(p)));
}
