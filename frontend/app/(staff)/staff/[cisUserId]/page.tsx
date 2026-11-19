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
  PageFrame,
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
  auditActionLabel,
  auditActionTone,
  useCan,
  useStaff,
} from "@/ui";
import { DetailHeader, DetailSkeleton, Fact, FactGrid, MetaItem, useDetailTab } from "@/app/(staff)/_parts/DetailHeader";
import { useStaffActions } from "../_parts/useStaffActions";
import { casePartyLabel } from "@/ui/booking/caseFacts";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./page.messages";

const ACTIVITY_PAGE = 50;

export default function StaffDetailPage({ params }: { params: Promise<{ cisUserId: string }> }) {
  const t = useT(MSG);
  const tx = useTx();
  const { cisUserId } = use(params);
  const id = decodeURIComponent(cisUserId);
  const isAdmin = useCan("staff.admin");
  const me = useStaff();
  const actions = useStaffActions();
  const q = useQuery({ queryKey: ["staff", id], queryFn: () => api.staff.get(id) });
  const tabs = [
    { key: "overview", label: t("Overview") },
    { key: "cases", label: t("Assigned cases"), count: q.data?.counts.open_assignments },
    ...(isAdmin ? [{ key: "activity", label: t("Activity log") }] : []),
  ];
  const [tab, setTab] = useDetailTab(tabs);

  if (q.isLoading) return <DetailSkeleton label={t("Loading staff profile")} />;
  if (q.isError || !q.data) {
    const status = q.error instanceof ApiError ? q.error.status : 0;
    return (
      <PageFrame width="full">
        {status === 404 ? (
          <EmptyState
            icon={<UserRound size={30} />}
            title={t("Staff profile not found")}
            description={t("A profile exists only after the person has signed in to Booking once.")}
            action={isAdmin ? <Link href="/staff" className="text-[13px] font-semibold text-accent-deep hover:underline">{t("Back to staff")}</Link> : undefined}
          />
        ) : status === 403 ? (
          <EmptyState
            icon={<ShieldOff size={30} />}
            title={t("Only an admin can view another staff profile")}
            description={t("You can still open your own profile.")}
            action={<Link href={`/staff/${encodeURIComponent(me.cis_user_id)}`} className="text-[13px] font-semibold text-accent-deep hover:underline">{t("Open my profile")}</Link>}
          />
        ) : (
          <ErrorState message={(q.error as Error)?.message ?? t("Could not load the profile.")} onRetry={() => q.refetch()} />
        )}
      </PageFrame>
    );
  }

  const s = q.data;
  const self = actions.isSelf(s);
  const managed = actions.roleManagedInCis(s);
  const roleLabel = isStaffRole(s.role) ? ROLE_LABEL[s.role] : s.role;
  return (
    <PageFrame bleed>
      <DetailHeader
        back={isAdmin ? { href: "/staff", label: t("Staff") } : undefined}
        name={s.name ?? s.email ?? s.cis_user_id}
        badges={
          <>
            <Pill dot={false}>{tx(roleLabel)}</Pill>
            <StatusBadge kind="staff" value={s.active ? "active" : "inactive"} />
            {self && <Pill tone="accent" dot={false}>{t("You")}</Pill>}
          </>
        }
        meta={
          <>
            <MetaItem icon={<Mail size={14} />}>{s.email ?? t("No e-mail")}</MetaItem>
            <MetaItem icon={<CalendarDays size={14} />}>{t("Since")} <DateText value={s.created_at} /></MetaItem>
            <MetaItem icon={<Clock size={14} />}>{t("Last seen")} <DateText value={s.last_seen_at} relative /></MetaItem>
          </>
        }
        actions={
          isAdmin && !self ? (
            <>
              {s.active ? (
                <Button variant="danger" onClick={() => actions.deactivate(s)}>
                  <Ban size={14} /> {t("Deactivate")}
                </Button>
              ) : (
                <Button variant="secondary" onClick={() => actions.activate(s)}>
                  <CheckCircle2 size={14} /> {t("Activate")}
                </Button>
              )}
              {managed ? (
                <ManagedInCisLink path={`${CIS_STAFF_PATH}/${encodeURIComponent(id)}`} label={t("Role is set in CIS → Platform → Staff")} />
              ) : (
                <Button variant="primary" onClick={() => actions.changeRole(s)}>
                  <Pencil size={14} /> {t("Change role")}
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
                ? t("This profile is not active: the person cannot use Booking. Their role comes from CIS; activate the profile here.")
                : t("This profile is not active: the person cannot use Booking yet. Check the role, then activate it.")
              : t("This profile is not active yet. A Booking admin needs to activate it.")}
          </div>
        )}
        {tab === "overview" && <Overview s={s} roleLabel={tx(roleLabel)} />}
        {tab === "cases" && <AssignedCases rows={s.open_assignments} total={s.counts.assignments} />}
        {tab === "activity" && isAdmin && <Activity id={s.cis_user_id} />}
      </div>
      {actions.dialogs}
    </PageFrame>
  );
}

function Overview({ s, roleLabel }: { s: StaffDetail; roleLabel: string }) {
  const t = useT(MSG);
  const tx = useTx();
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Section title={t("Booking access")} description={t("Set in Booking by an admin.")}>
        <FactGrid>
          <Fact label={t("Booking role")}>{tx(roleLabel)}</Fact>
          <Fact label={t("Status")}>{s.active ? t("Active") : t("Not active")}</Fact>
          <Fact label={t("Open assignments")}>
            <span className="bk-tabular">{s.counts.open_assignments}</span>
            <span className="text-muted"> {t("of {0} in total", s.counts.assignments)}</span>
          </Fact>
          <Fact label={t("Last seen")}><DateText value={s.last_seen_at} withTime /></Fact>
          <Fact label={t("Profile created")}><DateText value={s.created_at} withTime /></Fact>
          <Fact label={t("Last updated")}><DateText value={s.updated_at} withTime /></Fact>
        </FactGrid>
      </Section>
      <Section
        title={
          <span className="inline-flex items-center gap-1.5">
            {t("CIS account")} <Lock size={13} className="text-muted2" aria-label={t("Managed in CIS")} />
          </span>
        }
        description={t("The account, its company e-mail sign-in and the Booking role are managed in CIS → Platform → Staff.")}
        actions={<ManagedInCisLink path={`${CIS_STAFF_PATH}/${encodeURIComponent(s.cis_user_id)}`} label={t("Open CIS → Platform → Staff")} />}
      >
        <FactGrid>
          <Fact label={t("Name")}>{s.name ?? "—"}</Fact>
          <Fact label="E-mail"><span className="break-all">{s.email ?? "—"}</span></Fact>
          <Fact label={t("CIS role")}>{s.cis_role ?? "—"}</Fact>
          <Fact label={t("Sign-in")}>{s.managed_in_directory ? t("Company e-mail (role from CIS)") : t("CIS password (legacy)")}</Fact>
          <Fact label={t("Staff key")}><span className="font-mono text-xs text-muted">{s.cis_user_id}</span></Fact>
        </FactGrid>
      </Section>
    </div>
  );
}

function AssignedCases({ rows, total }: { rows: StaffAssignmentRow[]; total: number }) {
  const t = useT(MSG);
  const tx = useTx();
  const router = useRouter();
  const columns: ColumnDef<StaffAssignmentRow, unknown>[] = [
    { id: "code", header: t("Case"), accessorKey: "code", cell: ({ row }) => <Code>{row.original.code}</Code> },
    { id: "customer", header: t("For"), accessorFn: (r) => casePartyLabel(r), cell: ({ row }) => <span className="text-[13px] text-body">{tx(casePartyLabel(row.original))}</span> },
    { id: "role", header: t("Assigned as"), accessorKey: "role", cell: ({ row }) => <span className="text-[13px] capitalize text-body">{row.original.role}</span> },
    { id: "status", header: t("Status"), accessorKey: "status", cell: ({ row }) => <TripCaseStatusBadge status={row.original.status} /> },
    { id: "start", header: t("Trip starts"), accessorKey: "start_date", cell: ({ row }) => <DateText value={row.original.start_date} className="text-[13px] text-body" /> },
    { id: "since", header: t("Assigned"), accessorKey: "assigned_at", cell: ({ row }) => <DateText value={row.original.assigned_at} className="text-[13px] text-muted" /> },
  ];
  return (
    <Section
      title={t("Open trip cases")}
      description={total > rows.length ? t("{0} completed or cancelled cases are not listed.", total - rows.length) : t("Trip cases assigned to this person that are still open.")}
    >
      <DataTable<StaffAssignmentRow>
        tableId="staff-assigned"
        data={rows}
        columns={columns}
        getRowId={(r) => `${r.trip_case_id}-${r.role}`}
        pageSizeOptions={[25, 50, 100]}
        emptyTitle={t("No open assignments")}
        emptyDescription={t("Open trip cases assigned to this staff member appear here.")}
        onRowClick={(r) => router.push(`/cases/${encodeURIComponent(r.code)}`)}
        tableMinWidth={720}
      />
    </Section>
  );
}

function Activity({ id }: { id: string }) {
  const t = useT(MSG);
  const tx = useTx();
  const feed = useInfiniteQuery({
    queryKey: ["audit", "actor", id],
    queryFn: ({ pageParam }) => api.reference.auditLogs({ actor: id, limit: ACTIVITY_PAGE, before: pageParam ?? undefined }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before,
  });
  const items: AuditLogItem[] = feed.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <Section title={t("Activity log")} description={t("Every audited change made by this person, newest first.")}>
      {feed.isLoading ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-10 animate-pulse rounded-md bg-line2" />
          ))}
        </div>
      ) : feed.isError ? (
        <ErrorState message={tx((feed.error as Error).message)} onRetry={() => feed.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState title={t("No recorded actions yet")} description={t("Changes this person makes in Booking are listed here.")} />
      ) : (
        <>
          <ol className="flex flex-col">
            {items.map((a) => (
              <li key={a.id} className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 border-b border-line2 py-3 first:pt-0 last:border-b-0">
                <span className="flex min-w-0 flex-wrap items-center gap-2">
                  <Pill tone={auditActionTone(a)} dot={false} upper>{tx(auditActionLabel(a))}</Pill>
                  <span className="min-w-0 text-[13px] text-ink">
                    {a.summary ?? a.action}
                    {a.entity_label && <span className="text-muted"> · {tx(a.entity_label)}</span>}
                  </span>
                </span>
                <DateText value={a.at} withTime className="shrink-0 text-xs text-muted" />
              </li>
            ))}
          </ol>
          {feed.hasNextPage && (
            <div className="mt-4 flex justify-center">
              <Button variant="ghost" onClick={() => feed.fetchNextPage()} disabled={feed.isFetchingNextPage}>
                {feed.isFetchingNextPage ? t("Loading…") : t("Load more")}
              </Button>
            </div>
          )}
        </>
      )}
    </Section>
  );
}
