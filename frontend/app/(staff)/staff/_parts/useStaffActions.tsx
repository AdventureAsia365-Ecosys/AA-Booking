"use client";
// app/(staff)/staff/_parts/useStaffActions.tsx
// AA-683 (fe-people) — admin actions on a Booking staff profile (UI spec §4.13): Change role (drawer) and
// Activate / Deactivate (confirm). Both are PATCH /booking/staff/{cis_user_id} {role?, active?}, admin
// only, audited server-side ("staff.updated"). Deactivate replaces Figma's Block / Suspend; there is no
// reset password or invite here, because accounts and passwords live in CIS (ADR 0004 O1).
// An admin cannot deactivate or demote themself (API 409 `cannot_demote_self`); the UI disables both.
// AA-680: a profile linked to a company sign-in (`managed_in_directory`) takes its role from the booking-* group set
// in CIS → Platform → Staff; the API refuses a role change (409 `managed_in_directory`), so the UI offers the CIS link
// instead. Activate / Deactivate stays a Booking switch for every profile.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api/client";
import type { StaffProfile } from "@/lib/api/types";
import { ROLE_LABEL, STAFF_ROLES, type StaffRole } from "@/lib/rbac";
import { ConfirmDialog, FormDrawer, useStaff, useToast } from "@/ui";

/** What each Booking role can do, for the role picker (backend PERMISSIONS in app/auth/staff.py). */
export const ROLE_HELP: Record<StaffRole, string> = {
  admin: "Everything, including staff, customers' block / archive and the audit log.",
  sales: "Trip cases, proposals and customers.",
  contracting: "Reads trip cases and customers; suppliers, rates and pricing from wave 2.",
  content_creator: "Dashboard and the CIS catalog, read-only; content itself is written in CIS.",
};

export function staffErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "cannot_demote_self") return "You cannot deactivate or demote yourself. Ask another admin.";
    if (err.code === "staff_not_found") return "This staff profile no longer exists.";
    if (err.code === "managed_in_directory") return "This person's Booking role is set in CIS → Platform → Staff.";
  }
  return err instanceof Error ? err.message : "Could not update the staff profile.";
}

type Pending = { kind: "activate" | "deactivate"; staff: StaffProfile } | null;

export function useStaffActions() {
  const me = useStaff();
  const qc = useQueryClient();
  const toast = useToast();
  const [pending, setPending] = useState<Pending>(null);
  const [roleFor, setRoleFor] = useState<StaffProfile | null>(null);

  const patch = useMutation({
    mutationFn: ({ staff, body }: { staff: StaffProfile; body: { active?: boolean; role?: string } }) =>
      api.staff.update(staff.cis_user_id, body),
    onSuccess: (updated, { staff, body }) => {
      qc.invalidateQueries({ queryKey: ["staff"] });
      const name = updated.name ?? staff.name ?? staff.email ?? "Staff member";
      if (body.role !== undefined) toast.success(`${name} is now ${ROLE_LABEL[updated.role] ?? updated.role}.`);
      else toast.success(updated.active ? `${name} can now use Booking.` : `${name} can no longer use Booking.`);
      setPending(null);
      setRoleFor(null);
    },
  });

  const isSelf = (s: StaffProfile) => s.cis_user_id === me.cis_user_id;
  const roleManagedInCis = (s: StaffProfile) => s.managed_in_directory === true;
  const name = pending ? pending.staff.name ?? pending.staff.email ?? pending.staff.cis_user_id : "";

  const dialogs = (
    <>
      <ConfirmDialog
        open={pending !== null}
        onClose={() => setPending(null)}
        onConfirm={() => pending && patch.mutate({ staff: pending.staff, body: { active: pending.kind === "activate" } })}
        busy={patch.isPending}
        error={patch.error ? staffErrorText(patch.error) : null}
        danger={pending?.kind === "deactivate"}
        title={pending?.kind === "activate" ? "Activate staff member" : "Deactivate staff member"}
        confirmLabel={pending?.kind === "activate" ? "Activate" : "Deactivate"}
        message={
          pending?.kind === "activate" ? (
            <>
              Activate <strong className="text-ink">{name}</strong> as{" "}
              <strong className="text-ink">{ROLE_LABEL[pending.staff.role] ?? pending.staff.role}</strong>? They can use Booking with
              that role from their next page load. Change the role first if it is not right.
            </>
          ) : (
            <>
              Deactivate <strong className="text-ink">{name}</strong>? They lose access to Booking at once. Their CIS account is not
              changed, and their open trip cases stay assigned to them until someone reassigns them.
            </>
          )
        }
      />
      {roleFor && (
        <RoleDrawer
          staff={roleFor}
          busy={patch.isPending}
          error={patch.error ? staffErrorText(patch.error) : null}
          onClose={() => {
            setRoleFor(null);
            patch.reset();
          }}
          onSave={(role) => patch.mutate({ staff: roleFor, body: { role } })}
        />
      )}
    </>
  );

  return {
    isSelf,
    roleManagedInCis,
    changeRole: (s: StaffProfile) => {
      patch.reset();
      setRoleFor(s);
    },
    activate: (s: StaffProfile) => {
      patch.reset();
      setPending({ kind: "activate", staff: s });
    },
    deactivate: (s: StaffProfile) => {
      patch.reset();
      setPending({ kind: "deactivate", staff: s });
    },
    dialogs,
  };
}

function RoleDrawer({
  staff,
  busy,
  error,
  onClose,
  onSave,
}: {
  staff: StaffProfile;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (role: StaffRole) => void;
}) {
  const me = useStaff();
  const [role, setRole] = useState<StaffRole>(staff.role);
  const self = staff.cis_user_id === me.cis_user_id;
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="Change Booking role"
      submitLabel="Save role"
      onSubmit={() => (role === staff.role ? onClose() : onSave(role))}
      busy={busy}
      dirty={role !== staff.role}
      error={error}
    >
      <p className="text-[13px] text-muted">
        Role of <span className="font-medium text-ink">{staff.name ?? staff.email ?? staff.cis_user_id}</span> in Booking. Their CIS role
        ({staff.cis_role ?? "unknown"}) is managed in CIS and does not change here.
      </p>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-[13px] font-medium text-ink">Booking role</legend>
        {STAFF_ROLES.map((r) => {
          const disabled = self && r !== "admin";
          return (
            <label
              key={r}
              className={`flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 ${
                role === r ? "border-accent-border bg-accent-tint" : "border-line bg-card"
              } ${disabled ? "cursor-not-allowed opacity-50" : ""}`}
            >
              <input type="radio" name="role" value={r} checked={role === r} disabled={disabled} onChange={() => setRole(r)} className="mt-1 accent-accent" />
              <span>
                <span className="block text-[13px] font-semibold text-ink">{ROLE_LABEL[r]}</span>
                <span className="block text-xs text-muted">{ROLE_HELP[r]}</span>
              </span>
            </label>
          );
        })}
      </fieldset>
      {self && <p className="text-xs text-muted">You cannot demote yourself. Another admin can.</p>}
    </FormDrawer>
  );
}
