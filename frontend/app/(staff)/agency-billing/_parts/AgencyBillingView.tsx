"use client";
// app/(staff)/agency-billing/_parts/AgencyBillingView.tsx — Operations → Agency billing (AA-1037 R4, bk-041 / bk-042;
// root ADR 0019 d9, b14 / b16 / b17). Every invoice from AA to an agency: the software fee (AAS- numbers) and the tour
// settlement (AAT- numbers). Four tabs (URL key `tab`):
//   Invoices   GET /agency-invoices (kind, status, q), paged on the server; "Run drafts" queues a draft job for a month.
//   Payments   GET /agency-payments: payments waiting for the second person (approve / reject; never the recorder).
//   Agencies   GET /agency-billing/status: each agency's billing status (the view CIS reads to warn and suspend).
//   Settings   the issuing billing entity, tax rules and payment terms (data rows, never code; BO-15..17).
// Admin and finance only (lib/rbac.ts); write actions show only with the matching permission.

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  AGENCY_PAYMENT_METHOD_LABEL,
  KIND_LABEL,
  agencyBillingApi,
  overdueText,
  periodLabel,
  type AgencyInvoice,
  type AgencyPayment,
  type BillingStatusRow,
  type InvoiceKind,
} from "@/lib/api/agencyBilling";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  Button,
  ConfirmDialog,
  DataTable,
  DateText,
  Field,
  Input,
  KpiTile,
  Modal,
  Money,
  PageFrame,
  Section,
  Select,
  useCan,
  useStaff,
  useToast,
  useUrlFilters,
} from "@/ui";
import { SavedTabs } from "../../_parts/SavedTabs";
import { BillingSettings } from "./BillingSettings";
import { HealthPill, InvoiceStatusPill, PaymentStatusPill, errorText, thisMonth } from "./shared";

const TABS = [
  { key: "invoices", label: "Invoices" },
  { key: "payments", label: "Payments to approve" },
  { key: "agencies", label: "Agencies" },
  { key: "settings", label: "Settings" },
];

export function AgencyBillingView() {
  const { get } = useUrlFilters();
  const tab = get("tab") || "invoices";
  const summary = useQuery({ queryKey: ["agency-billing", "summary"], queryFn: agencyBillingApi.summary });
  const usd = summary.data?.by_currency.find((r) => r.currency === "USD");
  const loading = summary.isLoading ? undefined : null;
  return (
    <PageFrame
      title="Agency billing"
      description="Every invoice from AA to an agency: the software fee and the tour settlement. Drafts come from the monthly jobs; finance issues them, records payments and a second person approves each payment."
    >
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <KpiTile label="Drafts to issue" value={summary.data ? String(summary.data.by_currency.reduce((n, r) => n + r.drafts, 0)) : loading} />
        <KpiTile label="Open (USD)" value={usd ? <Money amount={usd.open} currency="USD" /> : loading} />
        <KpiTile label="Overdue (USD)" value={usd ? <Money amount={usd.overdue} currency="USD" /> : loading} valueTone={usd && Number(usd.overdue) > 0 ? "danger" : undefined} />
        <KpiTile label="Waiting for approval (USD)" value={usd ? <Money amount={usd.waiting} currency="USD" /> : loading} />
      </div>
      <SavedTabs tabs={TABS} ariaLabel="Agency billing" />
      {tab === "invoices" && <InvoicesTab />}
      {tab === "payments" && <PaymentsTab />}
      {tab === "agencies" && <AgenciesTab />}
      {tab === "settings" && <BillingSettings />}
    </PageFrame>
  );
}

// ── Invoices ─────────────────────────────────────────────────────────────────

const INVOICE_COLUMNS: ColumnDef<AgencyInvoice, unknown>[] = [
  {
    id: "number",
    header: "Invoice",
    cell: ({ row }) => (
      <Link href={`/agency-billing/invoices/${row.original.number ?? row.original.id}`} className="font-semibold whitespace-nowrap">
        {row.original.number ?? "Draft"}
      </Link>
    ),
  },
  { id: "agency", header: "Agency", cell: ({ row }) => row.original.agency_name },
  { id: "kind", header: "Kind", cell: ({ row }) => KIND_LABEL[row.original.invoice_kind] },
  { id: "period", header: "Period", cell: ({ row }) => <span className="whitespace-nowrap">{periodLabel(row.original.period_start)}</span> },
  {
    id: "total",
    header: "Total",
    cell: ({ row }) => <Money amount={row.original.total} currency={row.original.currency} strong />,
  },
  {
    id: "balance",
    header: "Left to pay",
    cell: ({ row }) => (row.original.status === "issued" ? <Money amount={row.original.balance} currency={row.original.currency} /> : "—"),
  },
  { id: "due", header: "Due", cell: ({ row }) => (row.original.due_date ? <DateText value={row.original.due_date} /> : "—") },
  {
    id: "status",
    header: "Status",
    cell: ({ row }) => <InvoiceStatusPill status={row.original.status} overdueDays={row.original.overdue_days} />,
  },
];

function InvoicesTab() {
  const { get, set } = useUrlFilters();
  const kind = get("kind");
  const status = get("status");
  const q = get("q");
  const paging = useOffsetPage();
  const canWrite = useCan("agency_billing.write");
  const [runOpen, setRunOpen] = useState(false);
  const list = useQuery({
    queryKey: ["agency-billing", "invoices", kind, status, q, paging.pageIndex, paging.pageSize],
    queryFn: () =>
      agencyBillingApi.invoices({ kind, status: status || "all", q, limit: paging.pageSize, offset: paging.pageIndex * paging.pageSize }),
  });
  return (
    <Section
      title="Invoices"
      actions={canWrite ? <Button variant="primary" onClick={() => setRunOpen(true)}>Run drafts</Button> : undefined}
    >
      <div className="mb-3 flex flex-wrap gap-3">
        <Field label="Kind">
          <Select
            value={kind}
            onChange={(e) => set("kind", e.target.value || null)}
            options={[{ value: "", label: "All kinds" }, { value: "saas", label: KIND_LABEL.saas }, { value: "settlement", label: KIND_LABEL.settlement }]}
          />
        </Field>
        <Field label="Status">
          <Select
            value={status}
            onChange={(e) => set("status", e.target.value || null)}
            options={[
              { value: "", label: "All" },
              { value: "draft", label: "Draft" },
              { value: "issued", label: "Issued" },
              { value: "overdue", label: "Overdue" },
              { value: "waiting", label: "Payment waiting" },
              { value: "paid", label: "Paid" },
              { value: "void", label: "Void" },
            ]}
          />
        </Field>
        <Field label="Search">
          <Input defaultValue={q} placeholder="Number, agency or e-invoice number" onBlur={(e) => set("q", e.target.value.trim() || null)} />
        </Field>
      </div>
      <DataTable<AgencyInvoice>
        searchable={false}
        tableId="agency-invoices"
        data={list.data?.items ?? []}
        columns={INVOICE_COLUMNS}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle="No agency invoices here"
        emptyDescription="Drafts appear when the monthly jobs run (software fee at the start of a month, settlement for the closed month), or when finance runs them for a month."
        tableMinWidth={980}
      />
      <RunDraftsModal open={runOpen} onClose={() => setRunOpen(false)} />
    </Section>
  );
}

function RunDraftsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [kind, setKind] = useState<InvoiceKind>("saas");
  const [period, setPeriod] = useState(thisMonth());
  const run = useMutation({
    mutationFn: () => agencyBillingApi.run({ kind, period }),
    onSuccess: (r) => {
      toast.success(`${KIND_LABEL[kind]} drafts for ${r.period} are queued; they appear in a minute`);
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
      onClose();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Run invoice drafts"
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={run.isPending || !/^\d{4}-\d{2}$/.test(period)} onClick={() => run.mutate()}>
            {run.isPending ? "Queuing…" : "Run"}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label="Kind">
          <Select
            value={kind}
            onChange={(e) => setKind(e.target.value as InvoiceKind)}
            options={[{ value: "saas", label: KIND_LABEL.saas }, { value: "settlement", label: KIND_LABEL.settlement }]}
          />
        </Field>
        <Field label="Month" hint="YYYY-MM. Drafts are idempotent: running a month again only adds what is missing.">
          <Input value={period} onChange={(e) => setPeriod(e.target.value)} placeholder="2026-11" />
        </Field>
      </div>
    </Modal>
  );
}

// ── Payments ─────────────────────────────────────────────────────────────────

function PaymentsTab() {
  const { get, set } = useUrlFilters();
  const state = get("state") || "recorded";
  const paging = useOffsetPage();
  const me = useStaff();
  const canApprove = useCan("agency_payment.approve");
  const toast = useToast();
  const qc = useQueryClient();
  const [rejecting, setRejecting] = useState<AgencyPayment | null>(null);
  const list = useQuery({
    queryKey: ["agency-billing", "payments", state, paging.pageIndex, paging.pageSize],
    queryFn: () => agencyBillingApi.payments({ state, limit: paging.pageSize, offset: paging.pageIndex * paging.pageSize }),
  });
  const approve = useMutation({
    mutationFn: (p: AgencyPayment) => agencyBillingApi.approve(p.id),
    onSuccess: () => {
      toast.success("Payment approved");
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const reject = useMutation({
    mutationFn: ({ p, reason }: { p: AgencyPayment; reason: string }) => agencyBillingApi.reject(p.id, reason),
    onSuccess: () => {
      toast.success("Payment rejected");
      setRejecting(null);
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const columns: ColumnDef<AgencyPayment, unknown>[] = [
    {
      id: "invoice",
      header: "Invoice",
      cell: ({ row }) => (
        <Link href={`/agency-billing/invoices/${row.original.invoice_number ?? row.original.invoice_id}`} className="font-semibold whitespace-nowrap">
          {row.original.invoice_number}
        </Link>
      ),
    },
    { id: "agency", header: "Agency", cell: ({ row }) => row.original.agency_name },
    { id: "amount", header: "Amount", cell: ({ row }) => <Money amount={row.original.amount} currency={row.original.currency} strong /> },
    { id: "received", header: "Received", cell: ({ row }) => <DateText value={row.original.received_on} /> },
    { id: "method", header: "Method", cell: ({ row }) => AGENCY_PAYMENT_METHOD_LABEL[row.original.method] },
    { id: "document", header: "Evidence", cell: ({ row }) => row.original.document_ref },
    { id: "by", header: "Recorded by", cell: ({ row }) => row.original.recorded_by },
    { id: "status", header: "Status", cell: ({ row }) => <PaymentStatusPill status={row.original.status} /> },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => {
        const p = row.original;
        if (!canApprove || p.status !== "recorded") return null;
        if (p.recorded_by === me.cis_user_id) return <span className="text-[12px] text-muted">Another person approves</span>;
        return (
          <span className="flex gap-2">
            <Button size="sm" variant="primary" disabled={approve.isPending} onClick={() => approve.mutate(p)}>Approve</Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(p)}>Reject</Button>
          </span>
        );
      },
    },
  ];
  return (
    <Section title="Payments">
      <div className="mb-3 flex gap-3">
        <Field label="Status">
          <Select
            value={state}
            onChange={(e) => set("state", e.target.value)}
            options={[
              { value: "recorded", label: "Waiting for approval" },
              { value: "approved", label: "Approved" },
              { value: "rejected", label: "Rejected" },
              { value: "all", label: "All" },
            ]}
          />
        </Field>
      </div>
      <DataTable<AgencyPayment>
        searchable={false}
        tableId="agency-payments"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        onRetry={() => list.refetch()}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
        emptyTitle="No payments here"
        emptyDescription="A payment is recorded on an issued invoice; it counts only once a second person approves it."
        tableMinWidth={1100}
      />
      <ConfirmDialog
        open={rejecting !== null}
        onClose={() => setRejecting(null)}
        onConfirm={(reason) => rejecting && reject.mutate({ p: rejecting, reason })}
        title="Reject payment"
        message="The payment will not count. Say why (for example: not on the bank statement)."
        confirmLabel="Reject"
        destructive
        reason={{ label: "Reason", required: true, maxLength: 500 }}
        busy={reject.isPending}
      />
    </Section>
  );
}

// ── Agencies ─────────────────────────────────────────────────────────────────

const STATUS_COLUMNS: ColumnDef<BillingStatusRow, unknown>[] = [
  { id: "agency", header: "Agency", cell: ({ row }) => row.original.agency_name },
  { id: "status", header: "Billing status", cell: ({ row }) => <HealthPill status={row.original.status} /> },
  { id: "oldest", header: "Oldest overdue", cell: ({ row }) => (row.original.oldest_overdue_days ? `${row.original.oldest_overdue_days} days` : "—") },
  { id: "invoices", header: "Overdue invoices", cell: ({ row }) => row.original.overdue_invoices || "—" },
  { id: "amount", header: "Overdue amount", cell: ({ row }) => overdueText(row.original.overdue_amount) },
  { id: "paid", header: "Software paid through", cell: ({ row }) => (row.original.paid_through ? <DateText value={row.original.paid_through} /> : "—") },
  {
    id: "terms",
    header: "Warn / suspend after",
    cell: ({ row }) => `${row.original.warn_after_days ?? "—"} / ${row.original.suspend_after_days ?? "—"} days`,
  },
];

function AgenciesTab() {
  const list = useQuery({ queryKey: ["agency-billing", "status"], queryFn: agencyBillingApi.status });
  return (
    <Section
      title="Agencies"
      description="CIS reads this status every hour: a warning moves the agency's account to its grace state and rings its bell; past the suspend days the account is suspended until the money is approved here."
    >
      <DataTable<BillingStatusRow>
        searchable={false}
        tableId="agency-billing-status"
        data={list.data?.items ?? []}
        columns={STATUS_COLUMNS}
        getRowId={(r) => r.tenant_id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        onRetry={() => list.refetch()}
        emptyTitle="No agencies yet"
        emptyDescription="Every CIS tenant gets a partner row from the tenant projection; they show here."
        tableMinWidth={900}
      />
    </Section>
  );
}
