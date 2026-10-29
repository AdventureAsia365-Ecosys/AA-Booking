"use client";
// ui/local/StaffContext.tsx (AA-994 S4b: moved from ui/, the Booking shell; not part of the kit core)
// AA-683 — the signed-in staff profile (GET /booking/staff/me), loaded server-side by the (staff) layout
// and handed to client components. Replaces CIS useAdminIdentity (which read a client-writable cookie):
// here the role comes from the Booking API, behind a verified JWT.

import { createContext, useContext } from "react";
import type { StaffMe } from "@/lib/api/types";
import { ROLE_LABEL, isStaffRole } from "@/lib/rbac";

const StaffContext = createContext<StaffMe | null>(null);

export function StaffProvider({ staff, children }: { staff: StaffMe; children: React.ReactNode }) {
  return <StaffContext.Provider value={staff}>{children}</StaffContext.Provider>;
}

export function useStaff(): StaffMe {
  const staff = useContext(StaffContext);
  if (!staff) throw new Error("useStaff must be used inside the (staff) layout");
  return staff;
}

/** Display helpers: a name that is never empty, initials and the role label. */
export function staffDisplay(staff: Pick<StaffMe, "name" | "email" | "role">) {
  const name = staff.name?.trim() || staff.email?.split("@")[0] || "Staff";
  const initials =
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((p) => p[0]!.toUpperCase())
      .join("") || "S";
  const roleLabel = isStaffRole(staff.role) ? ROLE_LABEL[staff.role] : staff.role;
  return { name, initials, roleLabel };
}

/** True when the staff profile carries a backend permission (app/auth/staff.py PERMISSIONS). */
export function useCan(permission: string): boolean {
  return useStaff().permissions.includes(permission);
}
