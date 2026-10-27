"use client";
// app/(staff)/staff/[cisUserId]/page.tsx — Staff detail (W1; UI spec §4.13; Figma USER DETAILS, Activity Log,
// Assigned Trips). GET /booking/staff/{id} (admin, or the staff member themself; 403 otherwise).
// Tabs: Overview (Booking-owned role + active flag, CIS-owned name / e-mail / CIS role), Assigned cases
// (open assignments), Activity (GET /booking/audit-logs?actor=, admin only, "Load more" by cursor).
// Admin actions on someone else: Change role, Activate / Deactivate (PATCH /booking/staff/{id}).

import { use } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, CalendarDays, CheckCircle2, Clock, Lock, Mail, Pencil, ShieldOff, UserRound } from "lucide-react";
import { api, ApiError } from "@/lib/api/client";
import type { AuditLogItem, StaffAssignmentRow, StaffDetail } from "@/lib/api/types";
import { ROLE_LABEL, isStaffRole } from "@/lib/rbac";
import { CIS_STAFF_PATH } from "@/lib/config";
import {
  Button,
  Code,
  DataTable,
  DateText,
  EmptyState,
  ErrorState,
  ManagedInCisLink,
  Pill,
  Section,
  StatusBadge,
  TripCaseStatusBadge,
  auditActionTone,
  useCan,
  useStaff,
} from "@/ui";
import { DetailHeader, DetailSkeleton, Fact, FactGrid, MetaItem, useDetailTab } from "@/app/(staff)/_parts/DetailHeader";
import { useStaffActions } from "../_parts/useStaffActions";

const ACTIVITY_PAGE = 50;

export default function StaffDetailPage({ params }: { params: Promise<{ cisUserId: string }> }) {
  const { cisUserId } = use(params);
  const id = decodeURIComponent(cisUserId);
  const isAdmin = useCan("staff.admin");
  const me = useStaff();
  const actions = useStaffActions();
  const q = useQuery({ queryKey: ["staff", id], queryFn: () => api.staff.get(id) });
  const tabs = [
    { key: "overview", label: "Overview" },
    { key: "cases", label: "Assigned cases", count: q.data?.counts.open_assignments },
    ...(isAdmin ? [{ key: "activity", label: "Activity log" }] : []),
  ];
  const [tab, setTab] = useDetailTab(tabs);

  if (q.isLoading) return <DetailSkeleton label="Loading staff profile" />;
  if (q.isError || !q.data) {
    const status = q.error instanceof ApiError ? q.error.status : 0;
    return (
      <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
        {status === 404 ? (
          <EmptyState
            icon={<UserRound size={30} />}
            title="Staff profile not found"
            description="A profile exists only after the person has signed in to Booking once."
            action={isAdmin ? <Link href="/staff" className="text-[13px] font-semibold text-accent-deep hover:underline">Back to staff</Link> : undefined}
          />
        ) : status === 403 ? (
          <EmptyState
            icon={<ShieldOff size={30} />}
            title="Only an admin can view another staff profile"
            description="You can still open your own profile."
            action={<Link href={`/staff/${encodeURIComponent(me.cis_user_id)}`} className="text-[13px] font-semibold text-accent-deep hover:underline">Open my profile</Link>}
          />
        ) : (
          <ErrorState message={(q.error as Error)?.message ?? "Could not load the profile."} onRetry={() => q.refetch()} />
        )}
      </main>
    );
  }

  const s = q.data;
  const self = actions.isSelf(s);
  const managed = actions.roleManagedInCis(s);
  const roleLabel = isStaffRole(s.role) ? ROLE_LABEL[s.role] : s.role;
  return (
    <main className="aa-admin-main flex-1 overflow-y-auto">
      <DetailHeader
        back={isAdmin ? { href: "/staff", label: "Staff" } : undefined}
        name={s.name ?? s.email ?? s.cis_user_id}
        badges={
          <>
            <Pill dot={false}>{roleLabel}</Pill>
            <StatusBadge kind="staff" value={s.active ? "active" : "inactive"} />
            {self && <Pill tone="accent" dot={false}>You</Pill>}
          </>
        }
        meta={
          <>
            <MetaItem icon={<Mail size={14} />}>{s.email ?? "No e-mail"}</MetaItem>
            <MetaItem icon={<CalendarDays size={14} />}>Since <DateText value={s.created_at} /></MetaItem>
            <MetaItem icon={<Clock size={14} />}>Last seen <DateText value={s.last_seen_at} relative /></MetaItem>
          </>
        }
        actions={
          isAdmin && !self ? (
            <>
              {s.active ? (
                <Button variant="danger" onClick={() => actions.deactivate(s)}>
                  <Ban size={14} /> Deactivate
                </Button>
              ) : (
                <Button variant="secondary" onClick={() => actions.activate(s)}>
                  <CheckCircle2 size={14} /> Activate
                </Button>
              )}
              {managed ? (
                <ManagedInCisLink path={`${CIS_STAFF_PATH}/${encodeURIComponent(id)}`} label="Role is set in CIS → Platform → Staff" />
              ) : (
                <Button variant="primary" onClick={() => actions.changeRole(s)}>
                  <Pencil size={14} /> Change role
                </Button>
              )}
            </>
          ) : undefined
        }
        tabs={tabs}
        activeTab={tab}
        onTab={setTab}
      />
      <div className="mx-auto max-w-[1440px] py-6 md:px-8">
        {!s.active && (
          <div role="status" className="mb-5 rounded-lg border border-warning bg-warning-soft px-4 py-3 text-[13px] text-body">
            {isAdmin && !self
              ? managed
                ? "This profile is not active: the person cannot use Booking. Their role comes from CIS; activate the profile here."
                : "This profile is not active: the person cannot use Booking yet. Check the role, then activate it."
              : "This profile is not active yet. A Booking admin needs to activate it."}
          </div>
        )}
        {tab === "overview" && <Overview s={s} roleLabel={roleLabel} />}
        {tab === "cases" && <AssignedCases rows={s.open_assignments} total={s.counts.assignments} />}
        {tab === "activity" && isAdmin && <Activity id={s.cis_user_id} />}
      </div>
      {actions.dialogs}
    </main>
  );
}

function Overview({ s, roleLabel }: { s: StaffDetail; roleLabel: string }) {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Section title="Booking access" description="Set in Booking by an admin.">
        <FactGrid>
          <Fact label="Booking role">{roleLabel}</Fact>
          <Fact label="Status">{s.active ? "Active" : "Not active"}</Fact>
          <Fact label="Open assignments">
            <span className="bk-tabular">{s.counts.open_assignments}</span>
            <span className="text-muted"> of {s.counts.assignments} in total</span>
          </Fact>
          <Fact label="Last seen"><DateText value={s.last_seen_at} withTime /></Fact>
          <Fact label="Profile created"><DateText value={s.created_at} withTime /></Fact>
          <Fact label="Last updated"><DateText value={s.updated_at} withTime /></Fact>
        </FactGrid>
      </Section>
      <Section
        title={
          <span className="inline-flex items-center gap-1.5">
            CIS account <Lock size={13} className="text-muted2" aria-label="Managed in CIS" />
          </span>
        }
        description="The account, its company e-mail sign-in and the Booking role are managed in CIS → Platform → Staff."
        actions={<ManagedInCisLink path={`${CIS_STAFF_PATH}/${encodeURIComponent(s.cis_user_id)}`} label="Open CIS → Platform → Staff" />}
      >
        <FactGrid>
          <Fact label="Name">{s.name ?? "—"}</Fact>
          <Fact label="E-mail"><span className="break-all">{s.email ?? "—"}</span></Fact>
          <Fact label="CIS role">{s.cis_role ?? "—"}</Fact>
          <Fact label="Sign-in">{s.managed_in_directory ? "Company e-mail (role from CIS)" : "CIS password (legacy)"}</Fact>
          <Fact label="Staff key"><span className="font-mono text-xs text-muted">{s.cis_user_id}</span></Fact>
        </FactGrid>
      </Section>
    </div>
  );
}

function AssignedCases({ rows, total }: { rows: StaffAssignmentRow[]; total: number }) {
  const router = useRouter();
  const columns: ColumnDef<StaffAssignmentRow, unknown>[] = [
    { id: "code", header: "Case", accessorKey: "code", cell: ({ row }) => <Code>{row.original.code}</Code> },
    { id: "customer", header: "Customer", accessorFn: (r) => r.customer_name ?? "", cell: ({ row }) => <span className="text-[13px] text-body">{row.original.customer_name ?? "—"}</span> },
    { id: "role", header: "Assigned as", accessorKey: "role", cell: ({ row }) => <span className="text-[13px] capitalize text-body">{row.original.role}</span> },
    { id: "status", header: "Status", accessorKey: "status", cell: ({ row }) => <TripCaseStatusBadge status={row.original.status} /> },
    { id: "start", header: "Trip starts", accessorKey: "start_date", cell: ({ row }) => <DateText value={row.original.start_date} className="text-[13px] text-body" /> },
    { id: "since", header: "Assigned", accessorKey: "assigned_at", cell: ({ row }) => <DateText value={row.original.assigned_at} className="text-[13px] text-muted" /> },
  ];
  return (
    <Section
      title="Open trip cases"
      description={total > rows.length ? `${total - rows.length} completed or cancelled cases are not listed.` : "Trip cases assigned to this person that are still open."}
    >
      <DataTable<StaffAssignmentRow>
        tableId="staff-assigned"
        data={rows}
        columns={columns}
        getRowId={(r) => `${r.trip_case_id}-${r.role}`}
        pageSizeOptions={[25, 50, 100]}
        emptyTitle="No open assignments"
        emptyDescription="Open trip cases assigned to this staff member appear here."
        onRowClick={(r) => router.push(`/cases/${encodeURIComponent(r.code)}`)}
        tableMinWidth={720}
      />
    </Section>
  );
}

function Activity({ id }: { id: string }) {
  const feed = useInfiniteQuery({
    queryKey: ["audit", "actor", id],
    queryFn: ({ pageParam }) => api.reference.auditLogs({ actor: id, limit: ACTIVITY_PAGE, before: pageParam ?? undefined }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before,
  });
  const items: AuditLogItem[] = feed.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <Section title="Activity log" description="Every audited change made by this person, newest first.">
      {feed.isLoading ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-10 animate-pulse rounded-md bg-line2" />
          ))}
        </div>
      ) : feed.isError ? (
        <ErrorState message={(feed.error as Error).message} onRetry={() => feed.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState title="No recorded actions yet" description="Changes this person makes in Booking are listed here." />
      ) : (
        <>
          <ol className="flex flex-col">
            {items.map((a) => (
              <li key={a.id} className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 border-b border-line2 py-3 first:pt-0 last:border-b-0">
                <span className="flex min-w-0 flex-wrap items-center gap-2">
                  <Pill tone={auditActionTone(a.action)} dot={false} upper>{a.action.replace(/[._]/g, " ")}</Pill>
                  <span className="min-w-0 text-[13px] text-ink">
                    {a.summary ?? a.action}
                    {a.entity_label && <span className="text-muted"> · {a.entity_label}</span>}
                  </span>
                </span>
                <DateText value={a.at} withTime className="shrink-0 text-xs text-muted" />
              </li>
            ))}
          </ol>
          {feed.hasNextPage && (
            <div className="mt-4 flex justify-center">
              <Button variant="ghost" onClick={() => feed.fetchNextPage()} disabled={feed.isFetchingNextPage}>
                {feed.isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </>
      )}
    </Section>
  );
}
