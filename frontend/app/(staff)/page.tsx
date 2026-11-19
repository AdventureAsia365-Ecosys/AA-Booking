"use client";
// app/(staff)/page.tsx — Dashboard (W1, UI spec §4.2; Figma OVERVIEW).
//
// Data (all real, no placeholders):
//   GET /booking/trip-cases/summary                       → KPI tiles + pipeline by phase
//   GET /booking/trip-cases?sort=-updated_at&limit=5      → "Recent trip cases"
//   GET /booking/trip-cases?assignee=me&status=<open>&…   → "My queue"
//   GET /booking/staff?active=false (admin only)          → "Inactive staff" tile (spec Q6; AA-1128 wording)
//
// Tiles follow the role (Nghiệp S236): New inquiries, In progress, Proposals sent, and a fourth tile that is
// "Inactive staff" for admins and "My open cases" for sales / contracting. content_creator has no trip
// case permission, so it gets the catalog card (CIS) instead. W2/W3 tiles (Active suppliers, Outstanding
// balance, Rate sheets awaiting publish) are added when their endpoints exist, never as dummies.
// Every tile's hint describes its own number. The open-case total and the month-over-month delta (spec §4.2 "Open
// trip cases") sit on the Pipeline by phase card (S237: under "In progress" they read as a mismatch, 0 vs "1 open").

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowRight, Hammer, Hotel, Lock, Send, UserCheck, UserPlus } from "lucide-react";
import { ApiError, api } from "@/lib/api/client";
import { supplierBookingsApi } from "@/lib/api/supplierBookings";
import type { TripCaseListItem, TripCaseStatus, TripCaseSummary } from "@/lib/api/types";
import { canSeePage } from "@/lib/rbac";
import {
  Code,
  DataTable,
  DateText,
  EmptyState,
  ErrorState,
  KpiTile,
  ManagedInCisLink,
  PageFrame,
  PersonCell,
  STATUS_PHASES,
  Section,
  SkeletonBar,
  TripCaseStatusBadge,
  staffDisplay,
  useCan,
  useIsPhone,
  useStaff,
} from "@/ui";
import { retryUnlessClientError } from "@/ui/booking/QueryProvider";
import { casePartyLabel, casePartySub } from "@/ui/booking/caseFacts";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./page.messages";

/** Worked on by staff, not yet sent to the customer (phase "Inquiry" after triage + "Proposal" building). */
const IN_PROGRESS: TripCaseStatus[] = ["discovery", "building"];
/** Every status that still needs work (the summary's `open` uses the same rule). */
const OPEN_STATUSES = STATUS_PHASES.flatMap((p) => p.statuses).filter((s) => s !== "completed");

const EMPTY_CASES = "Partner agency bookings appear here.";

const casesHref = (statuses: readonly string[]) => `/cases?status=${encodeURIComponent(statuses.join(","))}`;

export default function DashboardPage() {
  const tr = useT(MSG);
  const staff = useStaff();
  const { name } = staffDisplay(staff);
  const canCases = canSeePage(staff.role, "/cases");
  return (
    <PageFrame title={tr("Dashboard")} description={tr("Welcome back, {0}. Here is what needs your attention today.", name)}>
      {canCases ? <SalesDashboard role={staff.role} /> : <CatalogDashboard />}
    </PageFrame>
  );
}

function isForbidden(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403;
}

/** Permission-denied state (the API refused; the shell already hides pages a role cannot open). */
function NoAccess({ what }: { what: string }) {
  const tr = useT(MSG);
  return (
    <EmptyState
      icon={<Lock size={28} />}
      title={tr("You do not have access to this")}
      description={tr("Your Booking role cannot see {0}. Ask a Booking admin if you need it.", what)}
    />
  );
}

function TileLink({ href, label, children }: { href: string; label: string; children: React.ReactNode }) {
  const tx = useTx();
  return (
    <Link
      href={href}
      aria-label={tx(label)}
      className="block min-w-0 rounded-lg outline-none [&>div]:h-full transition hover:-translate-y-px hover:shadow-kit-md focus-visible:ring-2 focus-visible:ring-accent-border"
    >
      {children}
    </Link>
  );
}

/** AA-682 S1: supplier bookings waiting for the supplier's answer (requested), the late ones (past the
 *  supplier_answer_days setting) and those not yet requested. Opens the cases in Supplier confirming. */
function SupplierAnswers() {
  const tr = useT(MSG);
  const canOps = useCan("ops.read");
  const q = useQuery({
    queryKey: ["supplier-bookings", "summary"],
    queryFn: supplierBookingsApi.summary,
    enabled: canOps,
    retry: retryUnlessClientError,
  });
  if (!canOps) return null;
  const d = q.data;
  return (
    <div className="aa-kpi-grid grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
      <TileLink href={casesHref(["confirmed", "supplier_confirming"])} label={tr("Supplier answers pending: open the cases booking suppliers")}>
        <KpiTile
          label={tr("Supplier answers pending")}
          icon={<Hotel size={16} />}
          value={d ? d.requested : q.isError ? null : undefined}
          hint={
            q.isError
              ? tr("Could not load supplier bookings")
              : d
                ? d.overdue > 0
                  ? tr("{0} waiting more than {1} days", d.overdue, d.answer_days)
                  : d.pending > 0
                    ? tr("{0} still to request", d.pending)
                    : tr("Nothing late")
                : undefined
          }
          hintTone={d && d.overdue > 0 ? "danger" : d && d.pending > 0 ? "warning" : "neutral"}
        />
      </TileLink>
    </div>
  );
}

function SalesDashboard({ role }: { role: string }) {
  const tr = useT(MSG);
  const tx = useTx();
  const router = useRouter();
  const isPhone = useIsPhone();
  const isAdmin = role === "admin";

  const summary = useQuery({ queryKey: ["trip-cases", "summary"], queryFn: api.tripCases.summary, retry: retryUnlessClientError });
  const recent = useQuery({
    queryKey: ["trip-cases", "recent"],
    queryFn: () => api.tripCases.list({ sort: "-updated_at", limit: 5 }),
    retry: retryUnlessClientError,
  });
  const pending = useQuery({
    queryKey: ["staff", "list", { active: false }],
    queryFn: () => api.staff.list(false),
    enabled: isAdmin,
    retry: retryUnlessClientError,
  });

  if (isForbidden(summary.error) && isForbidden(recent.error)) {
    return (
      <Section>
        <NoAccess what={tr("trip cases")} />
      </Section>
    );
  }

  const s = summary.data;
  const v = (n: (x: TripCaseSummary) => number) => (s ? n(s) : summary.isError ? null : undefined);
  const inProgress = (x: TripCaseSummary) => IN_PROGRESS.reduce((t, k) => t + (x.by_status[k] ?? 0), 0);
  const pendingCount = pending.data?.items.length;

  return (
    <>
      <div className="aa-kpi-grid grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        {/* AA-1046: no "New inquiries" tile: TripPlanner inquiries were the B2C intake, retired with it. */}
        <TileLink href={casesHref(IN_PROGRESS)} label={tr("In progress: open the cases being worked on")}>
          <KpiTile
            label={tr("In progress")}
            icon={<Hammer size={16} />}
            value={v(inProgress)}
            hint={s ? tr("In discovery or building") : undefined}
          />
        </TileLink>
        <TileLink href={casesHref(["proposal_sent"])} label={tr("Proposals sent: open the cases waiting for the customer")}>
          <KpiTile
            label={tr("Proposals sent")}
            icon={<Send size={16} />}
            value={v((x) => x.proposals_awaiting)}
            hint={s ? tr("Waiting for the customer") : undefined}
            hintTone={s && s.proposals_awaiting > 0 ? "warning" : "neutral"}
          />
        </TileLink>
        {isAdmin ? (
          <TileLink href="/staff" label={tr("Inactive staff: open staff")}>
            <KpiTile
              label={tr("Inactive staff")}
              icon={<UserPlus size={16} />}
              value={pendingCount ?? (pending.isError ? null : undefined)}
              hint={
                pending.isError
                  ? tr("Could not load staff")
                  : pendingCount === undefined
                    ? undefined
                    : pendingCount > 0
                      ? tr("Not active: never signed in or deactivated")
                      : tr("Nobody waiting")
              }
              hintTone={pendingCount ? "warning" : pending.isError ? "danger" : "neutral"}
            />
          </TileLink>
        ) : (
          <TileLink href="/cases?tab=mine" label={tr("My open cases: open my cases")}>
            <KpiTile
              label={tr("My open cases")}
              icon={<UserCheck size={16} />}
              value={v((x) => x.mine)}
              hint={s ? tr("Open cases with you on them") : undefined}
            />
          </TileLink>
        )}
      </div>
      {summary.isError && !isForbidden(summary.error) && (
        <Section>
          <ErrorState
            message={tr("The dashboard counts could not load. {0}", (summary.error as Error).message)}
            onRetry={() => summary.refetch()}
          />
        </Section>
      )}
      <SupplierAnswers />

      <Section
        title={tr("Recent trip cases")}
        description={tr("The five cases updated most recently.")}
        padded={false}
        actions={
          <Link
            href="/cases"
            className="inline-flex items-center gap-1 rounded-full border border-accent-border px-3.5 py-1.5 text-xs font-semibold text-accent-deep hover:bg-accent-tint"
          >
            {tr("View all cases")} <ArrowRight size={13} />
          </Link>
        }
      >
        {isForbidden(recent.error) ? (
          <NoAccess what={tr("trip cases")} />
        ) : isPhone ? (
          <RecentCards
            items={recent.data?.items}
            loading={recent.isLoading}
            error={recent.isError ? (recent.error as Error).message : null}
            onRetry={() => recent.refetch()}
          />
        ) : (
          <div className="p-5">
            <DataTable<TripCaseListItem>
              searchable={false}
              tableId="dashboard-recent-cases"
              data={recent.data?.items ?? []}
              columns={RECENT_COLUMNS}
              getRowId={(r) => r.id}
              loading={recent.isLoading}
              error={recent.isError ? (recent.error as Error).message : null}
              onRetry={() => recent.refetch()}
              pageSize={5}
              pageSizeOptions={[5]}
              emptyTitle={tr("No trip cases yet")}
              emptyDescription={tx(EMPTY_CASES)}
              onRowClick={(r) => router.push(`/cases/${encodeURIComponent(r.code)}`)}
              tableMinWidth={760}
            />
          </div>
        )}
      </Section>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <PipelineCard summary={s} loading={summary.isLoading} failed={summary.isError} />
        <MyQueue />
      </div>
    </>
  );
}

const RECENT_COLUMNS: ColumnDef<TripCaseListItem, unknown>[] = [
  { id: "code", header: "Case", accessorKey: "code", cell: ({ row }) => <Code>{row.original.code}</Code> },
  {
    id: "customer",
    header: "For",
    accessorFn: (r) => casePartyLabel(r),
    cell: ({ row }) => (
      <PersonCell
        className="md:max-w-[220px]"
        name={casePartyLabel(row.original)}
        sub={casePartySub(row.original)}
      />
    ),
  },
  {
    id: "trip",
    header: "Trip",
    accessorKey: "trip_title",
    cell: ({ row }) => <TripText item={row.original} />,
  },
  { id: "status", header: "Status", accessorKey: "status", cell: ({ row }) => <TripCaseStatusBadge status={row.original.status} /> },
  {
    id: "sales",
    header: "Sales",
    cell: ({ row }) => <span className="text-[13px] text-body">{salesOf(row.original)}</span>,
  },
  {
    id: "updated",
    header: "Updated",
    accessorKey: "updated_at",
    cell: ({ row }) => <DateText value={row.original.updated_at} relative className="text-[13px] text-muted" />,
  },
];

function salesOf(c: TripCaseListItem): string {
  return c.assignees.find((a) => a.role === "sales")?.name ?? "Unassigned";
}

function TripText({ item }: { item: TripCaseListItem }) {
  const tr = useT(MSG);
  return (
    <span className="text-[13px] text-body">
      {item.trip_title ?? tr("Custom trip")}
      {item.leg_count > 1 && <span className="text-muted"> {tr("+{0} legs", item.leg_count - 1)}</span>}
    </span>
  );
}

/** Phone layout of the recent cases (390 px): one card per case instead of a sideways-scrolling table. */
function RecentCards({
  items,
  loading,
  error,
  onRetry,
}: {
  items: TripCaseListItem[] | undefined;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const tr = useT(MSG);
  const tx = useTx();
  if (loading) {
    return (
      <ul aria-busy="true" aria-label={tr("Loading recent trip cases")}>
        {[0, 1, 2].map((i) => (
          <li key={i} className="flex flex-col gap-2 border-b border-line2 px-4 py-3 last:border-b-0">
            <SkeletonBar w="40%" h={12} />
            <SkeletonBar w="75%" h={12} />
          </li>
        ))}
      </ul>
    );
  }
  if (error) return <ErrorState message={tx(error)} onRetry={onRetry} />;
  if (!items?.length) {
    return (
      <EmptyState title={tr("No trip cases yet")} description={tx(EMPTY_CASES)} />
    );
  }
  return (
    <ul>
      {items.map((c) => (
        <li key={c.id} className="border-b border-line2 last:border-b-0">
          <Link href={`/cases/${encodeURIComponent(c.code)}`} className="flex flex-col gap-1.5 px-4 py-3 hover:bg-bg">
            <span className="flex items-center justify-between gap-2">
              <Code>{c.code}</Code>
              <TripCaseStatusBadge status={c.status} />
            </span>
            <span className="truncate text-[13.5px] font-semibold text-ink">{tx(casePartyLabel(c))}</span>
            <TripText item={c} />
            <span className="flex items-center justify-between gap-2 text-xs text-muted">
              <span className="truncate">{tx(salesOf(c))}</span>
              <DateText value={c.updated_at} relative />
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** The 13 statuses grouped into the 4 phases of the case stepper (Nghiệp S236 decision 4), plus Cancelled. */
function PipelineCard({ summary, loading, failed }: { summary: TripCaseSummary | undefined; loading: boolean; failed: boolean }) {
  const tr = useT(MSG);
  const tx = useTx();
  const phases = STATUS_PHASES.map((p) => ({
    ...p,
    count: summary ? p.statuses.reduce((t, k) => t + (summary.by_status[k] ?? 0), 0) : 0,
  }));
  const cancelled = summary?.by_status.cancelled ?? 0;
  const max = Math.max(1, ...phases.map((p) => p.count), cancelled);
  return (
    <Section
      title={tr("Pipeline by phase")}
      description={tr("All trip cases, grouped like the case stepper.")}
      actions={
        summary ? (
          <span className="text-right text-xs text-muted">
            <span className="font-semibold text-ink">{summary.open}</span> {tr("open ·")}
            <span className="font-semibold text-ink">{summary.mom_delta.created_this_month}</span> {tr("opened so far this month · {0} in all of last month", summary.mom_delta.created_last_month)}
          </span>
        ) : undefined
      }
    >
      {loading ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <SkeletonBar key={i} h={14} />
          ))}
        </div>
      ) : failed || !summary ? (
        <p className="text-[13px] text-muted">{tr("Counts unavailable.")}</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {[...phases, { key: "cancelled", label: tr("Cancelled"), statuses: ["cancelled" as TripCaseStatus], count: cancelled }].map((p) => (
            <li key={p.key}>
              <Link href={casesHref(p.statuses)} className="group grid grid-cols-[96px_minmax(0,1fr)_40px] items-center gap-3">
                <span className="text-[13px] text-body group-hover:text-ink">{tx(p.label)}</span>
                <span className="h-2 overflow-hidden rounded-full bg-line2">
                  <span
                    className={`block h-full rounded-full ${p.key === "cancelled" ? "bg-muted2" : "bg-accent"}`}
                    style={{ width: `${(p.count / max) * 100}%` }}
                  />
                </span>
                <span className="bk-tabular text-right text-[13px] font-semibold text-ink">{p.count}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/** "My queue" (spec §3): open cases assigned to the signed-in staff member, most recently touched first. */
function MyQueue() {
  const tr = useT(MSG);
  const tx = useTx();
  const q = useQuery({
    queryKey: ["trip-cases", "my-queue"],
    queryFn: () => api.tripCases.list({ assignee: "me", status: OPEN_STATUSES, sort: "-updated_at", limit: 5 }),
    retry: retryUnlessClientError,
  });
  return (
    <Section
      title={tr("My queue")}
      description={tr("Open cases assigned to you.")}
      padded={false}
      actions={
        <Link href="/cases?tab=mine" className="text-xs font-semibold text-accent-deep hover:underline">
          {tr("All my cases")}
        </Link>
      }
    >
      {q.isLoading ? (
        <div className="flex flex-col gap-3 p-5" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <SkeletonBar key={i} h={14} />
          ))}
        </div>
      ) : isForbidden(q.error) ? (
        <NoAccess what={tr("trip cases")} />
      ) : q.isError ? (
        <ErrorState message={tx((q.error as Error).message)} onRetry={() => q.refetch()} />
      ) : !q.data?.items.length ? (
        <EmptyState title={tr("Nothing assigned to you")} description={tr("Cases you are assigned to appear here.")} />
      ) : (
        <ul>
          {q.data.items.map((c) => (
            <li key={c.id} className="border-b border-line2 last:border-b-0">
              <Link
                href={`/cases/${encodeURIComponent(c.code)}`}
                className="flex flex-wrap items-center justify-between gap-2 px-5 py-3 hover:bg-bg"
              >
                <span className="flex min-w-0 items-center gap-2">
                  <Code>{c.code}</Code>
                  <span className="truncate text-[13px] text-ink">{tx(casePartyLabel(c))}</span>
                </span>
                <TripCaseStatusBadge status={c.status} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function CatalogDashboard() {
  const tr = useT(MSG);
  return (
    <Section title={tr("Catalog")} description={tr("Tours, destinations and countries are written in CIS and read here.")}>
      <div className="flex flex-col gap-3 text-[13px] text-body">
        <p>
          {tr("Your role works on catalog content. Booking shows that content read-only; the catalog pages open in wave 2. Edit tours, places and photos in the CIS admin.")}
        </p>
        <ManagedInCisLink label={tr("Open CIS admin")} />
      </div>
    </Section>
  );
}
