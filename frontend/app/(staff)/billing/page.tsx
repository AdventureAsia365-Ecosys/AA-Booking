"use client";
// app/(staff)/billing/page.tsx — Payments & invoices (AA-682 S3, bk-028; S252 design §6, UI spec §3, Figma PAYMENT &
// INVOICE (09)). Four KPIs (left to pay, overdue, received this month = verified money net of refunds, waiting for
// verification), then two tabs: Invoices (GET /booking/invoices) and Payments (GET /booking/payments), paged on the
// server, filters in the URL. Invoices are created from a trip case's Billing tab (they start from its approved
// proposal); payments are recorded on an invoice. billing.read (admin, sales); never in a preview session.
// AA-682 S4 (bk-030): a third tab, Supplier bills (?tab=supplier-bills; bill.read = admin, sales, contracting), with its
// own four KPIs (to approve, to pay, overdue, paid out this month) and the bill drawer. Contracting sees only that tab.

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import { billingApi, PAYMENT_METHOD_LABEL, type InvoiceSummary, type Kpi, type Payment } from "@/lib/api/billing";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { SupplierBillKpis, SupplierBillsList } from "./_parts/SupplierBills";
import {
  DataTable,
  DateText,
  FilterBar,
  InvoiceStatusBadge,
  KpiTile,
  Money,
  PageFrame,
  PaymentStateBadge,
  Section,
  Tabs,
  formatMoney,
  useCan,
  useUrlFilters,
} from "@/ui";

const INVOICE_COLUMNS: ColumnDef<InvoiceSummary, unknown>[] = [
  {
    id: "number",
    header: "Invoice",
    cell: ({ row }) => (
      <span className="flex flex-col">
        <Link
          href={`/billing/invoices/${encodeURIComponent(row.original.invoice_number)}`}
          onClick={(e) => e.stopPropagation()}
          className="text-[13px] font-medium text-accent-deep hover:underline"
        >
          {row.original.invoice_number}
        </Link>
        <span className="text-xs text-muted">{row.original.issued_at ? "Sent" : "Draft"}</span>
      </span>
    ),
  },
  {
    id: "customer",
    header: "Customer",
    cell: ({ row }) => (
      <span className="block max-w-[200px] truncate text-[13px] text-body">{row.original.bill_to_name ?? row.original.customer?.name ?? "—"}</span>
    ),
  },
  {
    id: "case",
    header: "Trip case",
    cell: ({ row }) => (
      <Link
        href={`/cases/${encodeURIComponent(row.original.trip_case.code)}?tab=billing`}
        onClick={(e) => e.stopPropagation()}
        className="text-[13px] text-accent-deep hover:underline"
      >
        {row.original.trip_case.code}
      </Link>
    ),
  },
  {
    id: "issued",
    header: "Issued",
    cell: ({ row }) => (row.original.issued_at ? <DateText value={row.original.issued_at} className="text-[13px]" /> : <span className="text-muted">—</span>),
  },
  {
    id: "due",
    header: "Next due",
    cell: ({ row }) =>
      row.original.next_due_date ? (
        <DateText value={row.original.next_due_date} className={`text-[13px] ${row.original.overdue ? "font-semibold text-danger" : ""}`} />
      ) : (
        <span className="text-muted">—</span>
      ),
  },
  { id: "total", header: "Total", cell: ({ row }) => <Money amount={row.original.total_amount} currency={row.original.currency} className="text-[13px]" /> },
  { id: "paid", header: "Paid", cell: ({ row }) => <Money amount={row.original.net_paid} currency={row.original.currency} className="text-[13px]" /> },
  {
    id: "balance",
    header: "Left to pay",
    cell: ({ row }) => <Money amount={row.original.balance} currency={row.original.currency} strong className="text-[13px]" />,
  },
  { id: "status", header: "Status", cell: ({ row }) => <InvoiceStatusBadge status={row.original.status} overdue={row.original.overdue} /> },
];

const PAYMENT_COLUMNS: ColumnDef<Payment, unknown>[] = [
  {
    id: "ref",
    header: "Payment",
    cell: ({ row }) => (
      <span className="flex flex-col">
        <Link
          href={`/billing/payments/${encodeURIComponent(row.original.ref)}`}
          onClick={(e) => e.stopPropagation()}
          className="text-[13px] font-medium text-accent-deep hover:underline"
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
    cell: ({ row }) => (
      <span className="flex flex-col text-[13px]">
        <Link href={`/billing/invoices/${encodeURIComponent(row.original.invoice.number)}`} onClick={(e) => e.stopPropagation()} className="text-accent-deep hover:underline">
          {row.original.invoice.number}
        </Link>
        <span className="text-xs text-muted">{row.original.trip_case.code}</span>
      </span>
    ),
  },
  { id: "date", header: "Date", cell: ({ row }) => <DateText value={row.original.received_on} className="text-[13px]" /> },
  { id: "method", header: "Method", cell: ({ row }) => <span className="text-[13px] text-body">{PAYMENT_METHOD_LABEL[row.original.method]}</span> },
  {
    id: "amount",
    header: "Amount",
    cell: ({ row }) => (
      <Money amount={row.original.kind === "refund" ? `-${row.original.amount}` : row.original.amount} currency={row.original.currency} strong className="text-[13px]" />
    ),
  },
  {
    id: "who",
    header: "Recorded by",
    cell: ({ row }) => <span className="block max-w-[160px] truncate text-[13px] text-body">{row.original.recorded_by?.name ?? "—"}</span>,
  },
  { id: "state", header: "Status", cell: ({ row }) => <PaymentStateBadge state={row.original.state} /> },
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
        <KpiTile label="Received this month" value={canRead ? kpiValue(s?.received_this_month) : null} hint={s ? `verified, net of refunds, since ${s.month_start}` : undefined} />
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
  return (
    <Section>
      <div className="mb-4">
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
      </div>
      <DataTable<InvoiceSummary>
        searchable={false}
        tableId="billing-invoices"
        data={list.data?.items ?? []}
        columns={INVOICE_COLUMNS}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? (list.error instanceof ApiError ? list.error.message : "Could not load the invoices") : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle={canRead ? "No invoice matches" : "Invoices need the billing role (admin, sales)"}
        emptyDescription="Create an invoice from a confirmed trip case: open it, then its Billing tab."
        tableMinWidth={1100}
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
  return (
    <Section>
      <div className="mb-4">
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
      </div>
      <DataTable<Payment>
        searchable={false}
        tableId="billing-payments"
        data={list.data?.items ?? []}
        columns={PAYMENT_COLUMNS}
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
    </Section>
  );
}
