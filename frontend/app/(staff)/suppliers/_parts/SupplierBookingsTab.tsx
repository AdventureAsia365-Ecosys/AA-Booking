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
import {
  DataTable, DateText, Money, SUPPLIER_BOOKING_STATUS_META, Section, SegmentedControl, SupplierBookingStatusBadge, useCan,
  useUrlFilters,
} from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./SupplierBookingsTab.messages";

export function SupplierBookingsTab({ s }: { s: SupplierDetail }) {
  const t = useT(MSG);
  const tx = useTx();
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
      header: t("Trip case"),
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
      header: t("Booking"),
      accessorFn: (r) => r.item_description,
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col md:max-w-[280px]">
          <span className="truncate text-[13px] text-ink" title={tx(row.original.item_description)}>
            {tx(row.original.item_description)}
          </span>
          <span className="text-xs text-muted">{row.original.code}</span>
        </span>
      ),
    },
    {
      id: "date",
      header: t("Service date"),
      accessorFn: (r) => r.service_date ?? "",
      cell: ({ row }) =>
        row.original.service_date ? <DateText value={row.original.service_date} className="text-[13px]" /> : <span className="text-muted">—</span>,
    },
    {
      id: "net",
      header: t("Net"),
      cell: ({ row }) =>
        row.original.net_amount ? <Money amount={row.original.net_amount} currency={row.original.net_currency} /> : <span className="text-muted">—</span>,
    },
    {
      id: "status",
      header: t("Status"),
      accessorFn: (r) => r.status,
      cell: ({ row }) => (
        <span className="flex flex-col gap-1">
          <SupplierBookingStatusBadge status={row.original.status} />
          {row.original.confirmation_ref && <span className="text-xs text-muted">{t("Ref")} {row.original.confirmation_ref}</span>}
          {row.original.decline_code && <span className="text-xs text-muted">{tx(DECLINE_LABEL[row.original.decline_code])}</span>}
          {row.original.status === "requested" && row.original.requested_at && (
            <span className="text-xs text-muted">
              {t("asked")} <DateText value={row.original.requested_at} relative />
            </span>
          )}
        </span>
      ),
    },
  ];

  return (
    <Section title={t("Bookings with this supplier")} description={t("Supplier bookings of sold trips. Request, confirm or decline them on the trip case's Suppliers tab.")}>
      <SegmentedControl
        appearance="chips"
        size="sm"
        label={t("Booking status")}
        className="mb-3"
        options={(["live", ...SUPPLIER_BOOKING_STATUSES] as const).map((k) => ({
          value: k,
          label: k === "live" ? t("Live") : SUPPLIER_BOOKING_STATUS_META[k].label,
        }))}
        value={status}
        onChange={(k) => set("sb_status", k === "live" ? null : k)}
      />
      <DataTable<SupplierBooking>
        searchable={false}
        tableId="supplier-bookings"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? (list.error instanceof ApiError ? list.error.message : t("Could not load bookings")) : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle={canRead ? t("No booking here yet") : t("Bookings need the operations role")}
        emptyDescription={t("Bookings appear when a trip case books {0}.", s.company_name)}
        tableMinWidth={760}
      />
    </Section>
  );
}
