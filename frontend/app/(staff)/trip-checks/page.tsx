"use client";
// app/(staff)/trip-checks/page.tsx — Trip checks (AA-682 S2, bk-027; S252 design §6, UI spec §3, Figma TRIP CHECKS (03)).
// Every pre-departure check across the sold, unfinished trip cases (GET /booking/trip-checks, ops.read), soonest due
// first, paged on the server; filters in the URL (search, status, owner, check, due). A row opens the check drawer
// (status, due date, owner, note, emergency contact on a B2C trip). The three standard checks are added by the system
// when a trip is confirmed; custom checks are added on the case's Checks tab. The trip case status moves by itself with
// the travel dates (bk_case_lifecycle, daily); the last runs are listed under the table.
// AA-996 (Figma TRIP CHECKS, audit item 8): status tabs with counts above the list (Open / Overdue / Done / Not
// applicable / All; the counts come with the list, under the same owner / check / search filters) and, in the Trip
// case cell, how far that trip is from ready ("3/9": its done and not-applicable checks of all its checks).

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import { TRIP_CHECK_CODE_LABEL, tripChecksApi, type LifecycleRun, type TripCheck, type TripCheckCounts } from "@/lib/api/tripChecks";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  DataTable,
  DateText,
  FilterBar,
  PageFrame,
  Pill,
  Section,
  TripCaseStatusBadge,
  TripCheckStatusBadge,
  useCan,
  useUrlFilters,
} from "@/ui";
import { checkProgress } from "@/ui/booking/caseFacts";
import { SavedTabs } from "@/app/(staff)/_parts/SavedTabs";
import { CheckDrawer, checkTitle, isOverdue } from "./_parts/CheckDrawer";

const STATUS_TABS = [
  { key: "open", label: "Open" },
  { key: "overdue", label: "Overdue" },
  { key: "done", label: "Done" },
  { key: "not_applicable", label: "Not applicable" },
  { key: "all", label: "All" },
] as const;
type StatusTab = (typeof STATUS_TABS)[number]["key"];

/** The tab the URL shows: Overdue = open checks with the "overdue" due filter; otherwise the status key. */
function activeTab(status: string, due: string): StatusTab {
  if ((status === "" || status === "open") && due === "overdue") return "overdue";
  return (STATUS_TABS.find((t) => t.key === status)?.key ?? "open") as StatusTab;
}

/** "3/9" and a thin bar: the trip's resolved checks of all its checks (/trip-checks rows only). */
function TripProgress({ checks }: { checks: TripCheck["trip_case"]["checks"] }) {
  const p = checkProgress(checks);
  if (!p) return null;
  return (
    <span className="flex items-center gap-1.5" title={p.label} data-testid="trip-progress">
      <span className="block h-1.5 w-14 overflow-hidden rounded-full bg-line2" aria-hidden="true">
        <span className={`block h-full rounded-full ${p.pct === 100 ? "bg-success" : "bg-accent"}`} style={{ width: `${p.pct}%` }} />
      </span>
      <span className="bk-tabular text-[11px] text-muted">
        <span className="sr-only">{p.label}</span>
        <span aria-hidden="true">
          {p.resolved}/{p.total}
        </span>
      </span>
    </span>
  );
}

const DUE_WINDOWS: Record<string, number> = { overdue: -1, "7d": 7, "14d": 14, "30d": 30 };

function dueBefore(key: string): string | undefined {
  const days = DUE_WINDOWS[key];
  if (days === undefined) return undefined;
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

// AA-996 (Figma audit, deviation 8): the list fits 1440 px with its Status column in view (it was cut at the card edge):
// the free-text cells (check, customer, owner) truncate with the full text in the tooltip.
const COLUMNS: ColumnDef<TripCheck, unknown>[] = [
  {
    id: "task",
    header: "Check",
    accessorFn: (r) => checkTitle(r),
    cell: ({ row }) => (
      <span className="flex min-w-0 flex-col md:max-w-[170px]">
        <span className="truncate text-[13.5px] font-medium text-ink" title={checkTitle(row.original)}>
          {checkTitle(row.original)}
        </span>
        <span className="text-xs text-muted">{row.original.standard ? "Standard" : "Custom"}</span>
      </span>
    ),
  },
  {
    id: "case",
    header: "Trip case",
    accessorFn: (r) => r.trip_case.code,
    cell: ({ row }) => {
      const t = row.original.trip_case;
      return (
        <span className="flex min-w-0 flex-col gap-1">
          <Link
            href={`/cases/${encodeURIComponent(t.code)}?tab=checks`}
            onClick={(e) => e.stopPropagation()}
            className="text-[13px] font-medium text-accent-deep hover:underline"
          >
            {t.code}
          </Link>
          <TripCaseStatusBadge status={t.status} />
          <TripProgress checks={t.checks} />
        </span>
      );
    },
  },
  {
    id: "customer",
    header: "Customer",
    accessorFn: (r) => (r.trip_case.channel === "b2b" ? "B2B agency" : (r.trip_case.customer_name ?? "")),
    enableSorting: false,
    cell: ({ row }) => {
      const t = row.original.trip_case;
      if (t.channel === "b2b") return <Pill tone="accent" dot={false}>B2B agency</Pill>;
      return (
        <span className="block max-w-[180px] truncate text-[13px] text-body md:max-w-[110px]" title={t.customer_name ?? undefined}>
          {t.customer_name ?? "—"}
        </span>
      );
    },
  },
  {
    id: "start",
    header: "Trip starts",
    accessorFn: (r) => r.trip_case.start_date ?? "",
    cell: ({ row }) =>
      row.original.trip_case.start_date ? <DateText value={row.original.trip_case.start_date} className="whitespace-nowrap text-[13px]" /> : <span className="text-muted">—</span>,
  },
  {
    id: "due",
    header: "Due",
    accessorFn: (r) => r.due_date ?? "",
    cell: ({ row }) =>
      row.original.due_date ? (
        <DateText value={row.original.due_date} className={`whitespace-nowrap text-[13px] ${isOverdue(row.original) ? "font-semibold text-danger" : ""}`} />
      ) : (
        <span className="text-muted">—</span>
      ),
  },
  {
    id: "owner",
    header: "Owner",
    accessorFn: (r) => (r.assignee ? (r.assignee.name ?? r.assignee.id) : "Anyone"),
    enableSorting: false,
    cell: ({ row }) => {
      const name = row.original.assignee ? (row.original.assignee.name ?? row.original.assignee.id) : "Anyone";
      return (
        <span className="block max-w-[180px] truncate text-[13px] text-body md:max-w-[100px]" title={name}>
          {name}
        </span>
      );
    },
  },
  {
    id: "status",
    header: "Status",
    accessorFn: (r) => r.status,
    cell: ({ row }) => <TripCheckStatusBadge status={row.original.status} overdue={isOverdue(row.original)} />,
  },
];

export default function TripChecksPage() {
  const canRead = useCan("ops.read");
  const { get, setMany } = useUrlFilters();
  const paging = useOffsetPage();
  const [openId, setOpenId] = useState<string | null>(null);
  const status = get("status") || "open";
  const tab = activeTab(get("status"), get("due"));
  const pickTab = (key: string) => {
    // Overdue is the open list with the "overdue" due filter; leaving it clears that filter (other due windows stay).
    const due = get("due") === "overdue" ? null : get("due") || null;
    if (key === "overdue") setMany({ status: null, due: "overdue" });
    else setMany({ status: key === "open" ? null : key, due });
  };
  const query = {
    status,
    q: get("q") || undefined,
    assignee: get("owner") || undefined,
    code: get("check") || undefined,
    due_before: dueBefore(get("due")),
    limit: paging.limit,
    offset: paging.offset,
  };
  const list = useQuery({ queryKey: ["trip-checks", "list", query], queryFn: () => tripChecksApi.list(query), enabled: canRead });
  const open = list.data?.items.find((c) => c.id === openId) ?? null;
  // The counts arrive with the list; keep the last ones while a new page / filter loads (no flicker).
  const [counts, setCounts] = useState<TripCheckCounts | null>(null);
  const fresh = list.data?.counts ?? null;
  if (fresh && JSON.stringify(fresh) !== JSON.stringify(counts)) setCounts(fresh);

  return (
    <PageFrame
      title="Trip checks"
      description="Pre-departure checks across sold trips: re-check bookings, emergency contact, insurance and your own checks. Soonest due first."
    >
      {canRead && (
        <SavedTabs
          active={tab}
          onSelect={pickTab}
          tabs={STATUS_TABS.map(({ key, label }) => ({ key, label, count: counts?.[key], countPending: !counts && list.isPending }))}
        />
      )}
      <Section>
        <DataTable<TripCheck>
          filters={
            <FilterBar
              search={{ placeholder: "Search case code or check" }}
              selects={[
                {
                  key: "status",
                  label: "Status",
                  allLabel: "Open",
                  options: [
                    { value: "done", label: "Done" },
                    { value: "not_applicable", label: "Not applicable" },
                    { value: "all", label: "All statuses" },
                  ],
                  width: 150,
                },
                {
                  key: "due",
                  label: "Due",
                  allLabel: "Any time",
                  options: [
                    { value: "overdue", label: "Overdue" },
                    { value: "7d", label: "Within 7 days" },
                    { value: "14d", label: "Within 14 days" },
                    { value: "30d", label: "Within 30 days" },
                  ],
                  width: 150,
                },
                {
                  key: "owner",
                  label: "Owner",
                  allLabel: "Anyone",
                  options: [
                    { value: "me", label: "Mine" },
                    { value: "none", label: "No owner" },
                  ],
                  width: 130,
                },
                {
                  key: "check",
                  label: "Check",
                  allLabel: "All checks",
                  options: Object.entries(TRIP_CHECK_CODE_LABEL).map(([value, label]) => ({ value, label })),
                  width: 170,
                },
              ]}
            />
          }
          searchable={false}
          tableId="trip-checks"
          enableCsv
          csvFilename="trip-checks.csv"
          data={list.data?.items ?? []}
          columns={COLUMNS}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          error={list.isError ? (list.error instanceof ApiError ? list.error.message : "Could not load the checks") : null}
          onRetry={() => list.refetch()}
          pageSizeOptions={PAGE_SIZES}
          serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
          emptyTitle={canRead ? (status === "open" ? "No open check" : "No check matches") : "Trip checks need the operations role"}
          emptyDescription="Checks appear when a trip is confirmed. Change the filters to widen the list."
          onRowClick={(r) => setOpenId(r.id)}
          tableMinWidth={980}
        />
      </Section>
      {canRead && <LifecycleRuns />}
      <CheckDrawer check={open} open={!!open} onClose={() => setOpenId(null)} showCase />
    </PageFrame>
  );
}

/** The latest daily runs: what the case lifecycle moved (or, in a DRY RUN, would move) and the reminders it wrote. */
function LifecycleRuns() {
  const runs = useQuery({ queryKey: ["trip-checks", "runs"], queryFn: () => tripChecksApi.runs(6), staleTime: 60_000 });
  const items = runs.data?.items ?? [];
  return (
    <Section
      title="Automatic updates"
      description={
        runs.data
          ? `Once a day the trip status follows the travel dates (Pre-departure, On trip, Completed)${
              runs.data.scheduled_mode === "dry_run" ? "; the daily run is still a dry run (it only reports)" : ""
            }, and owners are reminded of checks due soon.`
          : "Once a day the trip status follows the travel dates and owners are reminded of checks due soon."
      }
    >
      {runs.isLoading ? (
        <p className="text-[13px] text-muted">Loading…</p>
      ) : runs.isError ? (
        <p className="text-[13px] text-danger">Could not load the runs.</p>
      ) : items.length === 0 ? (
        <p className="text-[13px] text-muted">No run yet.</p>
      ) : (
        <ul className="flex flex-col divide-y divide-line2 text-[13px]">
          {items.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <span className="min-w-0 text-body">{runSummary(r)}</span>
              <span className="flex items-center gap-2 text-xs text-muted">
                <Pill tone={r.status === "succeeded" ? "success" : r.status === "failed" ? "danger" : "neutral"} dot={false}>
                  {r.status}
                </Pill>
                <DateText value={r.finished_at ?? r.created_at} withTime />
              </span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function runSummary(r: LifecycleRun): string {
  const res = (r.result ?? {}) as Record<string, unknown>;
  if (r.kind === "bk_ops_reminders") {
    return `Reminders: ${Number(res.checks_due_notified ?? 0)} check(s) due, ${Number(res.supplier_waiting_notified ?? 0)} supplier answer(s) waiting, ${Number(res.payments_notified ?? 0)} payment(s) due or overdue`;
  }
  const moves = (res.moves ?? {}) as Record<string, number>;
  const total = Object.values(moves).reduce((a, b) => a + Number(b || 0), 0);
  const dry = res.mode === "dry_run";
  return `Trip status${dry ? " (dry run)" : ""}: ${total} move(s)${dry ? " would be made" : ""}, ${Number(res.blocked ?? 0)} blocked${
    res.scoped ? " · one case" : ""
  }`;
}
