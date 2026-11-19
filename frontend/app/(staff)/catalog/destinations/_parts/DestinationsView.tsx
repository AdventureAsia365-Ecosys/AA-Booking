"use client";
// app/(staff)/catalog/destinations/_parts/DestinationsView.tsx — Catalog → Destinations (AA-996; UI spec §3 / §7;
// Figma DESTINATION MANAGEMENT, -1, DESTINATION DETAILS, ADD / EDIT / DELETE DESTINATION, module 06). Every CIS place
// (catalog.v_destination, read live) with its Booking state (bk-029): KPI tiles over the whole catalog, server-side
// search / country / status filters and paging, the destination drawer (`?place=<id>`, view; `&edit=1` edit), and the
// row menu (View details, Edit destination, Deactivate / Activate). "Add destination" picks an existing CIS place:
// places are created in CIS. Every staff role reads; only an admin changes the Booking state.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCircle2, Eye, Globe, MapPin, Pencil, Plus, Power } from "lucide-react";
import { geoApi, type CustomerVisibility, type DestinationItem, type GeoStatus } from "@/lib/api/geo";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  Button,
  CisPhoto,
  DataTable,
  FilterBar,
  FromCisTag,
  KpiTile,
  ManagedInCisLink,
  Modal,
  PageFrame,
  Section,
  useCan,
  useUrlFilters,
  RowMenu,
} from "@/ui";
import { DestinationPicker } from "@/app/(staff)/suppliers/_parts/DestinationPicker";
import { CIS_PLACES_PATH, CustomerVisibilityBadge, DestinationDrawer, GeoStatusBadge, HIDDEN_REASON_LABEL, coords } from "../../_parts/GeoDrawers";
import { useGeoStatus } from "../../_parts/useGeoStatus";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./DestinationsView.messages";

const SORTS = [
  { value: "country", label: "Country" },
  { value: "-photos", label: "Most photos" },
];

export function DestinationsView() {
  const t = useT(MSG);
  const tx = useTx();
  const { get, setMany } = useUrlFilters();
  const paging = useOffsetPage();
  const canWrite = useCan("catalog.admin");
  const status = useGeoStatus();
  // AA-1128 B2: the Country filter lists the countries that have CIS places (the same source as the "Countries covered"
  // tile), not Booking's sold-country list: a destination is always reachable by its own country.
  const countries = useQuery({ queryKey: ["geo", "countries", {}], queryFn: () => geoApi.countries(), staleTime: 60_000 });
  const [adding, setAdding] = useState(false);

  const query = {
    q: get("q") || undefined,
    country: get("country") || undefined,
    status: (get("status") || undefined) as GeoStatus | undefined,
    visibility: (get("visibility") || undefined) as CustomerVisibility | undefined,
    sort: get("sort") || undefined,
    limit: paging.limit,
    offset: paging.offset,
  };
  const list = useQuery({ queryKey: ["geo", "destinations", query], queryFn: () => geoApi.destinations(query) });
  const open = get("place");
  const openPlace = (id: string, edit = false) => setMany({ place: id, edit: edit ? "1" : null, page: get("page") || null });
  const closePlace = () => setMany({ place: null, edit: null, page: get("page") || null });

  const s = list.data?.summary;
  const columns: ColumnDef<DestinationItem, unknown>[] = [
    {
      id: "name",
      header: t("Name"),
      accessorFn: (r) => r.name,
      cell: ({ row }) => (
        <span className="flex min-w-0 items-center gap-3">
          <CisPhoto path={row.original.cover_photo_path} alt="" className="h-10 w-14 shrink-0 rounded-md" />
          <span className="min-w-0">
            <span className="block max-w-[260px] truncate text-[13.5px] font-medium text-ink" title={row.original.name}>
              {row.original.name}
            </span>
            {row.original.status_reason && <span className="block max-w-[260px] truncate text-xs text-muted">{tx(row.original.status_reason)}</span>}
            {row.original.hidden_reason && (
              <span className="mt-0.5 block" title={tx(HIDDEN_REASON_LABEL[row.original.hidden_reason])}>
                <CustomerVisibilityBadge reason={tx(row.original.hidden_reason)} />
              </span>
            )}
          </span>
        </span>
      ),
    },
    { id: "country", header: t("Country"), accessorFn: (r) => r.country ?? "", cell: ({ row }) => <span className="text-[13px] text-body">{row.original.country ?? "—"}</span> },
    {
      id: "coords",
      header: t("Coordinates"),
      meta: { compact: "drawer" }, // AA-996: with Media, in the details drawer below 1200 px
      enableSorting: false,
      accessorFn: (r) => coords(r.lat, r.lng) ?? "",
      cell: ({ row }) => <span className="bk-tabular whitespace-nowrap text-[12.5px] text-muted">{coords(row.original.lat, row.original.lng) ?? "—"}</span>,
    },
    { id: "status", header: t("Booking status"), accessorKey: "status", cell: ({ row }) => <GeoStatusBadge status={row.original.status} /> },
    { id: "photos", header: t("Media"), meta: { compact: "drawer" }, accessorKey: "photo_count", cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.photo_count} {t("files")}</span> },
    {
      id: "actions",
      header: t("Actions"), // AA-996 audit: the column header was blank
      enableSorting: false,
      cell: ({ row }) => {
        const d = row.original;
        return (
          <RowMenu
            label={t("Actions for {0}", d.name)}
            items={[
              { label: t("View details"), icon: <Eye size={14} />, onSelect: () => openPlace(d.destination_id) },
              ...(canWrite
                ? [
                    { label: t("Edit destination"), icon: <Pencil size={14} />, onSelect: () => openPlace(d.destination_id, true) },
                    d.status === "active"
                      ? { label: t("Deactivate"), icon: <Power size={14} />, danger: true, divider: true, onSelect: () => status.ask("destination", { id: d.destination_id, name: d.name }, false) }
                      : { label: t("Activate"), icon: <CheckCircle2 size={14} />, divider: true, onSelect: () => status.ask("destination", { id: d.destination_id, name: d.name }, true) },
                  ]
                : []),
            ]}
          />
        );
      },
    },
  ];

  const filtered = !!(query.q || query.country || query.status || query.visibility);
  return (
    <PageFrame
      title={t("Destinations")}
      description={t("Places from CIS and whether Booking sells them. Names, coordinates and photos are edited in CIS.")}
      actions={
        <>
          <ManagedInCisLink path={CIS_PLACES_PATH} label={t("Open CIS places")} />
          {canWrite && (
            <Button variant="primary" onClick={() => setAdding(true)}>
              <Plus size={15} /> {t("Add destination")}
            </Button>
          )}
        </>
      }
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile label={t("Total destinations")} value={s?.total} icon={<MapPin size={16} />} hint={<FromCisTag />} />
        <KpiTile label={t("Active in Booking")} value={s?.active} icon={<CheckCircle2 size={16} />} />
        <KpiTile label={t("Inactive")} value={s?.inactive} icon={<Power size={16} />} hintTone="neutral" hint={s?.inactive ? t("Not sold by Booking") : undefined} />
        <KpiTile label={t("Countries covered")} value={s?.countries} icon={<Globe size={16} />} />
      </div>
      <Section>
        <DataTable<DestinationItem>
          filters={
            <FilterBar
              search={{ placeholder: t("Search destinations") }}
              selects={[
                {
                  key: "country",
                  label: t("Country"),
                  allLabel: t("All countries"),
                  options: (countries.data?.items ?? [])
                    .filter((c) => c.destinations > 0)
                    .sort((a, b) => a.name.localeCompare(b.name))
                    .map((c) => ({ value: c.country_code, label: c.name })),
                  width: 170,
                },
                {
                  key: "status",
                  label: t("Status"),
                  allLabel: t("All statuses"),
                  options: [
                    { value: "active", label: t("Active") },
                    { value: "inactive", label: t("Inactive") },
                  ],
                },
                {
                  key: "visibility",
                  label: t("Customer maps"),
                  allLabel: t("Shown and hidden"),
                  options: [
                    { value: "shown", label: t("Shown to customers") },
                    { value: "hidden", label: t("Hidden from customers") },
                  ],
                },
                { key: "sort", label: t("Sort"), allLabel: t("Name A–Z"), options: SORTS },
              ]}
            />
          }
          searchable={false}
          tableId="destinations"
          data={list.data?.items ?? []}
          columns={columns}
          getRowId={(r) => r.destination_id}
          loading={list.isLoading}
          error={list.isError ? (list.error as Error).message : null}
          onRetry={() => list.refetch()}
          pageSizeOptions={PAGE_SIZES}
          serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
          enableCsv
          csvFilename="destinations.csv"
          emptyTitle={filtered ? t("No destinations match") : t("No CIS places yet")}
          emptyDescription={filtered ? t("Change the search or the filters.") : t("Places appear here as soon as CIS publishes them to the catalog.")}
          onRowClick={(r) => openPlace(r.destination_id)}
          tableMinWidth={860}
        />
      </Section>
      {open && <DestinationDrawer key={`${open}-${get("edit")}`} id={open} startEditing={get("edit") === "1"} onClose={closePlace} />}
      {adding && (
        <AddDestinationModal
          onClose={() => setAdding(false)}
          onPick={(id) => {
            setAdding(false);
            openPlace(id, true);
          }}
        />
      )}
      {status.dialogs}
    </PageFrame>
  );
}

/** Figma ADD DESTINATION: places are created in CIS, so "Add" picks an existing CIS place and opens its Booking state. */
function AddDestinationModal({ onClose, onPick }: { onClose: () => void; onPick: (id: string) => void }) {
  const t = useT(MSG);
  return (
    <Modal open onClose={onClose} title={t("Add destination")} width={520}>
      <div className="flex flex-col gap-3">
        <p className="text-[13px] text-body">
          {t("Pick a CIS place to set its Booking status and notes. A place that is not in the list yet is created in CIS (Places), then appears here.")}
        </p>
        <DestinationPicker value={null} country="" onChange={(p) => p && onPick(p.destination_id)} />
        <ManagedInCisLink path={CIS_PLACES_PATH} label={t("Request a new place in CIS")} />
      </div>
    </Modal>
  );
}
