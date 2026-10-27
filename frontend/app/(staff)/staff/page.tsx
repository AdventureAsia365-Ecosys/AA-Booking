"use client";
// app/(staff)/staff/page.tsx — Staff (W1, admin only; UI spec §4.13; Figma ADMINS, BLOCK ADMIN).
// GET /booking/staff (admin). Accounts are created in CIS (ADR 0004 O1); Booking keeps a profile per CIS
// user with a Booking role and an active flag. KPI tiles, search + role / status filters (client-side: the
// endpoint returns every profile, one row per CIS user who signed in), per-row View / Change role /
// Activate or Deactivate (PATCH /booking/staff/{cis_user_id}).
// AA-996 (Figma USER.png "Invite User", RESET PASSWORD): invites and sign-in recovery live in CIS → Platform → Staff
// (O1): the page action "Manage staff & invites in CIS ↗" and a per-row "Sign-in & invites in CIS ↗" link replace them.

import { useMemo } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, CheckCircle2, Eye, KeyRound, Pencil } from "lucide-react";
import { api } from "@/lib/api/client";
import type { StaffProfile } from "@/lib/api/types";
import { ROLE_LABEL, STAFF_ROLES, isStaffRole } from "@/lib/rbac";
import { CIS_STAFF_PATH, CIS_STAFF_URL } from "@/lib/config";
import { DataTable, DateText, FilterBar, KpiTile, ManagedInCisLink, PageFrame, PersonCell, Pill, Section, StatusBadge, useUrlFilters } from "@/ui";
import { useStaffActions } from "./_parts/useStaffActions";

export default function StaffPage() {
  const router = useRouter();
  const q = useQuery({ queryKey: ["staff", "list"], queryFn: () => api.staff.list() });
  const actions = useStaffActions();
  const { get } = useUrlFilters();
  const items = useMemo(() => q.data?.items ?? [], [q.data]);
  const value = (n: number) => (q.data ? n : q.isLoading ? undefined : null);
  const pending = items.filter((s) => !s.active).length;

  const search = get("q").trim().toLowerCase();
  const role = get("role");
  const status = get("status");
  const rows = useMemo(
    () =>
      items.filter(
        (s) =>
          (!search || `${s.name ?? ""} ${s.email ?? ""}`.toLowerCase().includes(search)) &&
          (!role || s.role === role) &&
          (!status || (status === "active") === s.active),
      ),
    [items, search, role, status],
  );
  const filtered = !!(search || role || status);

  const columns: ColumnDef<StaffProfile, unknown>[] = [
    ...COLUMNS,
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => {
        const s = row.original;
        const self = actions.isSelf(s);
        const managed = actions.roleManagedInCis(s);
        return (
          <span className="inline-flex gap-1.5" onClick={(e) => e.stopPropagation()}>
            <IconAction label="View profile" onClick={() => router.push(`/staff/${encodeURIComponent(s.cis_user_id)}`)}>
              <Eye size={15} />
            </IconAction>
            <IconAction
              label={self ? "You cannot change your own role" : managed ? "Role is set in CIS → Platform → Staff" : "Change role"}
              disabled={self || managed}
              onClick={() => actions.changeRole(s)}
            >
              <Pencil size={15} />
            </IconAction>
            <a
              href={CIS_STAFF_URL}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`Sign-in & invites in CIS for ${s.name ?? s.email ?? s.cis_user_id}`}
              title="Sign-in & invites in CIS ↗ (no password exists in Booking)"
              className="grid h-8 w-8 place-items-center rounded-md border border-line bg-card text-muted hover:border-accent-border hover:text-ink"
            >
              <KeyRound size={15} />
            </a>
            {s.active ? (
              <IconAction label={self ? "You cannot deactivate yourself" : "Deactivate"} danger disabled={self} onClick={() => actions.deactivate(s)}>
                <Ban size={15} />
              </IconAction>
            ) : (
              <IconAction label="Activate" success onClick={() => actions.activate(s)}>
                <CheckCircle2 size={15} />
              </IconAction>
            )}
          </span>
        );
      },
    },
  ];

  return (
    <PageFrame
      title="Staff"
      description="Booking profiles of CIS staff accounts: their Booking role and whether they can use Booking."
      actions={<ManagedInCisLink path={CIS_STAFF_PATH} label="Manage staff & invites in CIS" />}
    >
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-accent-border bg-accent-tint px-4 py-3 text-[13px] text-body">
        <span>
          Staff accounts, invites and Booking roles are managed in CIS → Platform → Staff (company e-mail sign-in, no
          password). A person appears here after their first sign-in to Booking.
        </span>
        <ManagedInCisLink path={CIS_STAFF_PATH} label="Open CIS → Platform → Staff" />
      </div>
      <div className="aa-kpi-grid grid grid-cols-1 gap-4 sm:grid-cols-3">
        <KpiTile label="Total staff" value={value(items.length)} />
        <KpiTile label="Active" value={value(items.length - pending)} />
        <KpiTile label="Pending activation" value={value(pending)} hint={pending > 0 ? "Waiting for an admin to activate" : undefined} hintTone="warning" />
      </div>
      <Section>
        <div className="mb-4">
          <FilterBar
            search={{ placeholder: "Search name or e-mail" }}
            selects={[
              { key: "status", label: "Status", allLabel: "All statuses", options: [{ value: "active", label: "Active" }, { value: "inactive", label: "Pending / inactive" }] },
              { key: "role", label: "Role", allLabel: "All roles", options: STAFF_ROLES.map((r) => ({ value: r, label: ROLE_LABEL[r] })) },
            ]}
          />
        </div>
        <DataTable<StaffProfile>
          tableId="staff"
          data={rows}
          columns={columns}
          getRowId={(r) => r.cis_user_id}
          loading={q.isLoading}
          error={q.isError ? (q.error as Error).message : null}
          onRetry={() => q.refetch()}
          searchable={false}
          pageSizeOptions={[25, 50, 100]}
          enableCsv
          csvFilename="staff.csv"
          emptyTitle={filtered ? "No staff match" : "No staff profiles yet"}
          emptyDescription={
            filtered ? "Change the search or the filters." : "A profile is created the first time a CIS staff member signs in to Booking."
          }
          onRowClick={(r) => router.push(`/staff/${encodeURIComponent(r.cis_user_id)}`)}
          tableMinWidth={900}
        />
      </Section>
      {actions.dialogs}
    </PageFrame>
  );
}

function IconAction({
  label,
  onClick,
  children,
  danger,
  success,
  disabled,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  danger?: boolean;
  success?: boolean;
  disabled?: boolean;
}) {
  const tone = danger ? "text-danger hover:bg-danger-soft" : success ? "text-success hover:bg-success-soft" : "text-muted hover:text-ink hover:border-accent-border";
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={`grid h-8 w-8 cursor-pointer place-items-center rounded-md border border-line bg-card disabled:cursor-not-allowed disabled:opacity-40 ${tone}`}
    >
      {children}
    </button>
  );
}

const COLUMNS: ColumnDef<StaffProfile, unknown>[] = [
  { id: "name", header: "User", accessorFn: (r) => r.name ?? r.email ?? "", cell: ({ row }) => <PersonCell className="md:max-w-[240px]" name={row.original.name ?? row.original.email} sub={row.original.email} /> },
  { id: "role", header: "Booking role", accessorKey: "role", cell: ({ row }) => <Pill dot={false}>{isStaffRole(row.original.role) ? ROLE_LABEL[row.original.role] : row.original.role}</Pill> },
  { id: "cis_role", header: "CIS role", accessorFn: (r) => r.cis_role ?? "", cell: ({ row }) => <span className="text-[13px] text-muted">{row.original.cis_role ?? "—"}</span> },
  { id: "status", header: "Status", accessorFn: (r) => (r.active ? "active" : "inactive"), cell: ({ row }) => <StatusBadge kind="staff" value={row.original.active ? "active" : "inactive"} /> },
  { id: "seen", header: "Last seen", accessorKey: "last_seen_at", cell: ({ row }) => <DateText value={row.original.last_seen_at} relative className="text-[13px] text-muted" /> },
];
