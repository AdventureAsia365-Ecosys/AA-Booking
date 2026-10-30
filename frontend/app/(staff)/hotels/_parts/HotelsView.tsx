"use client";
// app/(staff)/hotels/_parts/HotelsView.tsx — Hotels (W2, AA-823; UI spec §3 / §6.3; Figma HOTEL MANAGEMENT 07):
// GET /booking/hotels with server-side search, status / country / stars filters and pagination; a row opens the hotel
// drawer (?hotel=HTL-…, so the link can be shared). Admin and contracting add, edit and deactivate (supply.write);
// sales read. Hotels sit at CIS places; their customer-facing text and photos are CIS content (Q17).
// S254 Figma audit #19 (HOTEL MANAGEMENT): two views of the same page of results, `?view=table` for the table and the
// photo grid by default. A card shows the first supplier photo (or the neutral tile), the photo count, the place chip,
// name, code, stars, supplier, status and when it was last updated, and the same row menu as the table. Filters,
// paging and CSV are shared: switching the view keeps them.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, ChevronLeft, ChevronRight, Eye, ImageIcon, MapPin, Pencil, Plus, Power } from "lucide-react";
import { supplyApi, type Hotel, type HotelDetail } from "@/lib/api/supply";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  Button,
  Code,
  DataTable,
  DateText,
  EmptyState,
  ErrorState,
  ExportCsvButton,
  FilterBar,
  PageFrame,
  RowMenu,
  Section,
  Select,
  SkeletonBar,
  Tabs,
  useUrlFilters,
  type FilterSelect,
  type RowMenuItem,
} from "@/ui";
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
  const view = get("view") === "table" ? "table" : "grid";
  const selects: FilterSelect[] = [
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
      width: 170, // the countries load after the page: a fixed width keeps the bar from shifting (CLS)
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
  ];
  const emptyTitle = filtered ? "No hotels match" : "No hotels yet";
  const emptyDescription = filtered
    ? "Change the search or the filters, or reset them to see every hotel."
    : status.canWrite
      ? "Add the first hotel with Add hotel. It needs a supplier with the Hotel category."
      : "Contracting adds hotels here; they appear once created.";
  return (
    <PageFrame
      title="Hotels"
      description="Hotels Booking contracts through its suppliers, at places that come from CIS. Facts, rooms and supplier photos here; descriptions and published photos are CIS content."
      actions={
        <>
          <span data-testid="hotels-view">
            <Tabs
              tabs={[
                { key: "grid", label: "Grid" },
                { key: "table", label: "Table" },
              ]}
              active={view}
              onChange={(k) => set("view", k === "table" ? "table" : null)}
            />
          </span>
          {status.canWrite && (
            <Button variant="primary" onClick={() => setForm({})}>
              <Plus size={15} /> Add hotel
            </Button>
          )}
        </>
      }
    >
      {view === "grid" ? (
        <Section>
          <div className="mb-4">
            <FilterBar
              search={SEARCH}
              selects={selects}
              tools={<ExportCsvButton rows={list.data?.items ?? []} columns={HOTEL_CSV} filename="hotels.csv" disabled={list.isLoading} />}
            />
          </div>
          {list.isLoading ? (
            <div className={GRID} aria-busy="true">
              {[0, 1, 2, 3].map((i) => (
                <SkeletonBar key={i} h={300} r={12} />
              ))}
            </div>
          ) : list.isError ? (
            <ErrorState message={(list.error as Error).message} onRetry={() => list.refetch()} />
          ) : (list.data?.items.length ?? 0) === 0 ? (
            <EmptyState title={emptyTitle} description={emptyDescription} />
          ) : (
            <ul className={GRID} data-testid="hotel-grid">
              {list.data!.items.map((h) => (
                <HotelCard
                  key={h.id}
                  hotel={h}
                  onOpen={() => set("hotel", h.code)}
                  menu={<RowMenu items={menuFor(h)} label={`Actions for ${h.name}`} />}
                />
              ))}
            </ul>
          )}
          <GridPager
            pageIndex={paging.pageIndex}
            pageSize={paging.pageSize}
            total={list.data?.total ?? 0}
            onChange={paging.onChange}
          />
        </Section>
      ) : (
      <Section>
        <div className="bk-dt-sticky-end">
          <DataTable<Hotel>
            filters={<FilterBar search={SEARCH} selects={selects} />}
            searchable={false}
            tableId="hotels"
            enableCsv
            csvFilename="hotels.csv"
            data={list.data?.items ?? []}
            columns={columns}
            getRowId={(r) => r.id}
            loading={list.isLoading}
            error={list.isError ? (list.error as Error).message : null}
            onRetry={() => list.refetch()}
            pageSizeOptions={PAGE_SIZES}
            serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
            emptyTitle={emptyTitle}
            emptyDescription={emptyDescription}
            onRowClick={(r) => set("hotel", r.code)}
            tableMinWidth={860}
          />
        </div>
      </Section>
      )}
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

// AA-910: the table fits the list frame at 1400 and 1440 px with 20 px to spare (S250: name 190 px, place 130 px). Text cells have a max width (a table cell grows to its
// content, so `truncate` alone never cuts; the CIS marker of the place is in the column header), the hotel style sits under the name, "Updated" sits under the status pill,
// and the row menu column stays pinned to the right when a narrower window makes the table scroll inside its frame.
const COLUMNS: ColumnDef<Hotel, unknown>[] = [
  {
    id: "hotel",
    header: "Hotel",
    accessorFn: (r) => r.name,
    cell: ({ row }) => (
      <span className="flex min-w-0 max-w-[190px] flex-col">
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
        <span className="flex min-w-0 items-center text-[13px] text-body md:max-w-[130px]">
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
        {/* AA-910: the date never breaks, but it may drop under "Updated" (a row older than 7 days shows the full date, much wider than "3 d ago"). */}
        <span className="text-xs text-muted">
          Updated <DateText value={row.original.updated_at} relative className="whitespace-nowrap" />
        </span>
      </span>
    ),
  },
];

const SEARCH = { placeholder: "Search hotel, HTL- code, address or supplier" };
const GRID = "grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4";

/** "Hoi An, Vietnam" (the CIS place), the country alone, or null. */
function placeText(h: Hotel): string | null {
  if (h.destination) return `${h.destination.name}, ${h.country_name ?? h.destination.country}`;
  return h.country_name ?? null;
}

const HOTEL_CSV: { header: string; value: (h: Hotel) => unknown }[] = [
  { header: "Code", value: (h) => h.code },
  { header: "Hotel", value: (h) => h.name },
  { header: "Style", value: (h) => h.hotel_style ?? "" },
  { header: "Stars", value: (h) => h.star_rating ?? "" },
  { header: "Place", value: (h) => placeText(h) ?? "" },
  { header: "Supplier", value: (h) => h.supplier_name },
  { header: "Rooms", value: (h) => h.room_type_count },
  { header: "Rates in force", value: (h) => h.rates_in_force },
  { header: "Photos", value: (h) => h.photo_count ?? 0 },
  { header: "Status", value: (h) => h.status },
  { header: "Updated", value: (h) => h.updated_at },
];

/** The first supplier photo (a presigned URL), or the same neutral tile as a CIS photo that is missing. */
function HotelCover({ url, name }: { url: string | null | undefined; name: string }) {
  const [failed, setFailed] = useState<string | null>(null);
  if (!url || failed === url) {
    return (
      <span
        data-hotel-photo="none"
        className="grid aspect-[16/10] w-full place-items-center bg-panel bg-gradient-to-br from-panel to-line/60 text-muted2"
        role="img"
        aria-label={`${name}: no photo`}
      >
        <span className="select-none text-[11px] font-semibold uppercase tracking-[0.08em]">No photo</span>
      </span>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- a short-lived presigned URL of a private bucket
    <img
      src={url}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed(url)}
      className="aspect-[16/10] w-full bg-panel object-cover"
    />
  );
}

function HotelCard({ hotel: h, onOpen, menu }: { hotel: Hotel; onOpen: () => void; menu: React.ReactNode }) {
  const place = placeText(h);
  const photos = h.photo_count ?? 0;
  return (
    <li className="flex min-w-0 flex-col overflow-hidden rounded-lg border border-line bg-card" data-testid="hotel-card">
      {/* The cover repeats the name button for the pointer; keyboard and screen readers use the name below. */}
      <a
        href={`?hotel=${encodeURIComponent(h.code)}`}
        onClick={(e) => {
          e.preventDefault();
          onOpen();
        }}
        className="relative block"
        tabIndex={-1}
        aria-hidden="true"
      >
        <HotelCover url={h.cover_photo_url} name={h.name} />
        <span className="bk-tabular absolute bottom-2 left-2 inline-flex items-center gap-1 rounded-full bg-black/55 px-2 py-0.5 text-[11.5px] font-medium text-white">
          <ImageIcon size={12} /> {photos === 1 ? "1 photo" : `${photos} photos`}
        </span>
        {place && (
          <span className="absolute right-2 top-2 inline-flex max-w-[75%] items-center gap-1 rounded-full bg-card/90 px-2 py-0.5 text-[11.5px] font-medium text-ink">
            <MapPin size={12} className="shrink-0 text-accent-deep" />
            <span className="truncate">{place}</span>
          </span>
        )}
      </a>
      <div className="flex min-w-0 flex-1 flex-col gap-2 p-4">
        <div className="flex items-start justify-between gap-2">
          <span className="flex min-w-0 flex-col">
            <a
              href={`?hotel=${encodeURIComponent(h.code)}`}
              onClick={(e) => {
                e.preventDefault();
                onOpen();
              }}
              className="truncate text-[15px] font-semibold text-ink hover:underline"
              title={h.name}
            >
              {h.name}
            </a>
            <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted">
              <Code>{h.code}</Code>
              {h.hotel_style && (
                <span className="truncate" title={h.hotel_style}>
                  · {h.hotel_style}
                </span>
              )}
            </span>
          </span>
          <span className="shrink-0">{menu}</span>
        </div>
        <div className="flex items-center justify-between gap-2 text-[13px]">
          <Stars value={h.star_rating} />
          <span className="min-w-0 truncate text-body" title={h.supplier_name}>
            {h.supplier_name}
          </span>
        </div>
        <p className="text-xs text-muted">
          {h.room_type_count === 1 ? "1 room type" : `${h.room_type_count} room types`} · {h.rates_in_force} rates in force
        </p>
        <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-line2 pt-2">
          <SupplierStatusBadge status={h.status} />
          <span className="text-xs text-muted">
            Updated <DateText value={h.updated_at} relative className="whitespace-nowrap" />
          </span>
        </div>
      </div>
    </li>
  );
}

/** The grid's pager: the same URL state (page, size) as the table's server pagination. */
function GridPager({
  pageIndex,
  pageSize,
  total,
  onChange,
}: {
  pageIndex: number;
  pageSize: number;
  total: number;
  onChange: (pageIndex: number, pageSize: number) => void;
}) {
  if (total === 0) return null;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = pageIndex * pageSize + 1;
  const to = Math.min(total, (pageIndex + 1) * pageSize);
  return (
    <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-[13px] text-muted" data-testid="hotel-grid-pager">
      <span className="bk-tabular">
        {from}–{to} of {total}
      </span>
      <span className="flex items-center gap-2">
        <Select
          size="sm"
          aria-label="Hotels per page"
          value={String(pageSize)}
          onChange={(e) => onChange(0, Number(e.target.value))}
          options={PAGE_SIZES.map((n) => ({ value: String(n), label: `${n} per page` }))}
        />
        <Button size="sm" variant="ghost" ariaLabel="Previous page" disabled={pageIndex === 0} onClick={() => onChange(pageIndex - 1, pageSize)}>
          <ChevronLeft size={14} />
        </Button>
        <span className="bk-tabular">
          {pageIndex + 1} / {pages}
        </span>
        <Button size="sm" variant="ghost" ariaLabel="Next page" disabled={pageIndex + 1 >= pages} onClick={() => onChange(pageIndex + 1, pageSize)}>
          <ChevronRight size={14} />
        </Button>
      </span>
    </div>
  );
}
