"use client";
// app/(staff)/catalog/trip-plans/_parts/TripPlansView.tsx — Catalog → Trip Plans (W2, UI spec §3 / §6.3 / §7;
// Figma TRIP TEMPLATES (05), "Create Trip Plan" becomes "Make sellable" on an existing CIS tour). AA-679, S237 D4.
// Every CIS catalog tour (GET /booking/catalog/trip-plans, a live join of catalog.v_tour and booking.trip_plans)
// with its Booking state: Sellable / Not for sale, trip style and the sales price hint. Tour content is read-only
// ("From CIS"). Filter options and counts come from the server (/facets), pagination is server-side.
// AA-996 (Figma audit item 2): the list opens sellable first (the API default), rows show "N days / N-1 nights", the
// tour's places as the subtitle (else SKU, else a short CIS id), the from price of the price matrix (else the price
// hint), and the row menu has "Edit commercial" (trip_plan.write) and "Edit in CIS ↗" (CIS content roles).

import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CalendarDays, ExternalLink, Eye, EyeOff, Globe, Pencil, Store, Unlink } from "lucide-react";
import { tripPlansApi, type TripPlanListItem } from "@/lib/api/tripPlans";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { CIS_ADMIN_URL } from "@/lib/config";
import { daysNights, planSubtitle } from "@/ui/booking/tripPlans";
import { CisPhoto, DataTable, FilterBar, FromCisTag, KpiTile, ManagedInCisLink, Money, PageFrame, Pill, Section, useCan, useStaff, useUrlFilters } from "@/ui";
import { OrphanedPlansTable } from "./OrphanedPlans";
import { CIS_EDITOR_ROLES, SellableBadge, priceBasisLabel } from "./shared";
import { useSellableToggle } from "./useSellableToggle";
import { RowMenu } from "@/ui/booking/RowMenu";

// The API opens sellable first (sort=sellable, then title): that is the "all" option of the Sort select.
const SORTS = [
  { value: "sellable", label: "Sellable first" },
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
  const canEditCommercial = useCan("trip_plan.write");
  const me = useStaff();
  const canEditInCis = CIS_EDITOR_ROLES.has(me.role);
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
            <span className="block max-w-[236px] truncate text-[13.5px] font-medium text-ink" title={row.original.title}>
              {row.original.title}
            </span>
            <span className="block max-w-[236px] truncate text-xs text-muted" title={planSubtitle(row.original)}>
              {planSubtitle(row.original)}
            </span>
          </span>
        </span>
      ),
    },
    { id: "country", header: "Country", accessorFn: (r) => r.country ?? "", cell: ({ row }) => <span className="text-[13px] text-body">{row.original.country ?? "—"}</span> },
    {
      id: "days",
      header: "Duration",
      accessorFn: (r) => r.duration_days ?? 0,
      // "15 days / 14 nights" on two lines, so the column stays narrow (the table must fit 1400 without scrolling).
      cell: ({ row }) => {
        const [days, nights] = (daysNights(row.original.duration_days) ?? "—").split(" / ");
        return (
          <span className="bk-tabular whitespace-nowrap text-[13px]" title={daysNights(row.original.duration_days) ?? undefined}>
            {days}
            {nights && <span className="block text-xs text-muted">{nights}</span>}
          </span>
        );
      },
    },
    { id: "status", header: "Status", accessorFn: (r) => (r.sellable ? 1 : 0), cell: ({ row }) => <SellableBadge sellable={row.original.sellable} /> },
    {
      id: "style",
      header: "Trip style",
      meta: { compact: "drawer" }, // AA-996: with the price hint, in the details drawer below 1200 px
      accessorFn: (r) => r.trip_style ?? "",
      cell: ({ row }) => (
        <span className="block max-w-[120px] truncate text-[13px] text-body" title={row.original.trip_style ?? undefined}>
          {row.original.trip_style ?? "—"}
        </span>
      ),
    },
    ...(canPrice
      ? [
          {
            id: "hint",
            header: "Price from",
            meta: { compact: "drawer" as const },
            accessorFn: (r: TripPlanListItem) => Number(r.from_price?.amount ?? r.price_hint?.amount ?? 0),
            cell: ({ row }: { row: { original: TripPlanListItem } }) =>
              row.original.from_price ? (
                <span className="text-[13px]" title="Per person, from the price matrix">
                  <Money amount={row.original.from_price.amount} currency={row.original.from_price.currency} />
                  <span className="block text-xs text-muted">per person</span>
                </span>
              ) : row.original.price_hint ? (
                <span className="text-[13px]" title="Price hint (no price matrix yet)">
                  <Money amount={row.original.price_hint.amount} currency={row.original.price_hint.currency} />
                  <span className="block text-xs text-muted">{priceBasisLabel(row.original.price_hint.basis)} · hint</span>
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
            ...(canEditCommercial
              ? [
                  {
                    label: "Edit commercial",
                    icon: <Pencil size={14} />,
                    onSelect: () => router.push(`/catalog/trip-plans/${encodeURIComponent(row.original.source_id)}/commercial`),
                  },
                ]
              : []),
            ...(canEditInCis
              ? [
                  {
                    label: "Edit in CIS ↗",
                    icon: <ExternalLink size={14} />,
                    onSelect: () =>
                      window.open(`${CIS_ADMIN_URL}/admin/master-content?tour=${encodeURIComponent(row.original.tour_id)}`, "_blank", "noopener,noreferrer"),
                  },
                ]
              : []),
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
            <OrphanedPlansTable
              filters={
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
              }
            />
          </>
        ) : (
          <>
            <DataTable<TripPlanListItem>
              filters={
                <FilterBar
                  search={{ placeholder: "Search title, SKU or CIS source id" }}
                  selects={[
                    {
                      key: "show",
                      label: "Show",
                      allLabel: "CIS catalog tours",
                      options: [{ value: "orphaned", label: `Orphaned plans (${f?.orphaned ?? 0})` }],
                      // The facet counts load after the page (CLS). AA-996: the widths let every filter fit one row at 1440.
                      width: 160,
                    },
                    {
                      key: "country",
                      label: "Country",
                      allLabel: "All countries",
                      options: (f?.countries ?? []).map((c) => ({ value: c.country, label: `${c.country} (${c.tours})` })),
                      width: 140,
                    },
                    {
                      key: "sellable",
                      label: "Status",
                      allLabel: "Any status",
                      options: [
                        { value: "yes", label: "Sellable" },
                        { value: "no", label: "Not for sale" },
                      ],
                      width: 120,
                    },
                    {
                      key: "style",
                      label: "Style",
                      allLabel: "Any trip style",
                      options: (f?.trip_styles ?? []).map((s) => ({ value: s, label: s })),
                      width: 150,
                    },
                    { key: "sort", label: "Sort", allLabel: "Sellable first", options: SORTS.slice(1), width: 150 },
                  ]}
                />
              }
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
