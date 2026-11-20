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
  LinkButton,
  Money,
  Section,
  SupplierBillStatusBadge,
  formatMoney,
  useCan,
  useUrlFilters,
} from "@/ui";
import { BillDrawer, BillForm } from "./BillDrawer";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { useBookingFormat } from "@/ui/local/useBookingFormat";
import { MSG } from "./SupplierBills.messages";

function kpiValue(k: BillKpi | undefined, lang: "en" | "vi"): React.ReactNode {
  if (!k) return undefined;
  if (k.by_currency.length === 0) return formatMoney("0", "USD", lang);
  return k.by_currency.map((c) => formatMoney(c.amount, c.currency, lang)).join(" + ");
}

export function SupplierBillKpis() {
  const t = useT(MSG);
  const { formatDay, lang } = useBookingFormat();
  const canRead = useCan("bill.read");
  const q = useQuery({ queryKey: ["supplier-bills", "summary"], queryFn: supplierBillsApi.summary, enabled: canRead });
  const s = q.data;
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <KpiTile label={t("To approve")} value={canRead ? kpiValue(s?.to_approve, lang) : null} hint={s ? t("{0} bill(s) received", s.to_approve.count) : undefined} />
      <KpiTile label={t("To pay")} value={canRead ? kpiValue(s?.to_pay, lang) : null} hint={s ? t("{0} approved bill(s), balance left", s.to_pay.count) : undefined} />
      <KpiTile
        label={t("Overdue")}
        value={canRead ? kpiValue(s?.overdue, lang) : null}
        hint={s ? t("{0} bill(s) past their due date", s.overdue.count) : undefined}
        hintTone={s && s.overdue.count ? "danger" : "neutral"}
      />
      <KpiTile label={t("Paid out this month")} value={canRead ? kpiValue(s?.paid_this_month, lang) : null} hint={s ? t("{0} payout(s) since {1}", s.paid_this_month.count, formatDay(s.month_start)) : undefined} />
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
  const t = useT(MSG);
  const tx = useTx();
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
    // AA-1159: "Due soon" = open bills not overdue yet that fall due within the reminder's window (policy
    // supplier_bill_due_soon_days); the My work tile of the dashboard opens this view.
    due_soon: get("bill_due") === "soon" ? true : undefined,
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
    { header: t("Bill"), value: (r) => r.code },
    { header: t("Supplier bill number"), value: (r) => r.bill_number },
    ...(supplier ? [] : [{ header: t("Supplier"), value: (r: SupplierBill) => r.supplier.name }]),
    { header: t("Trip case"), value: (r) => r.trip_case?.code ?? "" },
    { header: t("Issued"), value: (r) => r.issue_date },
    { header: t("Due"), value: (r) => r.due_date },
    { header: t("Amount"), value: (r) => r.amount },
    { header: t("Paid"), value: (r) => r.paid_amount },
    { header: t("Left to pay"), value: (r) => (r.status === "cancelled" ? "" : r.balance) },
    { header: t("Currency"), value: (r) => r.currency },
    { header: t("Status"), value: (r) => r.status },
    { header: t("Overdue"), value: (r) => (r.overdue ? "yes" : "no") },
  ];

  const columns: ColumnDef<SupplierBill, unknown>[] = [
    {
      id: "code",
      header: t("Bill"),
      cell: ({ row }) => (
        <span className="flex flex-col">
          <LinkButton
            onClick={(e) => {
              e.stopPropagation();
              showBill(row.original.code);
            }}
          >
            {row.original.code}
          </LinkButton>
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
            header: t("Supplier"),
            cell: ({ row }) => <span className="block max-w-[200px] truncate text-[13px] text-body">{row.original.supplier.name}</span>,
          } satisfies ColumnDef<SupplierBill, unknown>,
        ]),
    {
      id: "case",
      header: t("Trip case"),
      cell: ({ row }) => <span className="text-[13px] text-body">{row.original.trip_case?.code ?? t("Several / none")}</span>,
    },
    {
      id: "due",
      header: t("Due"),
      cell: ({ row }) => <DateText value={row.original.due_date} className={`text-[13px] ${row.original.overdue ? "font-semibold text-danger" : ""}`} />,
    },
    { id: "amount", header: t("Amount"), cell: ({ row }) => <Money amount={row.original.amount} currency={row.original.currency} className="text-[13px]" /> },
    {
      id: "balance",
      header: t("Left to pay"),
      cell: ({ row }) =>
        row.original.status === "cancelled" ? (
          <span className="text-muted">—</span>
        ) : (
          <Money amount={row.original.balance} currency={row.original.currency} strong className="text-[13px]" />
        ),
    },
    {
      id: "status",
      header: t("Status"),
      cell: ({ row }) => <SupplierBillStatusBadge status={row.original.status} overdue={row.original.overdue} />,
    },
  ];

  return (
    <Section
      title={tx(title)}
      description={tx(description)}
      actions={
        canWrite ? (
          <Button variant="primary" onClick={() => setEntering(true)}>
            <Plus size={14} /> {t("Enter bill")}
          </Button>
        ) : null
      }
    >
      {caseFilter && (
        <div className="mb-3 flex flex-wrap items-center gap-2 text-[13px] text-body">
          <span>
            {t("Bills of trip case")} <strong className="text-ink">{caseFilter}</strong> {t("(its own bills and bills with a line of it)")}
          </span>
          <LinkButton size="sm" onClick={() => setMany({ case: null })}>
            {t("Show all")}
          </LinkButton>
        </div>
      )}
      <DataTable<SupplierBill>
        filters={
          <FilterBar
            extraKeys={["case"]}
            search={{ placeholder: supplier ? t("Search bill or trip case") : t("Search bill, supplier or trip case") }}
            selects={[
              {
                key: "bill_status",
                label: t("Status"),
                allLabel: t("All statuses"),
                options: [
                  { value: "open", label: t("Open (to approve, to pay)") },
                  { value: "received", label: t("To approve") },
                  { value: "approved", label: t("To pay") },
                  { value: "paid", label: t("Paid") },
                  { value: "cancelled", label: t("Cancelled") },
                ],
                width: 200,
              },
              {
                key: "bill_due",
                label: t("Due"),
                allLabel: t("Any"),
                options: [
                  { value: "overdue", label: t("Overdue") },
                  { value: "soon", label: t("Due soon") },
                ],
                width: 130,
              },
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
        error={list.isError ? (list.error instanceof ApiError ? list.error.message : t("Could not load the supplier bills")) : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle={canRead ? t("No supplier bill matches") : t("Supplier bills need the admin, contracting or sales role")}
        emptyDescription={
          supplier
            ? t("Enter a bill when {0} sends one; it is matched to its confirmed bookings.", supplier.name)
            : t("Enter a bill when a supplier sends one; it is matched to confirmed supplier bookings.")
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
