"use client";
// app/(staff)/customers/page.tsx — Customer directory (W1, UI spec §4.12; Figma USER MANAGEMENT-1…-4).
// GET /booking/customers (status derived in booking.v_customer_summary, bk-005): tabs by computed status
// with counts, search, merge-review and source filters, server pagination, CSV of the current page.
// "+ New lead" (POST), and per row: view, new trip case, block / unblock and archive (admin).

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQueries, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Archive, Ban, Briefcase, Eye, Plus, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api/client";
import type { CustomerSummary } from "@/lib/api/types";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { Button, CustomerStatusBadge, DataTable, DateText, FilterBar, PageFrame, PersonCell, Pill, Section, useCan, useUrlFilters } from "@/ui";
import { RowMenu, type RowMenuItem } from "@/ui/booking/RowMenu";
import { SavedTabs } from "@/app/(staff)/_parts/SavedTabs";
import { CustomerFormDrawer } from "./_parts/CustomerFormDrawer";
import { useCustomerActions } from "./_parts/useCustomerActions";
import { CUSTOMER_TABS, SOURCE_LABEL, customerRef } from "./_parts/shared";

export default function CustomersPage() {
  const router = useRouter();
  const { get } = useUrlFilters();
  const paging = useOffsetPage();
  const canWrite = useCan("customer.write");
  const canCreateCase = useCan("trip_case.write");
  const actions = useCustomerActions();
  const [newLead, setNewLead] = useState(false);

  const tab = CUSTOMER_TABS.find((t) => t.key === get("tab")) ?? CUSTOMER_TABS[0];
  const base = {
    q: get("q") || undefined,
    merge_review: get("merge") === "yes" ? true : undefined,
    source: get("source") || undefined,
  };
  const filters = { ...base, status: tab.status, sort: "-last_activity_at", limit: paging.limit, offset: paging.offset };
  const list = useQuery({ queryKey: ["customers", "list", filters], queryFn: () => api.customers.list(filters) });

  // Tab counts under the same search / filters (limit 1: only `total` is read).
  const counts = useQueries({
    queries: CUSTOMER_TABS.map((t) => ({
      queryKey: ["customers", "count", t.key, base],
      queryFn: () => api.customers.list({ ...base, status: t.status, limit: 1 }),
      staleTime: 30_000,
    })),
  });

  function menuFor(c: CustomerSummary): RowMenuItem[] {
    const items: RowMenuItem[] = [{ label: "View profile", icon: <Eye size={15} />, onSelect: () => router.push(`/customers/${customerRef(c)}`) }];
    if (canCreateCase && !c.is_blocked) {
      items.push({ label: "New trip case", icon: <Briefcase size={15} />, onSelect: () => router.push(`/cases/new?customer=${customerRef(c)}`) });
    }
    if (actions.canAdmin) {
      items.push(
        c.is_blocked
          ? { label: "Unblock customer", icon: <ShieldCheck size={15} />, onSelect: () => actions.unblock(c), divider: true }
          : { label: "Block customer", icon: <Ban size={15} />, onSelect: () => actions.block(c), danger: true, divider: true },
        { label: "Archive", icon: <Archive size={15} />, onSelect: () => actions.archive(c), danger: true },
      );
    }
    return items;
  }

  const columns: ColumnDef<CustomerSummary, unknown>[] = [
    ...COLUMNS,
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => <RowMenu items={menuFor(row.original)} label={`Actions for ${row.original.name ?? "customer"}`} />,
    },
  ];

  const filtered = !!(base.q || base.merge_review || base.source);
  return (
    <PageFrame
      title="Customers"
      description="Everyone who asked us for a trip: TripPlanner leads, verified customers and past clients."
      actions={
        canWrite && (
          <Button variant="primary" onClick={() => setNewLead(true)}>
            <Plus size={15} /> New lead
          </Button>
        )
      }
    >
      <SavedTabs
        tabs={CUSTOMER_TABS.map((t, i) => ({ key: t.key, label: t.label, count: counts[i]?.data?.total, countPending: counts[i]?.isPending }))}
      />
      <Section>
        <DataTable<CustomerSummary>
          filters={
            <FilterBar
              search={{ placeholder: "Search name, e-mail, phone or CUS- code" }}
              selects={[
                { key: "merge", label: "Merge review", allLabel: "Any", options: [{ value: "yes", label: "Needs review" }] },
                {
                  key: "source",
                  label: "Source",
                  allLabel: "Any source",
                  options: Object.entries(SOURCE_LABEL).map(([value, label]) => ({ value, label })),
                },
              ]}
            />
          }
          searchable={false}
          tableId="customers"
          data={list.data?.items ?? []}
          columns={columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          error={list.isError ? (list.error as Error).message : null}
          onRetry={() => list.refetch()}
          pageSizeOptions={PAGE_SIZES}
          serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
          enableCsv
          csvFilename="customers.csv"
          emptyTitle={filtered ? "No customers match" : tab.key === "all" ? "No customers yet" : `No ${tab.label.toLowerCase()} yet`}
          emptyDescription={
            filtered
              ? "Change the search or the filters, or reset them to see everyone."
              : tab.key === "all"
                ? "Leads arrive from TripPlanner inquiries. Add a phone or e-mail lead with New lead."
                : "Customers move between tabs as their trip cases progress."
          }
          onRowClick={(r) => router.push(`/customers/${customerRef(r)}`)}
          tableMinWidth={960}
        />
      </Section>
      <CustomerFormDrawer
        open={newLead}
        onClose={() => setNewLead(false)}
        mode={{ kind: "create" }}
        onCreated={(c) => router.push(`/customers/${customerRef(c)}`)}
      />
      {actions.dialogs}
    </PageFrame>
  );
}

// AA-910 (S250): fits the list frame at 1400 and 1440 px with 20 px to spare: location 130 px on desktop (full text in
// the tooltip; AA-996 audit: 20 px went to the customer name, 260 px, which the Figma audit found cut at 220) and the short "Cases" header; a phone keeps at least 100 px (two lines at most, not one word per line);
// the status badges (Blocked, Merge review) wrap under each other, never cut.
const COLUMNS: ColumnDef<CustomerSummary, unknown>[] = [
  {
    id: "customer",
    header: "Customer",
    accessorFn: (r) => r.name ?? "",
    cell: ({ row }) => <PersonCell className="md:max-w-[260px]" name={row.original.name} sub={row.original.email ?? row.original.code} />,
  },
  { id: "phone", header: "Phone", accessorFn: (r) => r.phone ?? "", cell: ({ row }) => <span className="block text-[13px] text-body md:min-w-[100px]">{row.original.phone ?? "—"}</span> },
  {
    id: "address",
    header: "Location",
    accessorFn: (r) => r.address ?? "",
    cell: ({ row }) => <span className="block max-w-[220px] truncate text-[13px] text-body md:max-w-[130px]" title={row.original.address ?? undefined}>{row.original.address ?? "—"}</span>,
  },
  {
    id: "status",
    header: "Status",
    accessorKey: "status",
    cell: ({ row }) => (
      <span className="inline-flex flex-wrap justify-end gap-1 md:justify-start">
        <CustomerStatusBadge status={row.original.status} />
        {row.original.is_blocked && <Pill tone="danger" dot={false}>Blocked</Pill>}
        {row.original.needs_merge_review && <Pill tone="warning" dot={false}>Merge review</Pill>}
      </span>
    ),
  },
  { id: "cases", header: "Cases", accessorKey: "case_count", cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.case_count}</span> },
  { id: "activity", header: "Last activity", accessorKey: "last_activity_at", cell: ({ row }) => <DateText value={row.original.last_activity_at} relative className="text-[13px] text-muted" /> },
];
