"use client";
// app/(staff)/suppliers/_parts/SupplierBookingsTab.tsx
// AA-682 S1 (bk-026) — the Bookings tab of a supplier: every supplier booking made with it for sold trip cases
// (GET /booking/supplier-bookings?supplier_id=…, ops.read), filtered by status, paged on the server. Actions live on the
// case's Suppliers tab (one place to request / confirm / decline), so each row links there.

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import { DECLINE_LABEL, SUPPLIER_BOOKING_STATUSES, supplierBookingsApi, type SupplierBooking } from "@/lib/api/supplierBookings";
import type { SupplierDetail } from "@/lib/api/suppliers";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { DataTable, DateText, Money, SUPPLIER_BOOKING_STATUS_META, Section, SupplierBookingStatusBadge, useCan, useUrlFilters } from "@/ui";

export function SupplierBookingsTab({ s }: { s: SupplierDetail }) {
  const canRead = useCan("ops.read");
  const { get, set } = useUrlFilters();
  const paging = useOffsetPage();
  const status = get("sb_status") || "live";
  const query = { supplier_id: s.id, status, limit: paging.limit, offset: paging.offset };
  const list = useQuery({
    queryKey: ["supplier-bookings", "by-supplier", query],
    queryFn: () => supplierBookingsApi.list(query),
    enabled: canRead,
  });

  const columns: ColumnDef<SupplierBooking, unknown>[] = [
    {
      id: "case",
      header: "Trip case",
      accessorFn: (r) => r.trip_case.code,
      cell: ({ row }) => (
        <Link
          href={`/cases/${encodeURIComponent(row.original.trip_case.code)}?tab=suppliers`}
          className="text-[13px] font-medium text-accent-deep hover:underline"
        >
          {row.original.trip_case.code}
        </Link>
      ),
    },
    {
      id: "item",
      header: "Booking",
      accessorFn: (r) => r.item_description,
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col md:max-w-[280px]">
          <span className="truncate text-[13px] text-ink" title={row.original.item_description}>
            {row.original.item_description}
          </span>
          <span className="text-xs text-muted">{row.original.code}</span>
        </span>
      ),
    },
    {
      id: "date",
      header: "Service date",
      accessorFn: (r) => r.service_date ?? "",
      cell: ({ row }) =>
        row.original.service_date ? <DateText value={row.original.service_date} className="text-[13px]" /> : <span className="text-muted">—</span>,
    },
    {
      id: "net",
      header: "Net",
      cell: ({ row }) =>
        row.original.net_amount ? <Money amount={row.original.net_amount} currency={row.original.net_currency} /> : <span className="text-muted">—</span>,
    },
    {
      id: "status",
      header: "Status",
      accessorFn: (r) => r.status,
      cell: ({ row }) => (
        <span className="flex flex-col gap-1">
          <SupplierBookingStatusBadge status={row.original.status} />
          {row.original.confirmation_ref && <span className="text-xs text-muted">Ref {row.original.confirmation_ref}</span>}
          {row.original.decline_code && <span className="text-xs text-muted">{DECLINE_LABEL[row.original.decline_code]}</span>}
          {row.original.status === "requested" && row.original.requested_at && (
            <span className="text-xs text-muted">
              asked <DateText value={row.original.requested_at} relative />
            </span>
          )}
        </span>
      ),
    },
  ];

  return (
    <Section title="Bookings with this supplier" description="Supplier bookings of sold trips. Request, confirm or decline them on the trip case's Suppliers tab.">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {(["live", ...SUPPLIER_BOOKING_STATUSES] as const).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => set("sb_status", k === "live" ? null : k)}
            className={`rounded-full border px-3 py-1 text-xs ${
              status === k ? "border-accent bg-accent-tint font-semibold text-accent-deep" : "border-line text-muted hover:text-ink"
            }`}
          >
            {k === "live" ? "Live" : SUPPLIER_BOOKING_STATUS_META[k].label}
          </button>
        ))}
      </div>
      <DataTable<SupplierBooking>
        searchable={false}
        tableId="supplier-bookings"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? (list.error instanceof ApiError ? list.error.message : "Could not load bookings") : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle={canRead ? "No booking here yet" : "Bookings need the operations role"}
        emptyDescription={`Bookings appear when a trip case books ${s.company_name}.`}
        tableMinWidth={760}
      />
    </Section>
  );
}
