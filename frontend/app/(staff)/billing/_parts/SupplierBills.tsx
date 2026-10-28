"use client";
// app/(staff)/billing/_parts/SupplierBills.tsx
// AA-682 S4 (bk-030, S252 design §6) — the supplier bills list: /billing?tab=supplier-bills (all suppliers, with the
// four KPIs) and the Bills tab of /suppliers/[id] (one supplier). GET /booking/supplier-bills, paged on the server,
// filters in the URL; a row opens the bill drawer (?bill=BIL-…). "Enter bill" needs bill.write (admin,
// contracting). Reads need bill.read (admin, sales, contracting); never in a preview session.

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { supplierBillsApi, type BillKpi, type SupplierBill } from "@/lib/api/supplierBills";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  Button,
  DataTable,
  type CsvColumn,
  DateText,
  FilterBar,
  KpiTile,
  Money,
  Section,
  SupplierBillStatusBadge,
  formatDay,
  formatMoney,
  useCan,
  useUrlFilters,
} from "@/ui";
import { BillDrawer, BillForm } from "./BillDrawer";

function kpiValue(k: BillKpi | undefined): React.ReactNode {
  if (!k) return undefined;
  if (k.by_currency.length === 0) return formatMoney("0", "USD");
  return k.by_currency.map((c) => formatMoney(c.amount, c.currency)).join(" + ");
}

export function SupplierBillKpis() {
  const canRead = useCan("bill.read");
  const q = useQuery({ queryKey: ["supplier-bills", "summary"], queryFn: supplierBillsApi.summary, enabled: canRead });
  const s = q.data;
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <KpiTile label="To approve" value={canRead ? kpiValue(s?.to_approve) : null} hint={s ? `${s.to_approve.count} bill(s) received` : undefined} />
      <KpiTile label="To pay" value={canRead ? kpiValue(s?.to_pay) : null} hint={s ? `${s.to_pay.count} approved bill(s), balance left` : undefined} />
      <KpiTile
        label="Overdue"
        value={canRead ? kpiValue(s?.overdue) : null}
        hint={s ? `${s.overdue.count} bill(s) past their due date` : undefined}
        hintTone={s && s.overdue.count ? "danger" : "neutral"}
      />
      <KpiTile label="Paid out this month" value={canRead ? kpiValue(s?.paid_this_month) : null} hint={s ? `${s.paid_this_month.count} payout(s) since ${formatDay(s.month_start)}` : undefined} />
    </div>
  );
}

export function SupplierBillsList({
  supplier,
  title,
  description,
}: {
  supplier?: { id: string; name: string };
  title?: string;
  description?: string;
}) {
  const canRead = useCan("bill.read");
  const canWrite = useCan("bill.write");
  const qc = useQueryClient();
  const { get, setMany } = useUrlFilters();
  // Opening / closing the drawer keeps the list page (the URL helper resets it on other filter changes).
  const showBill = (code: string | null) => setMany({ bill: code, page: get("page") || null });
  const paging = useOffsetPage();
  const [entering, setEntering] = useState(false);
  const query = {
    status: get("bill_status") || undefined,
    overdue: get("bill_due") === "overdue" ? true : undefined,
    q: get("q") || undefined,
    supplier_id: supplier?.id,
    // ?case=TC-… (from a case's Cost vs billed panel): the bills of that case, or with a line of it.
    case: get("case") || undefined,
    limit: paging.limit,
    offset: paging.offset,
  };
  const list = useQuery({ queryKey: ["supplier-bills", "list", query], queryFn: () => supplierBillsApi.list(query), enabled: canRead });
  const openBill = get("bill");
  const caseFilter = get("case");

  // AA-996 (review fix): the list exports CSV like every Booking list, one column per fact (the Bill cell shows the
  // code and the supplier's own number).
  const csvCols: CsvColumn<SupplierBill>[] = [
    { header: "Bill", value: (r) => r.code },
    { header: "Supplier bill number", value: (r) => r.bill_number },
    ...(supplier ? [] : [{ header: "Supplier", value: (r: SupplierBill) => r.supplier.name }]),
    { header: "Trip case", value: (r) => r.trip_case?.code ?? "" },
    { header: "Issued", value: (r) => r.issue_date },
    { header: "Due", value: (r) => r.due_date },
    { header: "Amount", value: (r) => r.amount },
    { header: "Paid", value: (r) => r.paid_amount },
    { header: "Left to pay", value: (r) => (r.status === "cancelled" ? "" : r.balance) },
    { header: "Currency", value: (r) => r.currency },
    { header: "Status", value: (r) => r.status },
    { header: "Overdue", value: (r) => (r.overdue ? "yes" : "no") },
  ];

  const columns: ColumnDef<SupplierBill, unknown>[] = [
    {
      id: "code",
      header: "Bill",
      cell: ({ row }) => (
        <span className="flex flex-col">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              showBill(row.original.code);
            }}
            className="cursor-pointer text-left text-[13px] font-medium text-accent-deep hover:underline"
          >
            {row.original.code}
          </button>
          <span className="block max-w-[160px] truncate text-xs text-muted" title={row.original.bill_number}>
            {row.original.bill_number}
          </span>
        </span>
      ),
    },
    ...(supplier
      ? []
      : [
          {
            id: "supplier",
            header: "Supplier",
            cell: ({ row }) => <span className="block max-w-[200px] truncate text-[13px] text-body">{row.original.supplier.name}</span>,
          } satisfies ColumnDef<SupplierBill, unknown>,
        ]),
    {
      id: "case",
      header: "Trip case",
      cell: ({ row }) => <span className="text-[13px] text-body">{row.original.trip_case?.code ?? "Several / none"}</span>,
    },
    {
      id: "due",
      header: "Due",
      cell: ({ row }) => <DateText value={row.original.due_date} className={`text-[13px] ${row.original.overdue ? "font-semibold text-danger" : ""}`} />,
    },
    { id: "amount", header: "Amount", cell: ({ row }) => <Money amount={row.original.amount} currency={row.original.currency} className="text-[13px]" /> },
    {
      id: "balance",
      header: "Left to pay",
      cell: ({ row }) =>
        row.original.status === "cancelled" ? (
          <span className="text-muted">—</span>
        ) : (
          <Money amount={row.original.balance} currency={row.original.currency} strong className="text-[13px]" />
        ),
    },
    {
      id: "status",
      header: "Status",
      cell: ({ row }) => <SupplierBillStatusBadge status={row.original.status} overdue={row.original.overdue} />,
    },
  ];

  return (
    <Section
      title={title}
      description={description}
      actions={
        canWrite ? (
          <Button variant="primary" onClick={() => setEntering(true)}>
            <Plus size={14} /> Enter bill
          </Button>
        ) : null
      }
    >
      {caseFilter && (
        <div className="mb-3 flex flex-wrap items-center gap-2 text-[13px] text-body">
          <span>
            Bills of trip case <strong className="text-ink">{caseFilter}</strong> (its own bills and bills with a line of it)
          </span>
          <button type="button" className="text-xs font-semibold text-accent-deep hover:underline" onClick={() => setMany({ case: null })}>
            Show all
          </button>
        </div>
      )}
      <DataTable<SupplierBill>
        filters={
          <FilterBar
            extraKeys={["case"]}
            search={{ placeholder: supplier ? "Search bill or trip case" : "Search bill, supplier or trip case" }}
            selects={[
              {
                key: "bill_status",
                label: "Status",
                allLabel: "All statuses",
                options: [
                  { value: "open", label: "Open (to approve, to pay)" },
                  { value: "received", label: "To approve" },
                  { value: "approved", label: "To pay" },
                  { value: "paid", label: "Paid" },
                  { value: "cancelled", label: "Cancelled" },
                ],
                width: 200,
              },
              { key: "bill_due", label: "Due", allLabel: "Any", options: [{ value: "overdue", label: "Overdue" }], width: 120 },
            ]}
          />
        }
        searchable={false}
        enableCsv
        csv={csvCols}
        csvFilename={supplier ? "supplier-bills-of-supplier.csv" : "supplier-bills.csv"}
        tableId={supplier ? "supplier-bills-of-supplier" : "supplier-bills"}
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        onRowClick={(r) => showBill(r.code)}
        loading={list.isLoading}
        error={list.isError ? (list.error instanceof ApiError ? list.error.message : "Could not load the supplier bills") : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle={canRead ? "No supplier bill matches" : "Supplier bills need the admin, contracting or sales role"}
        emptyDescription={
          supplier
            ? `Enter a bill when ${supplier.name} sends one; it is matched to its confirmed bookings.`
            : "Enter a bill when a supplier sends one; it is matched to confirmed supplier bookings."
        }
        tableMinWidth={supplier ? 760 : 940}
      />
      <BillDrawer billRef={openBill || null} onClose={() => showBill(null)} />
      {entering && (
        <BillForm
          open
          supplier={supplier ?? null}
          tripCase={caseFilter || undefined}
          onClose={() => setEntering(false)}
          onSaved={(b) => {
            qc.invalidateQueries({ queryKey: ["supplier-bills"] });
            showBill(b.code);
          }}
        />
      )}
    </Section>
  );
}
