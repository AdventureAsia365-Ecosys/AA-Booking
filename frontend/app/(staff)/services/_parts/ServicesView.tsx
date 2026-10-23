"use client";
// app/(staff)/services/_parts/ServicesView.tsx — Services (W2, AA-823; UI spec §3 / §6.3; Figma ACTIVITY MANAGEMENT
// 07): activities, transfers, guides, entrances and meals Booking buys, at CIS places. GET /booking/services with
// server-side search, type / status / country filters and pagination; a row opens /services/{SVC-code}. Admin and
// contracting add, edit and deactivate (supply.write); sales read.

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, Eye, Plus, Power } from "lucide-react";
import { SERVICE_TYPES, supplyApi, type Service } from "@/lib/api/supply";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { Button, Code, DataTable, DateText, FilterBar, PageFrame, Section, useUrlFilters } from "@/ui";
import { RowMenu, type RowMenuItem } from "@/ui/booking/RowMenu";
import { useCountries } from "@/app/(staff)/cases/_parts/CountryPicker";
import { SERVICE_TYPE_LABEL, UNIT_LABEL } from "@/app/(staff)/_parts/supply";
import { useRecordStatus } from "@/app/(staff)/_parts/useRecordStatus";
import { SupplierStatusBadge } from "@/app/(staff)/suppliers/_parts/shared";
import { ServiceFormDrawer } from "./ServiceFormDrawer";

export const serviceHref = (s: Pick<Service, "code">) => `/services/${encodeURIComponent(s.code)}`;

export function ServicesView() {
  const router = useRouter();
  const { get } = useUrlFilters();
  const paging = useOffsetPage();
  const countries = useCountries();
  const status = useRecordStatus();
  const [adding, setAdding] = useState(false);

  const filters = {
    q: get("q") || undefined,
    type: get("type") || undefined,
    status: get("status") || undefined,
    country: get("country") || undefined,
    sort: get("sort") || undefined,
  };
  const list = useQuery({
    queryKey: ["services", "list", filters, paging.limit, paging.offset],
    queryFn: () => supplyApi.services({ ...filters, limit: paging.limit, offset: paging.offset }),
  });

  function menuFor(row: Service): RowMenuItem[] {
    const items: RowMenuItem[] = [{ label: "View service", icon: <Eye size={15} />, onSelect: () => router.push(serviceHref(row)) }];
    if (status.canWrite) {
      items.push(
        row.status === "active"
          ? { label: "Deactivate", icon: <Ban size={15} />, onSelect: () => status.ask("service", { code: row.code, name: row.title }, false), danger: true, divider: true }
          : { label: "Activate", icon: <Power size={15} />, onSelect: () => status.ask("service", { code: row.code, name: row.title }, true), divider: true },
      );
    }
    return items;
  }

  const columns: ColumnDef<Service, unknown>[] = [
    ...COLUMNS,
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => <RowMenu items={menuFor(row.original)} label={`Actions for ${row.original.title}`} />,
    },
  ];
  const filtered = !!(filters.q || filters.type || filters.status || filters.country);
  return (
    <PageFrame
      title="Services"
      description="Activities, transfers, guides, entrances and meals Booking buys, at CIS places. Customer-facing text stays in CIS."
      actions={
        status.canWrite && (
          <Button variant="primary" onClick={() => setAdding(true)}>
            <Plus size={15} /> Add service
          </Button>
        )
      }
    >
      <Section>
        <div className="mb-4">
          <FilterBar
            search={{ placeholder: "Search title, SVC- code or supplier" }}
            selects={[
              { key: "type", label: "Type", allLabel: "All types", options: SERVICE_TYPES.map((t) => ({ value: t, label: SERVICE_TYPE_LABEL[t] })) },
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
                key: "sort",
                label: "Sort",
                allLabel: "Title A–Z",
                options: [
                  { value: "-updated_at", label: "Recently updated" },
                  { value: "-created_at", label: "Newest first" },
                  { value: "service_type", label: "Type" },
                ],
              },
            ]}
          />
        </div>
        <div className="bk-dt-sticky-end">
          <DataTable<Service>
            searchable={false}
            tableId="services"
            data={list.data?.items ?? []}
            columns={columns}
            getRowId={(r) => r.id}
            loading={list.isLoading}
            error={list.isError ? (list.error as Error).message : null}
            onRetry={() => list.refetch()}
            pageSizeOptions={PAGE_SIZES}
            serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
            emptyTitle={filtered ? "No services match" : "No services yet"}
            emptyDescription={
              filtered
                ? "Change the search or the filters, or reset them to see every service."
                : status.canWrite
                  ? "Add the first service with Add service. Its supplier needs the matching category."
                  : "Contracting adds services here; they appear once created."
            }
            onRowClick={(r) => router.push(serviceHref(r))}
            tableMinWidth={860}
          />
        </div>
      </Section>
      {adding && <ServiceFormDrawer onClose={() => setAdding(false)} onSaved={(s) => router.push(serviceHref(s))} />}
      {status.dialogs}
    </PageFrame>
  );
}

// AA-910: the table fits the 1084 px list frame at 1440 px. Text cells have a max width (a table cell grows to its
// content, so `truncate` alone never cuts; the CIS marker of the place is in the column header), the pricing unit sits under the type, "Updated" sits under the status pill,
// and the row menu column stays pinned to the right when a narrower window makes the table scroll inside its frame.
const COLUMNS: ColumnDef<Service, unknown>[] = [
  {
    id: "service",
    header: "Service",
    accessorFn: (r) => r.title,
    cell: ({ row }) => (
      <span className="flex min-w-0 max-w-[200px] flex-col">
        <span className="truncate text-[13.5px] font-medium text-ink" title={row.original.title}>
          {row.original.title}
        </span>
        <Code>{row.original.code}</Code>
      </span>
    ),
  },
  {
    id: "type",
    header: "Type",
    accessorKey: "service_type",
    cell: ({ row }) => (
      <span className="flex flex-col whitespace-nowrap text-[13px] text-body">
        {SERVICE_TYPE_LABEL[row.original.service_type] ?? row.original.service_type}
        <span className="text-xs text-muted">{UNIT_LABEL[row.original.unit]}</span>
      </span>
    ),
  },
  {
    id: "place",
    header: "Place (CIS)",
    accessorFn: (r) => r.destination?.name ?? r.country_name ?? "",
    cell: ({ row }) => {
      const text = row.original.destination?.name ?? row.original.country_name ?? "—";
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
  { id: "group", header: "Max pax", accessorKey: "max_group_size", cell: ({ row }) => row.original.max_group_size ?? "—" },
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
