"use client";
// app/(staff)/suppliers/_parts/SuppliersListView.tsx — Supplier directory (W2, AA-822; UI spec §3 / §6.3, module spec
// 08 screen 1). GET /booking/suppliers with server-side search, status / category / country filters and pagination;
// KPI strip from /suppliers/summary (Total, Active, Inactive, contracts ending in 30 days: 08 conflict 8). Admin and
// contracting add, edit and deactivate (supply.write); sales read. Suppliers are never deleted (08 conflict 4).
// AA-910: fits the 1084 px frame at 1440 px (location under the name, text cells with a max width); the row menu
// column stays pinned to the right when a narrower window makes the table scroll inside its frame.

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, Eye, Pencil, Plus, Power } from "lucide-react";
import { SUPPLIER_CATEGORIES, suppliersApi, type Supplier } from "@/lib/api/suppliers";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { Code, DataTable, DateText, FilterBar, KpiTile, PageFrame, PersonCell, Section, useUrlFilters, RowMenu, type RowMenuItem } from "@/ui";
import { useCountries } from "@/app/(staff)/cases/_parts/CountryPicker";
import { CATEGORY_LABEL, CategoryChips, SupplierStatusBadge, locationText, supplierHref } from "./shared";
import { useSupplierActions } from "./useSupplierActions";

export function SuppliersListView() {
  const router = useRouter();
  const { get } = useUrlFilters();
  const paging = useOffsetPage();
  const actions = useSupplierActions();
  const countries = useCountries();

  const filters = {
    q: get("q") || undefined,
    status: get("status") || undefined,
    category: get("category") || undefined,
    country: get("country") || undefined,
    sort: get("sort") || undefined,
  };
  const list = useQuery({
    queryKey: ["suppliers", "list", filters, paging.limit, paging.offset],
    queryFn: () => suppliersApi.list({ ...filters, limit: paging.limit, offset: paging.offset }),
  });
  const summary = useQuery({ queryKey: ["suppliers", "summary"], queryFn: suppliersApi.summary, staleTime: 30_000 });
  const s = summary.data;

  function menuFor(row: Supplier): RowMenuItem[] {
    const items: RowMenuItem[] = [{ label: "View supplier", icon: <Eye size={15} />, onSelect: () => router.push(supplierHref(row)) }];
    if (actions.canWrite) {
      items.push({ label: "Edit profile", icon: <Pencil size={15} />, onSelect: () => router.push(supplierHref(row, "/edit")) });
      items.push(
        row.status === "active"
          ? { label: "Deactivate", icon: <Ban size={15} />, onSelect: () => actions.deactivate(row), danger: true, divider: true }
          : { label: "Activate", icon: <Power size={15} />, onSelect: () => actions.activate(row), divider: true },
      );
    }
    return items;
  }

  const columns: ColumnDef<Supplier, unknown>[] = [
    ...COLUMNS,
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => <RowMenu items={menuFor(row.original)} label={`Actions for ${row.original.company_name}`} />,
    },
  ];

  const filtered = !!(filters.q || filters.status || filters.category || filters.country);
  return (
    <PageFrame
      title="Suppliers"
      description="Hotels, transport, guides and other partners Booking buys from, with their contracts."
      actions={
        actions.canWrite && (
          <Link
            href="/suppliers/new"
            className="inline-flex items-center gap-1.5 rounded-full border border-accent bg-accent px-4 py-2 text-[13px] font-semibold uppercase tracking-[0.04em] text-on-accent hover:bg-accent-deep"
          >
            <Plus size={15} /> Add supplier
          </Link>
        )
      }
    >
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiTile label="Total suppliers" value={s?.total} hint={s ? `Across ${s.countries} ${s.countries === 1 ? "country" : "countries"}` : undefined} />
        <KpiTile label="Active" value={s?.active} hintTone="success" hint={s ? "Can take new contracts" : undefined} />
        <KpiTile label="Inactive" value={s?.inactive} hint={s ? "Kept on record, no new contracts" : undefined} />
        <KpiTile
          label="Contracts ending in 30 days"
          value={s?.contracts_ending_30d}
          hintTone={s && s.contracts_ending_30d > 0 ? "warning" : "neutral"}
          hint={s ? (s.contracts_ending_30d > 0 ? "Renew or plan new terms" : "Nothing ending soon") : undefined}
        />
      </div>
      <Section>
        <div className="bk-dt-sticky-end">
          <DataTable<Supplier>
            filters={
              <FilterBar
                search={{ placeholder: "Search name, SUP- code, contact or e-mail" }}
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
                    key: "category",
                    label: "Category",
                    allLabel: "All categories",
                    options: SUPPLIER_CATEGORIES.map((c) => ({ value: c, label: CATEGORY_LABEL[c] })),
                  },
                  {
                    key: "country",
                    label: "Country",
                    allLabel: "All countries",
                    options: (countries.data?.items ?? []).map((c) => ({ value: c.iso2, label: c.name })),
                    width: 170, // the countries load after the page: a fixed width keeps the bar from shifting (CLS)
                  },
                  {
                    key: "sort",
                    label: "Sort",
                    allLabel: "Name A–Z",
                    options: [
                      { value: "-updated_at", label: "Recently updated" },
                      { value: "-created_at", label: "Newest first" },
                      { value: "-company_name", label: "Name Z–A" },
                    ],
                  },
                ]}
              />
            }
            searchable={false}
            tableId="suppliers"
            enableCsv
            csvFilename="suppliers.csv"
            data={list.data?.items ?? []}
            columns={columns}
            getRowId={(r) => r.id}
            loading={list.isLoading}
            error={list.isError ? (list.error as Error).message : null}
            onRetry={() => list.refetch()}
            pageSizeOptions={PAGE_SIZES}
            serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
            emptyTitle={filtered ? "No suppliers match" : "No suppliers yet"}
            emptyDescription={
              filtered
                ? "Change the search or the filters, or reset them to see every supplier."
                : actions.canWrite
                  ? "Add the first supplier with Add supplier. Contracts are added on its page."
                  : "Contracting adds suppliers here; they appear once created."
            }
            onRowClick={(r) => router.push(supplierHref(r))}
            tableMinWidth={880}
          />
        </div>
      </Section>
      {actions.dialogs}
    </PageFrame>
  );
}

const COLUMNS: ColumnDef<Supplier, unknown>[] = [
  {
    id: "supplier",
    header: "Supplier",
    accessorFn: (r) => r.company_name,
    cell: ({ row }) => {
      const where = locationText(row.original);
      // AA-910: 200 px on desktop (and the contact 180 px) so the list keeps 20 px to spare at 1400 when "Updated" shows
      // a full date ("28 Sept 2026": rows older than 7 days, most of Dev). Full name and e-mail in the tooltips.
      return (
        <span className="flex min-w-0 max-w-[240px] flex-col md:max-w-[200px]">
          <span className="truncate text-[13.5px] font-medium text-ink" title={row.original.company_name}>
            {row.original.company_name}
          </span>
          <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted">
            <Code>{row.original.code}</Code>
            {where && (
              <span className="truncate" title={where}>
                · {where}
              </span>
            )}
          </span>
        </span>
      );
    },
  },
  { id: "categories", header: "Categories", accessorFn: (r) => r.categories.join(" "), enableSorting: false, cell: ({ row }) => <CategoryChips categories={row.original.categories} /> },
  {
    id: "contact",
    header: "Contact",
    accessorFn: (r) => r.contact_name ?? "",
    cell: ({ row }) =>
      row.original.contact_name || row.original.contact_email ? (
        <PersonCell
          className="md:max-w-[180px]"
          name={row.original.contact_name ?? row.original.contact_email}
          sub={row.original.contact_name ? row.original.contact_email : null}
        />
      ) : (
        <span className="text-[13px] text-muted">—</span>
      ),
  },
  {
    id: "contracts",
    header: "Contracts",
    meta: { compact: "drawer" }, // AA-996: a count, in the details drawer below 1200 px (with Updated)
    accessorKey: "contracts_in_force",
    cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.contracts_in_force}</span>,
  },
  { id: "status", header: "Status", accessorKey: "status", cell: ({ row }) => <SupplierStatusBadge status={row.original.status} /> },
  { id: "updated", header: "Updated", meta: { compact: "drawer" }, accessorKey: "updated_at", cell: ({ row }) => <DateText value={row.original.updated_at} relative className="whitespace-nowrap text-[13px] text-muted" /> },
];
