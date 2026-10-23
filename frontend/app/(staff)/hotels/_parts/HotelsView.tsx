"use client";
// app/(staff)/hotels/_parts/HotelsView.tsx — Hotels (W2, AA-823; UI spec §3 / §6.3; Figma HOTEL MANAGEMENT 07):
// GET /booking/hotels with server-side search, status / country / stars filters and pagination; a row opens the hotel
// drawer (?hotel=HTL-…, so the link can be shared). Admin and contracting add, edit and deactivate (supply.write);
// sales read. Hotels sit at CIS places; their customer-facing text and photos are CIS content (Q17).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, Eye, Pencil, Plus, Power } from "lucide-react";
import { supplyApi, type Hotel, type HotelDetail } from "@/lib/api/supply";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { Button, Code, DataTable, DateText, FilterBar, PageFrame, Section, useUrlFilters } from "@/ui";
import { RowMenu, type RowMenuItem } from "@/ui/booking/RowMenu";
import { useCountries } from "@/app/(staff)/cases/_parts/CountryPicker";
import { Stars } from "@/app/(staff)/_parts/supply";
import { useRecordStatus } from "@/app/(staff)/_parts/useRecordStatus";
import { SupplierStatusBadge } from "@/app/(staff)/suppliers/_parts/shared";
import { HotelDrawer } from "./HotelDrawer";
import { HotelFormDrawer } from "./HotelFormDrawer";

export function HotelsView() {
  const { get, set } = useUrlFilters();
  const paging = useOffsetPage();
  const countries = useCountries();
  const status = useRecordStatus();
  const [form, setForm] = useState<{ hotel?: HotelDetail } | null>(null);
  const [editRef, setEditRef] = useState<string | null>(null);

  const filters = {
    q: get("q") || undefined,
    status: get("status") || undefined,
    country: get("country") || undefined,
    star_rating: get("stars") || undefined,
    sort: get("sort") || undefined,
  };
  const list = useQuery({
    queryKey: ["hotels", "list", filters, paging.limit, paging.offset],
    queryFn: () => supplyApi.hotels({ ...filters, limit: paging.limit, offset: paging.offset }),
  });
  const editing = useQuery({ queryKey: ["hotel", editRef], queryFn: () => supplyApi.hotel(editRef as string), enabled: !!editRef });
  const open = get("hotel");

  function menuFor(row: Hotel): RowMenuItem[] {
    const items: RowMenuItem[] = [{ label: "View hotel", icon: <Eye size={15} />, onSelect: () => set("hotel", row.code) }];
    if (status.canWrite) {
      items.push({ label: "Edit hotel", icon: <Pencil size={15} />, onSelect: () => setEditRef(row.code) });
      items.push(
        row.status === "active"
          ? { label: "Deactivate", icon: <Ban size={15} />, onSelect: () => status.ask("hotel", { code: row.code, name: row.name }, false), danger: true, divider: true }
          : { label: "Activate", icon: <Power size={15} />, onSelect: () => status.ask("hotel", { code: row.code, name: row.name }, true), divider: true },
      );
    }
    return items;
  }

  const columns: ColumnDef<Hotel, unknown>[] = [
    ...COLUMNS,
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => <RowMenu items={menuFor(row.original)} label={`Actions for ${row.original.name}`} />,
    },
  ];
  const filtered = !!(filters.q || filters.status || filters.country || filters.star_rating);
  return (
    <PageFrame
      title="Hotels"
      description="Hotels Booking contracts through its suppliers, at places that come from CIS. Facts and rooms here; descriptions and photos are CIS content."
      actions={
        status.canWrite && (
          <Button variant="primary" onClick={() => setForm({})}>
            <Plus size={15} /> Add hotel
          </Button>
        )
      }
    >
      <Section>
        <div className="mb-4">
          <FilterBar
            search={{ placeholder: "Search hotel, HTL- code, address or supplier" }}
            selects={[
              {
                key: "status",
                label: "Status",
                allLabel: "All statuses",
                options: [
                  { value: "active", label: "Active" },
                  { value: "inactive", label: "Inactive" },
                ],
              },
              {
                key: "country",
                label: "Country",
                allLabel: "All countries",
                options: (countries.data?.items ?? []).map((c) => ({ value: c.iso2, label: c.name })),
              },
              {
                key: "stars",
                label: "Stars",
                allLabel: "Any rating",
                options: [5, 4, 3, 2, 1].map((n) => ({ value: String(n), label: `${n} ${n === 1 ? "star" : "stars"}` })),
              },
              {
                key: "sort",
                label: "Sort",
                allLabel: "Name A–Z",
                options: [
                  { value: "-updated_at", label: "Recently updated" },
                  { value: "-created_at", label: "Newest first" },
                  { value: "-star_rating", label: "Most stars" },
                ],
              },
            ]}
          />
        </div>
        <div className="bk-dt-sticky-end">
          <DataTable<Hotel>
            searchable={false}
            tableId="hotels"
            data={list.data?.items ?? []}
            columns={columns}
            getRowId={(r) => r.id}
            loading={list.isLoading}
            error={list.isError ? (list.error as Error).message : null}
            onRetry={() => list.refetch()}
            pageSizeOptions={PAGE_SIZES}
            serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
            emptyTitle={filtered ? "No hotels match" : "No hotels yet"}
            emptyDescription={
              filtered
                ? "Change the search or the filters, or reset them to see every hotel."
                : status.canWrite
                  ? "Add the first hotel with Add hotel. It needs a supplier with the Hotel category."
                  : "Contracting adds hotels here; they appear once created."
            }
            onRowClick={(r) => set("hotel", r.code)}
            tableMinWidth={860}
          />
        </div>
      </Section>
      {open && <HotelDrawer hotelRef={open} onClose={() => set("hotel", null)} />}
      {form && (
        <HotelFormDrawer
          hotel={form.hotel}
          onClose={() => setForm(null)}
          onSaved={(h) => {
            if (!form.hotel) set("hotel", h.code);
          }}
        />
      )}
      {editRef && editing.data && <HotelFormDrawer hotel={editing.data} onClose={() => setEditRef(null)} />}
      {status.dialogs}
    </PageFrame>
  );
}

// AA-910: the table fits the 1084 px list frame at 1440 px. Text cells have a max width (a table cell grows to its
// content, so `truncate` alone never cuts; the CIS marker of the place is in the column header), the hotel style sits under the name, "Updated" sits under the status pill,
// and the row menu column stays pinned to the right when a narrower window makes the table scroll inside its frame.
const COLUMNS: ColumnDef<Hotel, unknown>[] = [
  {
    id: "hotel",
    header: "Hotel",
    accessorFn: (r) => r.name,
    cell: ({ row }) => (
      <span className="flex min-w-0 max-w-[200px] flex-col">
        <span className="truncate text-[13.5px] font-medium text-ink" title={row.original.name}>
          {row.original.name}
        </span>
        <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted">
          <Code>{row.original.code}</Code>
          {row.original.hotel_style && (
            <span className="truncate" title={row.original.hotel_style}>
              · {row.original.hotel_style}
            </span>
          )}
        </span>
      </span>
    ),
  },
  { id: "stars", header: "Stars", accessorKey: "star_rating", cell: ({ row }) => <Stars value={row.original.star_rating} /> },
  {
    id: "place",
    header: "Place (CIS)",
    accessorFn: (r) => r.destination?.name ?? r.country_name ?? "",
    cell: ({ row }) => {
      const r = row.original;
      const text = r.destination ? `${r.destination.name}, ${r.country_name ?? r.destination.country}` : (r.country_name ?? "—");
      return (
        <span className="flex min-w-0 items-center text-[13px] text-body md:max-w-[150px]">
          <span className="md:truncate" title={text}>
            {text}
          </span>
        </span>
      );
    },
  },
  {
    id: "supplier",
    header: "Supplier",
    accessorKey: "supplier_name",
    cell: ({ row }) => (
      <span className="block break-words text-[13px] text-body md:max-w-[120px] md:truncate" title={row.original.supplier_name}>
        {row.original.supplier_name}
      </span>
    ),
  },
  { id: "rooms", header: "Rooms", accessorKey: "room_type_count", cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.room_type_count}</span> },
  { id: "rates", header: "Rates", accessorKey: "rates_in_force", cell: ({ row }) => <span className="bk-tabular text-[13px]" title="Rates in force">{row.original.rates_in_force}</span> },
  {
    id: "status",
    header: "Status",
    accessorKey: "status",
    cell: ({ row }) => (
      <span className="flex flex-col items-end gap-1 md:items-start">
        <SupplierStatusBadge status={row.original.status} />
        <span className="whitespace-nowrap text-xs text-muted">
          Updated <DateText value={row.original.updated_at} relative />
        </span>
      </span>
    ),
  },
];
