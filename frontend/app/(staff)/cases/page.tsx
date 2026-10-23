"use client";
// app/(staff)/cases/page.tsx — Trip case list (W1, UI spec §4.5; Figma TRIP CASES MANAGEMENT).
// GET /booking/trip-cases with server pagination; GET /booking/trip-cases/summary for the tab counts.
// Saved-filter tabs, the FilterBar and the page all live in the URL, so a filtered list is shareable.
// An explicit Status / Sales rep filter overrides the preset of the active tab.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowRight, Plus } from "lucide-react";
import { api } from "@/lib/api/client";
import type { TripCaseListItem, TripCaseStatus, TripCaseSummary } from "@/lib/api/types";
import { useOffsetPage, PAGE_SIZES } from "@/lib/useOffsetPage";
import {
  Code,
  DataTable,
  DateText,
  FilterBar,
  PageFrame,
  PersonCell,
  Pill,
  Section,
  TripCaseStatusBadge,
  useCan,
  useUrlFilters,
  formatDate,
} from "@/ui";
import { SavedTabs } from "@/app/(staff)/_parts/SavedTabs";
import { QueryError, isForbidden } from "./_parts/QueryError";
import { StatusMultiSelect, parseStatuses } from "./_parts/StatusMultiSelect";
import { sortStaff, useStaffDirectory } from "./_parts/StaffSelect";

const PRE_SALE: TripCaseStatus[] = ["new_inquiry", "discovery", "building", "proposal_sent"];
const CLOSED: TripCaseStatus[] = ["completed", "cancelled"];

/** Saved filters (spec §4.5): each sets the status / assignee query. */
const CASE_TABS = [
  { key: "all", label: "All" },
  { key: "inquiries", label: "Inquiries", status: ["new_inquiry"] as TripCaseStatus[] },
  { key: "mine", label: "Mine", assignee: "me" },
  { key: "unassigned", label: "Unassigned", assignee: "none" },
  { key: "active", label: "Active", status: PRE_SALE },
  { key: "closed", label: "Closed", status: CLOSED },
] as const;

/** Tab counts that the summary endpoint answers exactly; Mine / Unassigned have no exact count there. */
function tabCount(key: string, s: TripCaseSummary | undefined): number | undefined {
  if (!s) return undefined;
  const sum = (list: TripCaseStatus[]) => list.reduce((n, st) => n + (s.by_status[st] ?? 0), 0);
  if (key === "all") return Object.values(s.by_status).reduce((a, b) => a + b, 0);
  if (key === "inquiries") return s.new_inquiry;
  if (key === "active") return sum(PRE_SALE);
  if (key === "closed") return sum(CLOSED);
  return undefined;
}

export default function TripCasesPage() {
  const router = useRouter();
  const canWrite = useCan("trip_case.write");
  const { get, set } = useUrlFilters();
  const paging = useOffsetPage();
  const tab = CASE_TABS.find((t) => t.key === get("tab")) ?? CASE_TABS[0];
  const pickedStatus = parseStatuses(get("status"));
  const status = pickedStatus.length ? pickedStatus : "status" in tab ? [...tab.status] : undefined;
  const assignee = get("sales") || ("assignee" in tab ? tab.assignee : undefined);
  const filters = {
    q: get("q") || undefined,
    status,
    assignee,
    source: get("source") || undefined,
    created_from: get("from") || undefined,
    created_to: get("to") || undefined,
    sort: "-updated_at",
    limit: paging.limit,
    offset: paging.offset,
  };
  const list = useQuery({ queryKey: ["trip-cases", "list", filters], queryFn: () => api.tripCases.list(filters) });
  const summary = useQuery({ queryKey: ["trip-cases", "summary"], queryFn: () => api.tripCases.summary(), staleTime: 60_000 });
  const directory = useStaffDirectory();

  const forbidden = isForbidden(list.error);

  return (
    <PageFrame
      title="Trip cases"
      description="Every inquiry and booking in one list, from the first TripPlanner inquiry to the trip itself."
      actions={
        canWrite ? (
          <Link
            href="/cases/new"
            className="inline-flex items-center gap-1.5 rounded-full bg-accent px-4 py-2 text-[13px] font-semibold uppercase tracking-[0.06em] text-on-accent hover:bg-accent-deep"
          >
            <Plus size={15} /> New trip case
          </Link>
        ) : undefined
      }
    >
      {forbidden ? (
        <Section>
          <QueryError error={list.error} what="trip cases" />
        </Section>
      ) : (
        <>
          <SavedTabs tabs={CASE_TABS.map(({ key, label }) => ({ key, label, count: tabCount(key, summary.data) }))} />
          <Section>
            <div className="mb-4">
              <FilterBar
                search={{ placeholder: "Search case code, customer name or e-mail" }}
                selects={[
                  {
                    key: "sales",
                    label: "Sales rep",
                    allLabel: "All reps",
                    options: [
                      { value: "none", label: "Unassigned" },
                      ...sortStaff(directory.data?.items ?? []).map((s) => ({ value: s.cis_user_id, label: s.name ?? s.cis_user_id })),
                    ],
                  },
                  {
                    key: "source",
                    label: "Source",
                    allLabel: "All sources",
                    options: [
                      { value: "tripplanner", label: "TripPlanner" },
                      { value: "manual", label: "Manual" },
                      { value: "cis", label: "CIS trip plan" },
                      { value: "crm", label: "B2B agency (CRM)" },
                    ],
                  },
                ]}
                dateRange={{ fromKey: "from", toKey: "to", label: "Created" }}
                extraKeys={["status"]}
              >
                <StatusMultiSelect value={pickedStatus} onChange={(next) => set("status", next.length ? next.join(",") : null)} />
              </FilterBar>
            </div>
            {list.isError ? (
              <QueryError error={list.error} onRetry={() => list.refetch()} what="trip cases" />
            ) : (
              <DataTable<TripCaseListItem>
                searchable={false}
                tableId="trip-cases"
                data={list.data?.items ?? []}
                columns={CASE_COLUMNS}
                getRowId={(r) => r.id}
                loading={list.isLoading}
                pageSizeOptions={PAGE_SIZES}
                serverPagination={{
                  pageIndex: paging.pageIndex,
                  pageSize: paging.pageSize,
                  total: list.data?.total ?? 0,
                  onChange: paging.onChange,
                }}
                enableCsv
                csvFilename="trip-cases.csv"
                emptyTitle={tab.key === "all" && !get("q") && !pickedStatus.length ? "No trip cases yet" : "No trip cases match these filters"}
                emptyDescription={
                  tab.key === "all" && !get("q") && !pickedStatus.length
                    ? "Cases appear here when a TripPlanner inquiry arrives or when you create one."
                    : "Change the filters or the tab, or use Reset filters to see every case."
                }
                onRowClick={(r) => router.push(`/cases/${encodeURIComponent(r.code)}`)}
                tableMinWidth={1000}
              />
            )}
          </Section>
        </>
      )}
    </PageFrame>
  );
}

const CASE_COLUMNS: ColumnDef<TripCaseListItem, unknown>[] = [
  { id: "code", header: "Case", accessorKey: "code", cell: ({ row }) => <Code>{row.original.code}</Code> },
  {
    id: "customer",
    header: "Customer",
    accessorKey: "customer_name",
    cell: ({ row }) => (
      <PersonCell
        className="md:max-w-[190px]"
        name={row.original.customer_name}
        sub={row.original.customer_email ?? row.original.customer_phone}
        chips={
          <>
            {!row.original.customer_verified && (
              <Pill tone="accent" dot={false}>
                Unverified
              </Pill>
            )}
            {row.original.customer_needs_merge_review && (
              <Pill tone="warning" dot={false}>
                Merge review
              </Pill>
            )}
          </>
        }
      />
    ),
  },
  {
    id: "trip",
    header: "Trip",
    accessorKey: "trip_title",
    cell: ({ row }) => (
      <span className="block max-w-[280px] text-[13px] text-body md:max-w-[190px]">
        <span className="block truncate" title={row.original.trip_title ?? undefined}>
          {row.original.trip_title ?? "Custom trip"}
        </span>
        {row.original.leg_count > 1 && <span className="text-xs text-muted">+{row.original.leg_count - 1} more legs</span>}
        {row.original.source_system === "tripplanner" && row.original.leg_count <= 1 && (
          <span className="text-xs text-muted">From TripPlanner</span>
        )}
      </span>
    ),
  },
  {
    // AA-910: pax shares this cell (line 2, "20 days · 6 pax") so the list fits 1440 px; the accessor keeps the
    // start date first, so sorting is still by date, and the CSV still carries the pax.
    id: "dates",
    header: "Dates / pax",
    accessorFn: (r) => [r.start_date ?? "", r.duration_days ? `${r.duration_days} days` : "", r.pax ? `${r.pax} pax` : ""].filter(Boolean).join(" · "),
    cell: ({ row }) => {
      const r = row.original;
      const sub = [r.duration_days ? `${r.duration_days} days` : null, r.pax ? `${r.pax} pax` : null].filter(Boolean).join(" · ");
      if (!r.start_date) {
        return (
          <span className="text-[13px] text-muted">
            {r.duration_days ? `${r.duration_days} days, no date` : "—"}
            {r.pax ? <span className="block text-xs">{r.pax} pax</span> : null}
          </span>
        );
      }
      return (
        <span className="text-[13px] text-body">
          {formatDate(r.start_date)}
          {r.trip_end_date && <> – {formatDate(r.trip_end_date)}</>}
          {sub && <span className="block text-xs text-muted">{sub}</span>}
        </span>
      );
    },
  },
  {
    // AA-910: "Updated" sits under the status pill (as on /hotels and /services) so the list fits 1440 px. The
    // accessor starts with the status, so the header still sorts by status, and the CSV keeps the update time.
    id: "status",
    header: "Status",
    accessorFn: (r) => `${r.status} · ${r.updated_at}`,
    cell: ({ row }) => (
      <span className="flex flex-col items-start gap-1">
        <TripCaseStatusBadge status={row.original.status} />
        <span className="whitespace-nowrap text-xs text-muted">
          Updated <DateText value={row.original.updated_at} relative />
        </span>
      </span>
    ),
  },
  {
    id: "sales",
    header: "Sales",
    accessorFn: (r) => r.assignees.find((a) => a.role === "sales")?.name ?? "",
    cell: ({ row }) => {
      const sales = row.original.assignees.find((a) => a.role === "sales");
      if (!sales) return <span className="text-[13px] text-muted">Unassigned</span>;
      const name = sales.name ?? sales.cis_user_id;
      return (
        <span className="block text-[13px] text-body md:max-w-[100px] md:truncate" title={name}>
          {name}
        </span>
      );
    },
  },
  {
    id: "open",
    header: "",
    enableSorting: false,
    cell: ({ row }) => (
      <Link
        href={`/cases/${encodeURIComponent(row.original.code)}`}
        aria-label={`Open ${row.original.code}`}
        onClick={(e) => e.stopPropagation()}
        className="grid h-8 w-8 place-items-center rounded-md border border-line text-muted hover:border-accent-border hover:text-accent-deep"
      >
        <ArrowRight size={15} />
      </Link>
    ),
  },
];
