// lib/api/staffRoles.ts
// AA-1155 T6 / T7 — the Booking role editor (backend app/routers/staff_roles.py, app/auth/role_editor.py; bk-062).
// Built-in roles stay in code and are listed read-only; custom roles are data: an owner (the CIS Staff page marker)
// clones a built-in role, ticks capabilities, relabels or archives one. Every change is audited before and after.
//
//   GET   /booking/staff-roles                  staff.admin or owner: built-in + custom roles, every capability, can_edit
//   POST  /booking/staff-roles                  owner: {code, label, based_on, capabilities?}  (201)
//   PATCH /booking/staff-roles/{code}           owner: {label?, capabilities?, archived?}
// Errors: 403 owner_only (not an owner), 400 approve_class_capability / unknown_capability / invalid_role_code /
// invalid_label / unknown_base_role, 409 role_exists, 404 role_not_found.

import { bkGet, bkSend } from "./client";
import type { IsoDateTime } from "./types";

const enc = encodeURIComponent;

export interface BuiltInRole {
  code: string;
  capabilities: string[];
}

export interface CustomRole {
  code: string;
  label: string;
  based_on: string;
  archived: boolean;
  capabilities: string[];
  /** Active staff profiles holding the role now. */
  holders: number;
  created_at: IsoDateTime;
  created_by: string;
  updated_at: IsoDateTime;
  updated_by: string;
}

export interface Capability {
  name: string;
  /** False for the approve class: built-in roles only, never a custom role. */
  assignable: boolean;
}

export interface StaffRoles {
  builtin: BuiltInRole[];
  custom: CustomRole[];
  capabilities: Capability[];
  /** The caller is an owner: may create and change custom roles. */
  can_edit: boolean;
  /** AA-1157: the caller is an owner (not inside a view): may start a read-only view as one of these roles. */
  can_view_as?: boolean;
}

export interface NewRole {
  code: string;
  label: string;
  based_on: string;
  capabilities?: string[];
}

export interface RolePatch {
  label?: string;
  capabilities?: string[];
  archived?: boolean;
}

export const staffRolesApi = {
  list: () => bkGet<StaffRoles>("staff-roles"),
  create: (body: NewRole) => bkSend<CustomRole>("staff-roles", { method: "POST", body }),
  update: (code: string, body: RolePatch) => bkSend<CustomRole>(`staff-roles/${enc(code)}`, { method: "PATCH", body }),
};
