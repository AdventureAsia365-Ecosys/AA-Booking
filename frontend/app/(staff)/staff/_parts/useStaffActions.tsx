"use client";
// app/(staff)/staff/_parts/useStaffActions.tsx
// AA-683 (fe-people) — admin actions on a Booking staff profile (UI spec §4.13): Change role (drawer) and
// Activate / Deactivate (confirm). Both are PATCH /booking/staff/{cis_user_id} {role?, active?, reason?}, admin
// only, audited server-side ("staff.updated"). AA-996 (Figma BLOCK ADMIN, spec §4.13): Deactivate asks for a reason
// (required here; the API keeps it optional) that is stored in the audit row only; Activate takes an optional one. Deactivate replaces Figma's Block / Suspend; there is no
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
import { Radio, ConfirmDialog, FormDrawer, useStaff, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./useStaffActions.messages";

/** What each Booking role can do, for the role picker (backend PERMISSIONS in app/auth/staff.py). */
export const ROLE_HELP: Record<StaffRole, string> = {
  admin: "Everything, including staff, customers' block / archive and the audit log.",
  sales: "Trip cases, proposals and customers.",
  contracting: "Reads trip cases and customers; suppliers, rates and pricing from wave 2.",
  content_creator: "Dashboard and the CIS catalog, read-only; content itself is written in CIS.",
  finance: "Invoices and payments; verifying a payment stays with an admin. Given on the CIS Staff page.",
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
  const t = useT(MSG);
  const tx = useTx();
  const me = useStaff();
  const qc = useQueryClient();
  const toast = useToast();
  const [pending, setPending] = useState<Pending>(null);
  const [roleFor, setRoleFor] = useState<StaffProfile | null>(null);

  const patch = useMutation({
    mutationFn: ({ staff, body }: { staff: StaffProfile; body: { active?: boolean; role?: string; reason?: string } }) =>
      api.staff.update(staff.cis_user_id, body),
    onSuccess: (updated, { staff, body }) => {
      qc.invalidateQueries({ queryKey: ["staff"] });
      const name = updated.name ?? staff.name ?? staff.email ?? "Staff member";
      if (body.role !== undefined) toast.success(t("{0} is now {1}.", name, tx(ROLE_LABEL[updated.role]) ?? updated.role));
      else toast.success(updated.active ? t("{0} can now use Booking.", name) : t("{0} can no longer use Booking.", name));
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
        onConfirm={(reason) =>
          pending &&
          patch.mutate({ staff: pending.staff, body: { active: pending.kind === "activate", ...(reason ? { reason } : {}) } })
        }
        busy={patch.isPending}
        error={patch.error ? staffErrorText(patch.error) : null}
        danger={pending?.kind === "deactivate"}
        title={pending?.kind === "activate" ? t("Activate staff member") : t("Deactivate staff member")}
        confirmLabel={pending?.kind === "activate" ? t("Activate") : t("Deactivate")}
        reason={
          pending?.kind === "deactivate"
            ? { label: t("Reason"), required: true, placeholder: t("e.g. left the company, contract ended"), maxLength: 500 }
            : { label: t("Note (optional)"), placeholder: t("e.g. back from leave"), maxLength: 500 }
        }
        message={
          pending?.kind === "activate" ? (
            <>
              {t("Activate")} <strong className="text-ink">{name}</strong> {t("as")}
              <strong className="text-ink">{ROLE_LABEL[pending.staff.role] ?? pending.staff.role}</strong>{t("? They can use Booking with that role from their next page load. Change the role first if it is not right.")}
            </>
          ) : (
            <>
              {t("Deactivate")} <strong className="text-ink">{name}</strong>{t("? They lose access to Booking at once. Their CIS account is not changed, and their open trip cases stay assigned to them until someone reassigns them. The reason is kept in the audit log.")}
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
  const t = useT(MSG);
  const tx = useTx();
  const me = useStaff();
  const [role, setRole] = useState<StaffRole>(staff.role);
  const self = staff.cis_user_id === me.cis_user_id;
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("Change Booking role")}
      submitLabel={t("Save role")}
      onSubmit={() => (role === staff.role ? onClose() : onSave(role))}
      busy={busy}
      dirty={role !== staff.role}
      error={tx(error)}
    >
      <p className="text-[13px] text-muted">
        {t("Role of")} <span className="font-medium text-ink">{staff.name ?? staff.email ?? staff.cis_user_id}</span> {t("in Booking. Their CIS role ({0}) is managed in CIS and does not change here.", staff.cis_role ?? "unknown")}
      </p>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-[13px] font-medium text-ink">{t("Booking role")}</legend>
        {STAFF_ROLES.map((r) => {
          const disabled = self && r !== "admin";
          return (
            <label
              key={r}
              className={`flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 ${
                role === r ? "border-accent-border bg-accent-tint" : "border-line bg-card"
              } ${disabled ? "cursor-not-allowed opacity-50" : ""}`}
            >
              <Radio name="role" value={r} checked={role === r} disabled={disabled} onChange={() => setRole(r)} style={{ marginTop: 4 }} />
              <span>
                <span className="block text-[13px] font-semibold text-ink">{tx(ROLE_LABEL[r])}</span>
                <span className="block text-xs text-muted">{tx(ROLE_HELP[r])}</span>
              </span>
            </label>
          );
        })}
      </fieldset>
      {self && <p className="text-xs text-muted">{t("You cannot demote yourself. Another admin can.")}</p>}
    </FormDrawer>
  );
}
