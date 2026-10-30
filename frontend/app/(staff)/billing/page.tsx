"use client";
// app/(staff)/billing/page.tsx — Payments & invoices (AA-682 S3, bk-028; S252 design §6, UI spec §3, Figma PAYMENT &
// INVOICE (09)). Four KPIs (left to pay, overdue, received this month = verified money net of refunds, waiting for
// verification), then two tabs: Invoices (GET /booking/invoices) and Payments (GET /booking/payments), paged on the
// server, filters in the URL. Invoices are created from a trip case's Billing tab (they start from its approved
// proposal); payments are recorded on an invoice. billing.read (admin, sales); never in a preview session.
// AA-682 S4 (bk-030): a third tab, Supplier bills (?tab=supplier-bills; bill.read = admin, sales, contracting), with its
// own four KPIs (to approve, to pay, overdue, paid out this month) and the bill drawer. Contracting sees only that tab.
// S255 Figma rescore (PAYMENT & INVOICE): a row menu on the invoice and payment tables with the detail pages' actions:
// invoice = View invoice, Open trip case, Cancel invoice (when the row's `actions.cancel` allows it); payment = View
// payment, View invoice, Download evidence (when a file is attached), Void (when `actions.void` allows it). There is no
// invoice PDF in Booking yet, so no "Download PDF" item (the invoice page says so).

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Ban, Download, Eye, FileText, FolderOpen } from "lucide-react";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import { billingApi, PAYMENT_METHOD_LABEL, type InvoiceSummary, type Kpi, type Payment } from "@/lib/api/billing";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { SupplierBillKpis, SupplierBillsList } from "./_parts/SupplierBills";
import { useBillingRefresh } from "./_parts/InvoiceView";
import { VoidPaymentDialog, errorText } from "./_parts/PaymentDialogs";
import {
  ConfirmDialog,
  DataTable,
  DateText,
  FilterBar,
  InvoiceStatusBadge,
  csvMoney,
  type CsvColumn,
  formatDay,
  KpiTile,
  Money,
  PageFrame,
  PaymentStateBadge,
  paymentEvidenceFileId,
  RowMenu,
  type RowMenuItem,
  Section,
  Tabs,
  formatMoney,
  useCan,
  useToast,
  useUrlFilters,
} from "@/ui";

// AA-996 (Figma audit, deviation 5; Figma PAYMENT & INVOICE): invoice numbers, trip case codes, dates and amounts never
// wrap mid-value. To fit 1440 px with the widest real amounts (VND billions) the customer sits under the trip case code
// (Figma "Trip case & customer", the one cell that truncates) and the amount paid under the total. Display only: the
// CSV keeps one column per fact (INVOICE_CSV).
const INVOICE_COLUMNS: ColumnDef<InvoiceSummary, unknown>[] = [
  {
    id: "number",
    header: "Invoice",
    cell: ({ row }) => (
      <span className="flex flex-col">
        <Link
          href={`/billing/invoices/${encodeURIComponent(row.original.invoice_number)}`}
          onClick={(e) => e.stopPropagation()}
          className="whitespace-nowrap text-[13px] font-medium text-accent-deep hover:underline"
        >
          {row.original.invoice_number}
        </Link>
        <span className="whitespace-nowrap text-xs text-muted">{row.original.issued_at ? "Sent" : "Draft"}</span>
      </span>
    ),
  },
  {
    id: "case",
    header: "Trip case / customer",
    cell: ({ row }) => {
      const name = row.original.bill_to_name ?? row.original.customer?.name ?? "—";
      return (
        <span className="flex min-w-0 flex-col">
          <Link
            href={`/cases/${encodeURIComponent(row.original.trip_case.code)}?tab=billing`}
            onClick={(e) => e.stopPropagation()}
            className="self-start whitespace-nowrap text-[13px] text-accent-deep hover:underline"
          >
            {row.original.trip_case.code}
          </Link>
          <span className="block max-w-[220px] truncate text-xs text-muted md:max-w-[140px] min-[1200px]:max-w-[180px]" title={name}>
            {name}
          </span>
        </span>
      );
    },
  },
  {
    id: "issued",
    header: "Issued",
    // AA-996: Issued (audit) and Total (money detail, with the amount paid; "Left to pay" stays) open in the details
    // drawer below 1200 px.
    meta: { compact: "drawer" },
    cell: ({ row }) => (row.original.issued_at ? <DateText value={row.original.issued_at} className="whitespace-nowrap text-[13px]" /> : <span className="text-muted">—</span>),
  },
  {
    id: "due",
    header: "Next due",
    cell: ({ row }) =>
      row.original.next_due_date ? (
        <DateText value={row.original.next_due_date} className={`whitespace-nowrap text-[13px] ${row.original.overdue ? "font-semibold text-danger" : ""}`} />
      ) : (
        <span className="text-muted">—</span>
      ),
  },
  {
    id: "total",
    header: "Total / paid",
    meta: { compact: "drawer" },
    cell: ({ row }) => (
      <span className="flex flex-col whitespace-nowrap">
        <Money amount={row.original.total_amount} currency={row.original.currency} className="text-[13px]" />
        <span className="text-xs text-muted">
          paid <Money amount={row.original.net_paid} currency={row.original.currency} className="text-xs text-muted" />
        </span>
      </span>
    ),
  },
  {
    id: "balance",
    header: "Left to pay",
    cell: ({ row }) => <Money amount={row.original.balance} currency={row.original.currency} strong className="whitespace-nowrap text-[13px]" />,
  },
  { id: "status", header: "Status", cell: ({ row }) => <InvoiceStatusBadge status={row.original.status} overdue={row.original.overdue} /> },
];

/** The invoices CSV: one column per fact, whatever the table groups in one cell. */
const INVOICE_CSV: CsvColumn<InvoiceSummary>[] = [
  { header: "Invoice", value: (r) => r.invoice_number },
  { header: "Trip case", value: (r) => r.trip_case.code },
  { header: "Customer", value: (r) => r.bill_to_name ?? r.customer?.name ?? "" },
  { header: "Issued", value: (r) => r.issued_at ?? "" },
  { header: "Next due", value: (r) => r.next_due_date ?? "" },
  { header: "Total", value: (r) => r.total_amount },
  { header: "Paid", value: (r) => r.net_paid },
  { header: "Left to pay", value: (r) => r.balance ?? "" },
  { header: "Currency", value: (r) => r.currency },
  { header: "Status", value: (r) => r.status },
  { header: "Overdue", value: (r) => (r.overdue ? "yes" : "no") },
];

const PAYMENT_COLUMNS: ColumnDef<Payment, unknown>[] = [
  {
    id: "ref",
    header: "Payment",
    accessorFn: (r) => r.ref,
    enableSorting: false,
    cell: ({ row }) => (
      <span className="flex flex-col">
        <Link
          href={`/billing/payments/${encodeURIComponent(row.original.ref)}`}
          onClick={(e) => e.stopPropagation()}
          className="whitespace-nowrap text-[13px] font-medium text-accent-deep hover:underline"
        >
          {row.original.ref}
        </Link>
        <span className="text-xs text-muted">{row.original.kind === "refund" ? "Refund" : "Received"}</span>
      </span>
    ),
  },
  {
    id: "invoice",
    header: "Invoice",
    accessorFn: (r) => r.invoice.number,
    enableSorting: false,
    cell: ({ row }) => (
      <span className="flex flex-col text-[13px]">
        <Link
          href={`/billing/invoices/${encodeURIComponent(row.original.invoice.number)}`}
          onClick={(e) => e.stopPropagation()}
          className="whitespace-nowrap text-accent-deep hover:underline"
        >
          {row.original.invoice.number}
        </Link>
        <span className="whitespace-nowrap text-xs text-muted">{row.original.trip_case.code}</span>
      </span>
    ),
  },
  { id: "date", header: "Date", accessorFn: (r) => r.received_on, enableSorting: false, cell: ({ row }) => <DateText value={row.original.received_on} className="whitespace-nowrap text-[13px]" /> },
  { id: "method", header: "Method", accessorFn: (r) => PAYMENT_METHOD_LABEL[r.method], enableSorting: false, cell: ({ row }) => <span className="text-[13px] text-body">{PAYMENT_METHOD_LABEL[row.original.method]}</span> },
  {
    id: "amount",
    header: "Amount",
    accessorFn: (r) => csvMoney(r.kind === "refund" ? `-${r.amount}` : r.amount, r.currency),
    enableSorting: false,
    cell: ({ row }) => (
      <Money amount={row.original.kind === "refund" ? `-${row.original.amount}` : row.original.amount} currency={row.original.currency} strong className="text-[13px]" />
    ),
  },
  {
    id: "who",
    header: "Recorded by",
    meta: { compact: "drawer" }, // AA-996: audit, in the details drawer below 1200 px
    accessorFn: (r) => r.recorded_by?.name ?? "",
    enableSorting: false,
    cell: ({ row }) => <span className="block max-w-[160px] truncate text-[13px] text-body">{row.original.recorded_by?.name ?? "—"}</span>,
  },
  { id: "state", header: "Status", accessorFn: (r) => r.state, enableSorting: false, cell: ({ row }) => <PaymentStateBadge state={row.original.state} /> },
];

function kpiValue(k: Kpi | undefined): React.ReactNode {
  if (!k) return undefined;
  if (k.by_currency.length === 0) return formatMoney("0", "USD");
  if (k.by_currency.length === 1) return formatMoney(k.by_currency[0].amount, k.by_currency[0].currency);
  return formatMoney(k.usd, "USD");
}

function kpiHint(k: Kpi | undefined, noun: string): string | undefined {
  if (!k) return undefined;
  const parts = [`${k.count} ${noun}`];
  if (k.by_currency.length > 1) parts.push(`${k.by_currency.map((c) => formatMoney(c.amount, c.currency)).join(" + ")} (USD at the quote's FX)`);
  return parts.join(" · ");
}

export default function BillingPage() {
  const canRead = useCan("billing.read");
  const canBills = useCan("bill.read");
  const { get, setMany } = useUrlFilters();
  const asked = get("tab");
  const tab =
    asked === "supplier-bills" && canBills
      ? "supplier-bills"
      : !canRead && canBills
        ? "supplier-bills"
        : asked === "payments"
          ? "payments"
          : "invoices";
  const summary = useQuery({ queryKey: ["billing", "summary"], queryFn: billingApi.summary, enabled: canRead });
  const s = summary.data;
  const tabs = [
    ...(canRead || !canBills
      ? [
          { key: "invoices", label: "Invoices" },
          { key: "payments", label: "Payments" },
        ]
      : []),
    ...(canBills ? [{ key: "supplier-bills", label: "Supplier bills" }] : []),
  ];
  return (
    <PageFrame
      title="Payments & invoices"
      description={
        tab === "supplier-bills"
          ? "Bills suppliers sent AA, matched to confirmed supplier bookings. Another person approves each bill; an admin records the payouts."
          : "Payment requests sent to customers and the money recorded against them. Payments are marked by hand and count once a second admin verifies them."
      }
    >
      {tab === "supplier-bills" ? (
        <SupplierBillKpis />
      ) : (
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <KpiTile label="Left to pay" value={canRead ? kpiValue(s?.outstanding) : null} hint={kpiHint(s?.outstanding, "invoice(s)")} />
        <KpiTile
          label="Overdue"
          value={canRead ? kpiValue(s?.overdue) : null}
          hint={kpiHint(s?.overdue, "invoice(s)")}
          hintTone={s && s.overdue.count ? "danger" : "neutral"}
        />
        <KpiTile label="Received this month" value={canRead ? kpiValue(s?.received_this_month) : null} hint={s ? `verified, net of refunds, since ${formatDay(s.month_start)}` : undefined} />
        <KpiTile
          label="Waiting for verification"
          value={canRead ? (s ? s.awaiting_verification.count : undefined) : null}
          hint={s && s.awaiting_verification.count ? kpiHint(s.awaiting_verification, "payment(s)") : "Nothing waiting"}
          hintTone={s && s.awaiting_verification.count ? "warning" : "neutral"}
        />
      </div>
      )}
      <Tabs
        tabs={tabs}
        active={tab}
        onChange={(k) => setMany({ tab: k === "invoices" ? null : k, q: null, bill: null })}
      />
      {tab === "invoices" && <InvoicesTab canRead={canRead} />}
      {tab === "payments" && <PaymentsTab canRead={canRead} />}
      {tab === "supplier-bills" && <SupplierBillsList />}
    </PageFrame>
  );
}

function InvoicesTab({ canRead }: { canRead: boolean }) {
  const { get } = useUrlFilters();
  const paging = useOffsetPage();
  const query = {
    status: get("status") || undefined,
    overdue: get("due") === "overdue" ? true : undefined,
    q: get("q") || undefined,
    limit: paging.limit,
    offset: paging.offset,
  };
  const list = useQuery({ queryKey: ["billing", "invoices", query], queryFn: () => billingApi.invoices(query), enabled: canRead });
  const router = useRouter();
  const toast = useToast();
  const refresh = useBillingRefresh();
  const [cancelling, setCancelling] = useState<InvoiceSummary | null>(null);
  const cancel = useMutation({
    mutationFn: ({ inv, reason }: { inv: InvoiceSummary; reason: string }) => billingApi.cancel(inv.invoice_number, reason),
    onSuccess: (_, { inv }) => {
      toast.success(`${inv.invoice_number} cancelled`);
      setCancelling(null);
      refresh();
    },
  });
  function menuFor(r: InvoiceSummary): RowMenuItem[] {
    const items: RowMenuItem[] = [
      { label: "View invoice", icon: <Eye size={15} />, onSelect: () => router.push(`/billing/invoices/${encodeURIComponent(r.invoice_number)}`) },
      { label: "Open trip case", icon: <FolderOpen size={15} />, onSelect: () => router.push(`/cases/${encodeURIComponent(r.trip_case.code)}`) },
    ];
    if (r.actions?.cancel) {
      items.push({ label: "Cancel invoice", icon: <Ban size={15} />, onSelect: () => setCancelling(r), danger: true, divider: true });
    }
    return items;
  }
  const columns: ColumnDef<InvoiceSummary, unknown>[] = [
    ...INVOICE_COLUMNS,
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => <RowMenu items={menuFor(row.original)} label={`Actions for ${row.original.invoice_number}`} />,
    },
  ];
  return (
    <Section>
      <div className="bk-dt-sticky-end">
      <DataTable<InvoiceSummary>
        filters={
          <FilterBar
            search={{ placeholder: "Search invoice, trip case or customer" }}
            selects={[
              {
                key: "status",
                label: "Status",
                allLabel: "All statuses",
                options: [
                  { value: "open", label: "Open (sent, part paid)" },
                  { value: "draft", label: "Draft" },
                  { value: "sent", label: "Sent" },
                  { value: "partially_paid", label: "Partially paid" },
                  { value: "paid", label: "Paid" },
                  { value: "cancelled", label: "Cancelled" },
                ],
                width: 190,
              },
              { key: "due", label: "Due", allLabel: "Any", options: [{ value: "overdue", label: "Overdue" }], width: 120 },
            ]}
          />
        }
        searchable={false}
        tableId="billing-invoices"
        csv={INVOICE_CSV}
        enableCsv
        csvFilename="invoices.csv"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? (list.error instanceof ApiError ? list.error.message : "Could not load the invoices") : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle={canRead ? "No invoice matches" : "Invoices need the billing role (admin, sales)"}
        emptyDescription="Create an invoice from a confirmed trip case: open it, then its Billing tab."
        tableMinWidth={1000}
      />
      </div>
      <ConfirmDialog
        open={!!cancelling}
        onClose={() => setCancelling(null)}
        onConfirm={(reason) => cancelling && cancel.mutate({ inv: cancelling, reason })}
        title={cancelling ? `Cancel ${cancelling.invoice_number}` : "Cancel invoice"}
        confirmLabel="Cancel invoice"
        danger
        busy={cancel.isPending}
        error={cancel.isError ? errorText(cancel.error) : null}
        reason={{ label: "Why is it cancelled?", required: true }}
        message="Only an invoice with nothing paid can be cancelled: refund verified payments first. The customer's My Trips feed says it was cancelled."
      />
    </Section>
  );
}

function PaymentsTab({ canRead }: { canRead: boolean }) {
  const { get } = useUrlFilters();
  const paging = useOffsetPage();
  const query = {
    state: get("state") || undefined,
    kind: get("kind") || undefined,
    method: get("method") || undefined,
    q: get("q") || undefined,
    limit: paging.limit,
    offset: paging.offset,
  };
  const list = useQuery({ queryKey: ["billing", "payments", query], queryFn: () => billingApi.payments(query), enabled: canRead });
  const router = useRouter();
  const toast = useToast();
  const refresh = useBillingRefresh();
  const [voiding, setVoiding] = useState<Payment | null>(null);
  const download = useMutation({
    mutationFn: (fileId: string) => billingApi.download(fileId),
    onSuccess: (r) => window.open(r.url, "_blank", "noopener"),
    onError: (e) => toast.error(errorText(e)),
  });
  function menuFor(p: Payment): RowMenuItem[] {
    const items: RowMenuItem[] = [
      { label: "View payment", icon: <Eye size={15} />, onSelect: () => router.push(`/billing/payments/${encodeURIComponent(p.ref)}`) },
      { label: "View invoice", icon: <FileText size={15} />, onSelect: () => router.push(`/billing/invoices/${encodeURIComponent(p.invoice.number)}`) },
    ];
    // The evidence file (bank slip, receipt): list rows carry its id but no `files`; a 5-minute download link.
    const fileId = paymentEvidenceFileId(p);
    if (fileId) items.push({ label: "Download evidence", icon: <Download size={15} />, onSelect: () => download.mutate(fileId) });
    if (p.actions.void) items.push({ label: "Void", icon: <Ban size={15} />, onSelect: () => setVoiding(p), danger: true, divider: true });
    return items;
  }
  const columns: ColumnDef<Payment, unknown>[] = [
    ...PAYMENT_COLUMNS,
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => <RowMenu items={menuFor(row.original)} label={`Actions for ${row.original.ref}`} />,
    },
  ];
  return (
    <Section>
      <div className="bk-dt-sticky-end">
      <DataTable<Payment>
        filters={
          <FilterBar
            search={{ placeholder: "Search payment, invoice, case or reference" }}
            selects={[
              {
                key: "state",
                label: "Status",
                allLabel: "All statuses",
                options: [
                  { value: "recorded", label: "To verify" },
                  { value: "verified", label: "Verified" },
                  { value: "voided", label: "Voided" },
                ],
                width: 150,
              },
              {
                key: "kind",
                label: "Type",
                allLabel: "Received and refunds",
                options: [
                  { value: "receipt", label: "Received" },
                  { value: "refund", label: "Refunds" },
                ],
                width: 190,
              },
              {
                key: "method",
                label: "Method",
                allLabel: "Any method",
                options: Object.entries(PAYMENT_METHOD_LABEL).map(([value, label]) => ({ value, label })),
                width: 190,
              },
            ]}
          />
        }
        searchable={false}
        tableId="billing-payments"
        enableCsv
        csvFilename="payments.csv"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? (list.error instanceof ApiError ? list.error.message : "Could not load the payments") : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle={canRead ? "No payment matches" : "Payments need the billing role (admin, sales)"}
        emptyDescription="Payments are recorded on an invoice."
        tableMinWidth={960}
      />
      </div>
      <VoidPaymentDialog payment={voiding} onClose={() => setVoiding(null)} onDone={refresh} />
    </Section>
  );
}
