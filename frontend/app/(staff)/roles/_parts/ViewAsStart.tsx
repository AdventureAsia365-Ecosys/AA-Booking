"use client";
// app/(staff)/roles/_parts/ViewAsStart.tsx — AA-1157: Roles › "View as a role" (owners only: `can_view_as` of GET
// /booking/staff-roles). Pick a built-in or active custom role, a length (15 / 30 / 60 minutes) and an optional reason,
// then POST /api/view-as (the BFF keeps the session id in an httpOnly cookie) and reload the dashboard as that role.
// The API re-checks owner and downward (a role that sees more than the owner is refused).

import { useState } from "react";
import { Eye } from "lucide-react";
import type { StaffRoles } from "@/lib/api/staffRoles";
import { STAFF_ROLES, roleName } from "@/lib/rbac";
import { DEFAULT_MINUTES, MINUTE_CHOICES, VIEW_AS_API } from "@/lib/viewAs";
import { Button, Field, Input, Section, Select } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { VIEW_AS_MSG } from "@/ui/local/ViewAsBanner.messages";

/** The roles an owner may pick: every built-in role and every active custom role. */
export function viewableRoles(data: StaffRoles): { value: string; label: string }[] {
  const custom = data.custom.filter((r) => !r.archived).map((r) => ({ value: r.code, label: r.label }));
  return [...STAFF_ROLES.map((r) => ({ value: r as string, label: roleName(r) })), ...custom];
}

export function ViewAsStart({ data }: { data: StaffRoles }) {
  const t = useT(VIEW_AS_MSG);
  const tx = useTx();
  const roles = viewableRoles(data);
  const [role, setRole] = useState(roles[0]?.value ?? "");
  const [minutes, setMinutes] = useState<number>(DEFAULT_MINUTES);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await fetch(VIEW_AS_API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role, minutes, reason: reason.trim() || null }),
      });
      if (r.ok) {
        window.location.assign("/");
        return;
      }
      const detail = ((await r.json().catch(() => ({}))) as { detail?: unknown }).detail;
      setError(
        detail === "view_as_not_downward" ? t("errNotDownward")
          : detail === "view_as_unknown_role" ? t("errUnknown")
            : detail === "owner_only" ? t("errOwner") : t("startFailed"),
      );
    } catch {
      setError(t("startFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title={t("sectionTitle")} description={t("sectionIntro")}>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_160px_minmax(0,1fr)_auto] sm:items-end" data-testid="view-as-start">
        <Field label={t("roleLabel")}>
          <Select value={role} onChange={(e) => setRole(e.target.value)} options={roles.map((r) => ({ ...r, label: tx(r.label) }))} />
        </Field>
        <Field label={t("minutesLabel")}>
          <Select
            value={String(minutes)}
            onChange={(e) => setMinutes(Number(e.target.value))}
            options={MINUTE_CHOICES.map((n) => ({ value: String(n), label: t("minutesOption", n) }))}
          />
        </Field>
        <Field label={t("reasonLabel")}>
          <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <Button variant="primary" onClick={start} disabled={busy || !role}>
          <Eye size={14} /> {busy ? t("starting") : t("start")}
        </Button>
      </div>
      {error ? (
        <p role="alert" className="mt-2 text-xs text-danger">
          {error}
        </p>
      ) : null}
    </Section>
  );
}
