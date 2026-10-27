"use client";
// app/(staff)/catalog/trip-plans/_parts/TripPlansView.tsx — Catalog → Trip Plans (W2, UI spec §3 / §6.3 / §7;
// Figma TRIP TEMPLATES (05), "Create Trip Plan" becomes "Make sellable" on an existing CIS tour). AA-679, S237 D4.
// Every CIS catalog tour (GET /booking/catalog/trip-plans, a live join of catalog.v_tour and booking.trip_plans)
// with its Booking state: Sellable / Not for sale, trip style and the sales price hint. Tour content is read-only
// ("From CIS"). Filter options and counts come from the server (/facets), pagination is server-side.

import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CalendarDays, Eye, EyeOff, Globe, Store, Unlink } from "lucide-react";
import { tripPlansApi, type TripPlanListItem } from "@/lib/api/tripPlans";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { CisPhoto, DataTable, FilterBar, FromCisTag, KpiTile, ManagedInCisLink, Money, PageFrame, Pill, Section, useCan, useUrlFilters } from "@/ui";
import { OrphanedPlansTable } from "./OrphanedPlans";
import { SellableBadge, priceBasisLabel } from "./shared";
import { useSellableToggle } from "./useSellableToggle";
import { RowMenu } from "@/ui/booking/RowMenu";

const SORTS = [
  { value: "title", label: "Title A–Z" },
  { value: "country", label: "Country" },
  { value: "duration_days", label: "Shortest first" },
  { value: "-duration_days", label: "Longest first" },
  { value: "-updated_at", label: "Recently updated in CIS" },
];

export function TripPlansView() {
  const router = useRouter();
  const { get } = useUrlFilters();
  const paging = useOffsetPage();
  const canPrice = useCan("pricing.read");
  const sellableToggle = useSellableToggle();

  // AA-986 L5: "show=orphaned" switches the table to the plans whose tour left the CIS catalog.
  const orphanedView = get("show") === "orphaned";
  const sellableParam = get("sellable");
  const query = {
    q: get("q") || undefined,
    country: get("country") || undefined,
    sellable: sellableParam === "yes" ? true : sellableParam === "no" ? false : undefined,
    trip_style: get("style") || undefined,
    sort: get("sort") || undefined,
    limit: paging.limit,
    offset: paging.offset,
  };
  const list = useQuery({
    queryKey: ["trip-plans", "list", query],
    queryFn: () => tripPlansApi.list(query),
    enabled: !orphanedView,
  });
  const facets = useQuery({ queryKey: ["trip-plans", "facets"], queryFn: tripPlansApi.facets, staleTime: 60_000 });

  const columns: ColumnDef<TripPlanListItem, unknown>[] = [
    {
      id: "tour",
      header: "Tour",
      accessorFn: (r) => r.title,
      cell: ({ row }) => (
        <span className="flex min-w-0 items-center gap-3">
          <CisPhoto path={row.original.cover_photo_path} alt="" className="h-10 w-14 shrink-0 rounded-md" />
          <span className="min-w-0">
            <span className="block max-w-[320px] truncate text-[13.5px] font-medium text-ink" title={row.original.title}>
              {row.original.title}
            </span>
            <span className="block text-xs text-muted">{row.original.sku ?? "No SKU"}</span>
          </span>
        </span>
      ),
    },
    { id: "country", header: "Country", accessorFn: (r) => r.country ?? "", cell: ({ row }) => <span className="text-[13px] text-body">{row.original.country ?? "—"}</span> },
    {
      id: "days",
      header: "Days",
      accessorFn: (r) => r.duration_days ?? 0,
      cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.duration_days ?? "—"}</span>,
    },
    { id: "status", header: "Status", accessorFn: (r) => (r.sellable ? 1 : 0), cell: ({ row }) => <SellableBadge sellable={row.original.sellable} /> },
    {
      id: "style",
      header: "Trip style",
      accessorFn: (r) => r.trip_style ?? "",
      cell: ({ row }) => <span className="text-[13px] text-body">{row.original.trip_style ?? "—"}</span>,
    },
    ...(canPrice
      ? [
          {
            id: "hint",
            header: "Price hint",
            accessorFn: (r: TripPlanListItem) => Number(r.price_hint?.amount ?? 0),
            cell: ({ row }: { row: { original: TripPlanListItem } }) =>
              row.original.price_hint ? (
                <span className="text-[13px]">
                  <span className="text-muted">from </span>
                  <Money amount={row.original.price_hint.amount} currency={row.original.price_hint.currency} />
                  <span className="block text-xs text-muted">{priceBasisLabel(row.original.price_hint.basis)}</span>
                </span>
              ) : (
                <span className="text-[13px] text-muted2">—</span>
              ),
          } satisfies ColumnDef<TripPlanListItem, unknown>,
        ]
      : []),
    {
      id: "actions",
      header: "",
      enableSorting: false,
      cell: ({ row }) => (
        <RowMenu
          label={`Actions for ${row.original.title}`}
          items={[
            { label: "View details", icon: <Eye size={14} />, onSelect: () => router.push(`/catalog/trip-plans/${encodeURIComponent(row.original.source_id)}`) },
            ...(sellableToggle.canWrite
              ? [
                  row.original.sellable
                    ? { label: "Make not sellable", icon: <EyeOff size={14} />, danger: true, divider: true, onSelect: () => sellableToggle.ask(row.original) }
                    : { label: "Make sellable", icon: <Store size={14} />, divider: true, onSelect: () => sellableToggle.ask(row.original) },
                ]
              : []),
          ]}
        />
      ),
    },
  ];

  const f = facets.data;
  const filtered = !!(query.q || query.country || query.sellable !== undefined || query.trip_style);
  return (
    <PageFrame
      title="Trip plans"
      description="CIS tours, and which of them Booking sells. Tour content is read-only here: edit it in CIS."
      actions={<ManagedInCisLink path="/admin/master-content" label="Open CIS content" />}
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile label="CIS tours" value={f?.total} icon={<CalendarDays size={16} />} hint={<FromCisTag />} />
        <KpiTile
          label="Sellable"
          value={f?.sellable}
          icon={<Store size={16} />}
          hint={f ? `${f.total - f.sellable} not for sale` : undefined}
          hintTone="neutral"
        />
        <KpiTile label="Countries" value={f?.countries.length} icon={<Globe size={16} />} />
        <KpiTile
          label="Orphaned plans"
          value={f?.orphaned}
          icon={<Unlink size={16} />}
          hint={f ? (f.orphaned ? "Tour left the CIS catalog" : "None") : undefined}
          hintTone={f?.orphaned ? "warning" : "neutral"}
        />
      </div>
      <Section>
        {orphanedView ? (
          <>
            <div className="mb-4">
              <FilterBar
                search={{ placeholder: "Exact CIS source id" }}
                selects={[
                  {
                    key: "show",
                    label: "Show",
                    allLabel: "CIS catalog tours",
                    options: [{ value: "orphaned", label: `Orphaned plans (${f?.orphaned ?? 0})` }],
                    width: 190, // the facet counts load after the page (CLS)
                  },
                ]}
              />
            </div>
            <OrphanedPlansTable />
          </>
        ) : (
          <>
            <div className="mb-4">
              <FilterBar
                search={{ placeholder: "Search title, SKU or CIS source id" }}
                selects={[
                  {
                    key: "show",
                    label: "Show",
                    allLabel: "CIS catalog tours",
                    options: [{ value: "orphaned", label: `Orphaned plans (${f?.orphaned ?? 0})` }],
                    width: 190, // the facet counts load after the page (CLS)
                  },
                  {
                    key: "country",
                    label: "Country",
                    allLabel: "All countries",
                    options: (f?.countries ?? []).map((c) => ({ value: c.country, label: `${c.country} (${c.tours})` })),
                    width: 180,
                  },
                  {
                    key: "sellable",
                    label: "Status",
                    allLabel: "Any status",
                    options: [
                      { value: "yes", label: "Sellable" },
                      { value: "no", label: "Not for sale" },
                    ],
                  },
                  {
                    key: "style",
                    label: "Trip style",
                    allLabel: "Any style",
                    options: (f?.trip_styles ?? []).map((s) => ({ value: s, label: s })),
                    width: 180,
                  },
                  { key: "sort", label: "Sort", allLabel: "Title A–Z", options: SORTS.slice(1) },
                ]}
              />
            </div>
            <DataTable<TripPlanListItem>
              searchable={false}
              tableId="trip-plans"
              data={list.data?.items ?? []}
              columns={columns}
              getRowId={(r) => r.source_id}
              loading={list.isLoading}
              error={list.isError ? (list.error as Error).message : null}
              onRetry={() => list.refetch()}
              pageSizeOptions={PAGE_SIZES}
              serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
              enableCsv
              csvFilename="trip-plans.csv"
              emptyTitle={filtered ? "No trip plans match" : "No CIS tours yet"}
              emptyDescription={
                filtered
                  ? "Change the search or the filters, or reset them to see every CIS tour."
                  : "Tours appear here as soon as CIS publishes them to the catalog."
              }
              onRowClick={(r) => router.push(`/catalog/trip-plans/${encodeURIComponent(r.source_id)}`)}
              tableMinWidth={canPrice ? 920 : 780}
            />
            <p className="mt-3 text-xs text-muted">
              <Pill tone="neutral" dot={false}>
                Live
              </Pill>{" "}
              Read from the CIS catalog on every visit: a tour CIS unpublishes leaves this list and shows under Orphaned plans.
            </p>
          </>
        )}
      </Section>
      {sellableToggle.dialogs}
    </PageFrame>
  );
}
