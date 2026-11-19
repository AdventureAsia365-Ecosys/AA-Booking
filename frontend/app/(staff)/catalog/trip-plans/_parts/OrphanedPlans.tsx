"use client";
// app/(staff)/catalog/trip-plans/_parts/OrphanedPlans.tsx — Trip plans, filter "Orphaned" (AA-986 L5, root ADR 0015
// decision 5, Q4). Booking trip plans whose tour left the CIS catalog (GET /booking/catalog/trip-plans?orphaned=true):
// the CIS key, why it left (from catalog.v_tour_retired when CIS has it), since when, and what is still attached
// (open trip cases, future departures, confirmed B2B holds). Everything is kept; agencies can no longer quote or hold
// it. No row link: an orphaned plan has no CIS content left to open.

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { tripPlansApi, type OrphanedPlanItem } from "@/lib/api/tripPlans";
import { CATALOG_STATE_META, catalogReasonLabel } from "@/lib/tourState";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { DataTable, DateText, Pill, useUrlFilters } from "@/ui";
import { SellableBadge } from "./shared";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./OrphanedPlans.messages";

/** `filters`: the page's FilterBar, drawn in the table toolbar (AA-996 audit slice 1). */
export function OrphanedPlansTable({ filters }: { filters?: React.ReactNode }) {
  const t = useT(MSG);
  const tx = useTx();
  const { get } = useUrlFilters();
  const paging = useOffsetPage();
  const query = { q: get("q").trim() || undefined, limit: paging.limit, offset: paging.offset };
  const list = useQuery({ queryKey: ["trip-plans", "orphaned", query], queryFn: () => tripPlansApi.orphaned(query) });

  const columns: ColumnDef<OrphanedPlanItem, unknown>[] = [
    {
      id: "tour",
      header: t("CIS tour key"),
      accessorFn: (r) => r.source_id,
      cell: ({ row }) => <span className="break-all font-mono text-[12px] text-ink">{row.original.source_id}</span>,
    },
    {
      id: "state",
      header: t("Catalog"),
      accessorFn: (r) => r.catalog.state,
      cell: ({ row }) => {
        const meta = CATALOG_STATE_META[row.original.catalog.state];
        return (
          <span className="flex min-w-0 flex-col gap-1">
            <Pill tone={meta.tone} title={tx(meta.help)}>
              {tx(meta.label)}
            </Pill>
            <span className="text-xs text-muted">{tx(catalogReasonLabel(row.original.catalog.reason))}</span>
          </span>
        );
      },
    },
    {
      id: "since",
      header: t("Since"),
      accessorFn: (r) => r.catalog.changed_at ?? "",
      cell: ({ row }) =>
        row.original.catalog.changed_at ? (
          <DateText value={row.original.catalog.changed_at} />
        ) : (
          <span className="text-[13px] text-muted2">—</span>
        ),
    },
    { id: "status", header: t("Status"), accessorFn: (r) => (r.sellable ? 1 : 0), cell: ({ row }) => <SellableBadge sellable={row.original.sellable} /> },
    {
      id: "attached",
      header: t("Still attached"),
      accessorFn: (r) => r.open_cases + r.future_departures + r.confirmed_holds,
      cell: ({ row }) => (
        <span className="bk-tabular text-[13px] text-body">
          {t("{0} open case{1} · {2} future departure{3} · {4} confirmed hold{5}", row.original.open_cases, row.original.open_cases === 1 ? "" : "s", row.original.future_departures, row.original.future_departures === 1 ? "" : "s", row.original.confirmed_holds, row.original.confirmed_holds === 1 ? "" : "s")}
        </span>
      ),
    },
  ];

  return (
    <>
      <DataTable<OrphanedPlanItem>
        filters={filters}
        searchable={false}
        tableId="trip-plans-orphaned"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.source_id}
        loading={list.isLoading}
        error={list.isError ? (list.error as Error).message : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        enableCsv
        csvFilename="trip-plans-orphaned.csv"
        emptyTitle={query.q ? t("No orphaned plan with this key") : t("No orphaned trip plans")}
        emptyDescription={t("A trip plan shows here when CIS stops serving its tour (deleted or trashed). A paused tour is temporary and is not listed.")}
        tableMinWidth={860}
      />
      <p className="mt-3 text-xs text-muted">
        {t("Agencies can no longer quote or hold these tours. Confirmed bookings, holds and trip cases are kept: cancelling is a staff decision. Search takes the exact CIS key.")}
      </p>
    </>
  );
}
