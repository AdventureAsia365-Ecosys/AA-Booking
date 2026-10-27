// lib/rbac.ts
// AA-683 — Booking staff roles and which pages each role may open (UI spec §2.2 and §3).
//
// The roles are `booking.staff_users.role` (bk-003). The Booking API enforces its own permission
// matrix on every call (backend app/auth/staff.py); this table only decides what the shell shows,
// so a page a role cannot use is never offered and, if typed in, shows a "no access" state instead
// of a wall of 403 errors. Same pattern as CIS lib/rbac.ts (PAGE_RULES + canSeePage).

export const STAFF_ROLES = ["admin", "sales", "contracting", "content_creator"] as const;
export type StaffRole = (typeof STAFF_ROLES)[number];

export const ROLE_LABEL: Record<StaffRole, string> = {
  admin: "Admin",
  sales: "Sales",
  contracting: "Contracting",
  content_creator: "Content creator",
};

export function isStaffRole(value: unknown): value is StaffRole {
  return typeof value === "string" && (STAFF_ROLES as readonly string[]).includes(value);
}

const ALL: readonly StaffRole[] = STAFF_ROLES;
const A: StaffRole = "admin";
const S: StaffRole = "sales";
const C: StaffRole = "contracting";

export interface PageRule {
  /** Path pattern; `:x` matches one segment, a trailing `/*` matches the rest. */
  pattern: string;
  roles: readonly StaffRole[];
}

// Most specific first: the first match wins. Read-only access ("r" in spec §3) is a page concern,
// so a read-only role is listed here and the page hides the write actions.
export const PAGE_RULES: readonly PageRule[] = [
  { pattern: "/", roles: ALL },
  { pattern: "/notifications", roles: ALL },
  { pattern: "/settings", roles: ALL },
  // Sales (W1)
  { pattern: "/cases/new", roles: [A, S] },
  { pattern: "/cases", roles: [A, S, C] },
  { pattern: "/cases/*", roles: [A, S, C] },
  { pattern: "/proposals", roles: [A, S, C] },
  { pattern: "/customers", roles: [A, S] },
  { pattern: "/customers/*", roles: [A, S] },
  { pattern: "/pricing/calculator", roles: [A, S, C] },
  // Admin (W1 / W2)
  { pattern: "/staff", roles: [A] },
  { pattern: "/staff/:id", roles: ALL }, // admin, or a staff member reading their own profile (API-enforced)
  { pattern: "/audit-log", roles: [A] },
  // Supply (W2)
  { pattern: "/suppliers/new", roles: [A, C] },
  { pattern: "/suppliers/rate-sheets/*", roles: [A, C] },
  { pattern: "/suppliers/:id/edit", roles: [A, C] },
  { pattern: "/suppliers", roles: [A, C, S] },
  { pattern: "/suppliers/*", roles: [A, C, S] },
  { pattern: "/hotels", roles: [A, C, S] },
  { pattern: "/services", roles: [A, C, S] },
  { pattern: "/services/*", roles: [A, C, S] },
  { pattern: "/pricing/rules", roles: [A, C] },
  // Catalog from CIS (W2): everyone reads; admin / contracting edit commercial fields.
  { pattern: "/catalog/trip-plans/:id/commercial", roles: [A, C] },
  { pattern: "/catalog/*", roles: ALL },
  // Operations (W3)
  // AA-682 S4: contracting opens /billing for its Supplier bills tab only (API bill.*; invoices stay billing.read).
  { pattern: "/billing", roles: [A, S, C] },
  { pattern: "/billing/*", roles: [A, S] },
  // Trip checks (AA-682 S2): every operational role (API ops.read / ops.write).
  { pattern: "/trip-checks", roles: [A, S, C] },
  // B2B desk (AA-975): on-request lines and departure incidents; every operational role (API b2b_desk.*).
  { pattern: "/b2b-desk", roles: [A, S, C] },
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

/** May `role` open `pathname`? Unknown paths are denied (allow-list, as in CIS AA-252). */
export function canSeePage(role: string | null | undefined, pathname: string): boolean {
  if (!isStaffRole(role)) return false;
  const rule = pageRule(pathname);
  return !!rule && rule.roles.includes(role);
}
