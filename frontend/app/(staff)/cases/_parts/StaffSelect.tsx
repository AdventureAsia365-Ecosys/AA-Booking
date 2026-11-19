"use client";
// app/(staff)/cases/_parts/StaffSelect.tsx
// AA-683 (fe-cases) — the spec §5 StaffPicker as a plain select over GET /booking/staff/directory (active
// staff only, any role: the API lets any active staff hold the sales role on a case). Sales staff are
// listed first; the signed-in user is marked "(me)".

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import type { StaffDirectoryEntry } from "@/lib/api/types";
import { ROLE_LABEL, isStaffRole } from "@/lib/rbac";
import { Select, useStaff } from "@/ui";
import { inputCls } from "./fields";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./StaffSelect.messages";

export function useStaffDirectory() {
  return useQuery({
    queryKey: ["staff", "directory", "all"],
    queryFn: () => api.staff.directory(),
    staleTime: 5 * 60_000,
  });
}

export function sortStaff(items: StaffDirectoryEntry[]): StaffDirectoryEntry[] {
  return [...items].sort((a, b) => {
    const sa = a.role === "sales" ? 0 : 1;
    const sb = b.role === "sales" ? 0 : 1;
    if (sa !== sb) return sa - sb;
    return (a.name ?? a.cis_user_id).localeCompare(b.name ?? b.cis_user_id);
  });
}

export function StaffSelect({
  value,
  onChange,
  emptyLabel = "Unassigned",
  id,
  ariaLabel,
}: {
  value: string;
  onChange: (cisUserId: string) => void;
  emptyLabel?: string;
  id?: string;
  ariaLabel?: string;
}) {
  const t = useT(MSG);
  const tx = useTx();
  const me = useStaff();
  const directory = useStaffDirectory();
  const items = sortStaff(directory.data?.items ?? []);
  // Keep a current holder selectable even when the directory no longer lists them (deactivated).
  const known = !value || items.some((s) => s.cis_user_id === value);
  return (
    <Select
      id={id}
      aria-label={tx(ariaLabel)}
      className={inputCls}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={directory.isLoading}
    >
      <option value="">{directory.isLoading ? t("Loading staff…") : emptyLabel}</option>
      {!known && <option value={value}>{value} {t("(inactive)")}</option>}
      {items.map((s) => (
        <option key={s.cis_user_id} value={s.cis_user_id}>
          {s.name ?? s.cis_user_id}
          {s.role !== "sales" ? ` · ${tx(isStaffRole(s.role) ? ROLE_LABEL[s.role] : s.role)}` : ""}
          {s.cis_user_id === me.cis_user_id ? t(" (me)") : ""}
        </option>
      ))}
    </Select>
  );
}
