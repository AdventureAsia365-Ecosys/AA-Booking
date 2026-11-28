"use client";
// app/(staff)/agency-billing/_parts/AgencyBillingView.tsx — Operations → Agency billing (AA-1037 R4, bk-041 / bk-042;
// root ADR 0019 d9, b14 / b16 / b17). Every invoice from AA to an agency: the software fee (AAS- numbers) and the tour
// settlement (AAT- numbers). Four tabs (URL key `tab`):
//   Invoices   GET /agency-invoices (kind, status, q, tenant_id), paged on the server; "Run drafts" queues a draft job
//              for a month; "Export for accountant" downloads the invoices or payments of a period as CSV (AA-1037
//              extras G5). `?tenant=<CIS tenant uuid>` (the CIS Tenant 360 "Open in Booking" link) shows one agency's
//              invoices, with a removable chip naming the agency (ui/booking/agencyFilter.ts).
//   Payments   GET /agency-payments: payments waiting for the second person (approve / reject; never the recorder).
//              AA-1159: `?approvable=me` keeps the payments the viewer did not record (the My work tile opens it).
//   Agencies   GET /agency-billing/status: each agency's billing status (the view CIS reads to warn and suspend).
//   Settings   the issuing billing entity, tax rules and payment terms (data rows, never code; BO-15..17).
// Admin and finance only (lib/rbac.ts); write actions show only with the matching permission.

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { X } from "lucide-react";
import {
  AGENCY_PAYMENT_METHOD_LABEL,
  KIND_LABEL,
  agencyBillingApi,
  overdueText,
  periodLabel,
  saveBlob,
  type ExportWhat,
  type AgencyInvoice,
  type AgencyPayment,
  type BillingStatusRow,
  type InvoiceKind,
  paymentRecorder,
} from "@/lib/api/agencyBilling";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import { agencyFilterLabel, tenantFilter } from "@/ui/booking/agencyFilter";
import {
  Button,
  ChipButton,
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
import { useLang, useT } from "@/ui/core/useI18n";
import { Phrase } from "@/ui/local/Phrase";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./AgencyBillingView.messages";
import { useSelfApproval } from "@/lib/useSelfApproval";

const TABS = [
  { key: "invoices", label: "Invoices" },
  { key: "payments", label: "Payments to approve" },
  { key: "agencies", label: "Agencies" },
  { key: "settings", label: "Settings" },
];

export function AgencyBillingView() {
  const t = useT(MSG);
  const { get } = useUrlFilters();
  const tab = get("tab") || "invoices";
  const summary = useQuery({ queryKey: ["agency-billing", "summary"], queryFn: agencyBillingApi.summary });
  // AA-1116: agencies are billed in USD or VND (their CIS billing currency): each money tile lists every currency.
  const rows = summary.data?.by_currency ?? [];
  const loading = summary.isLoading ? undefined : null;
  // AA-1191: a currency the platform has not enabled shows only when this tile has an amount in it.
  const enabled = summary.data?.enabled_currencies;
  const perCurrency = (pick: (r: (typeof rows)[number]) => string) => {
    const shown = rows.filter((r) => !enabled || enabled.includes(r.currency) || Number(pick(r)) !== 0);
    return summary.data ? (
      shown.length ? (
        <span className="flex flex-col">
          {shown.map((r) => (
            <Money key={r.currency} amount={pick(r)} currency={r.currency} />
          ))}
        </span>
      ) : (
        <Money amount="0" currency="USD" />
      )
    ) : (
      loading
    );
  };
  const overdue = rows.some((r) => Number(r.overdue) > 0);
  return (
    <PageFrame
      title={t("Agency billing")}
      description={t("Every invoice from AA to an agency: the software fee and the tour settlement. Drafts come from the monthly jobs; finance issues them, records payments and a second person approves each payment.")}
    >
      {/* One column on a phone, like the billing KPIs: a money value at the tile size is wider than half of 390 px. */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile label={t("Drafts to issue")} value={summary.data ? String(summary.data.by_currency.reduce((n, r) => n + r.drafts, 0)) : loading} />
        <KpiTile label={t("Open")} value={perCurrency((r) => r.open)} />
        <KpiTile label={t("Overdue")} value={perCurrency((r) => r.overdue)} valueTone={overdue ? "danger" : undefined} />
        <KpiTile label={t("Waiting for approval")} value={perCurrency((r) => r.waiting)} />
      </div>
      <SavedTabs tabs={TABS} ariaLabel={t("Agency billing")} />
      {tab === "invoices" && <InvoicesTab />}
      {tab === "payments" && <PaymentsTab />}
      {tab === "agencies" && <AgenciesTab />}
      {tab === "settings" && <BillingSettings />}
    </PageFrame>
  );
}

// ── Invoices ─────────────────────────────────────────────────────────────────

/** The invoice period ("November 2026") in the UI language, for the module-level columns. */
function PeriodText({ start }: { start: string }) {
  const lang = useLang();
  return <span className="whitespace-nowrap">{periodLabel(start, lang)}</span>;
}

const INVOICE_COLUMNS: ColumnDef<AgencyInvoice, unknown>[] = [
  {
    id: "number",
    header: "Invoice",
    cell: ({ row }) => (
      <Link href={`/agency-billing/invoices/${row.original.number ?? row.original.id}`} className="font-semibold whitespace-nowrap">
        {row.original.number ?? <Phrase text="Draft" />}
      </Link>
    ),
  },
  { id: "agency", header: "Agency", cell: ({ row }) => row.original.agency_name },
  { id: "kind", header: "Kind", cell: ({ row }) => <Phrase text={KIND_LABEL[row.original.invoice_kind]} /> },
  { id: "period", header: "Period", cell: ({ row }) => <PeriodText start={row.original.period_start} /> },
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
  const t = useT(MSG);
  const tx = useTx();
  const { get, set } = useUrlFilters();
  const kind = get("kind");
  const status = get("status");
  const q = get("q");
  const tenant = tenantFilter(get("tenant"));
  const paging = useOffsetPage();
  const canWrite = useCan("agency_billing.write");
  const [runOpen, setRunOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const list = useQuery({
    queryKey: ["agency-billing", "invoices", kind, status, q, tenant, paging.pageIndex, paging.pageSize],
    queryFn: () =>
      agencyBillingApi.invoices({
        kind,
        status: status || "all",
        tenant_id: tenant ?? undefined,
        q,
        limit: paging.pageSize,
        offset: paging.pageIndex * paging.pageSize,
      }),
  });
  // The chip's agency name: every partner has a billing status row (the Agencies tab reads the same query).
  const agencies = useQuery({ queryKey: ["agency-billing", "status"], queryFn: agencyBillingApi.status, enabled: tenant !== null });
  const agencyName = tenant ? agencyFilterLabel(tenant, agencies.data?.items, list.data?.items) : "";
  return (
    <Section
      title={t("Invoices")}
      actions={
        <span className="flex flex-wrap gap-2">
          <Button variant="ghost" onClick={() => setExportOpen(true)}>{t("Export for accountant")}</Button>
          {canWrite && <Button variant="primary" onClick={() => setRunOpen(true)}>{t("Run drafts")}</Button>}
        </span>
      }
    >
      <div className="mb-3 flex flex-wrap gap-3">
        <Field label={t("Kind")}>
          <Select
            value={kind}
            onChange={(e) => set("kind", e.target.value || null)}
            options={[{ value: "", label: t("All kinds") }, { value: "saas", label: tx(KIND_LABEL.saas) }, { value: "settlement", label: tx(KIND_LABEL.settlement) }]}
          />
        </Field>
        <Field label={t("Status")}>
          <Select
            value={status}
            onChange={(e) => set("status", e.target.value || null)}
            options={[
              { value: "", label: t("All") },
              { value: "draft", label: t("Draft") },
              { value: "issued", label: t("Issued") },
              { value: "overdue", label: t("Overdue") },
              { value: "waiting", label: t("Payment waiting") },
              { value: "paid", label: t("Paid") },
              { value: "void", label: t("Void") },
            ]}
          />
        </Field>
        <Field label={t("Search")}>
          <Input defaultValue={q} placeholder={t("Number, agency or e-invoice number")} onBlur={(e) => set("q", e.target.value.trim() || null)} />
        </Field>
        {tenant && (
          <Field label={t("Agency")} as="div">
            <ChipButton selected ariaLabel={t("{0}: remove the agency filter", agencyName)} title={t("Show every agency")} onClick={() => set("tenant", null)}>
              <span className="block max-w-[220px] truncate">{agencyName}</span>
              <X size={14} aria-hidden="true" />
            </ChipButton>
          </Field>
        )}
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
        emptyTitle={t("No agency invoices here")}
        emptyDescription={t("Drafts appear when the monthly jobs run (software fee at the start of a month, settlement for the closed month), or when finance runs them for a month.")}
        tableMinWidth={980}
      />
      <RunDraftsModal open={runOpen} onClose={() => setRunOpen(false)} />
      <ExportModal open={exportOpen} onClose={() => setExportOpen(false)} />
    </Section>
  );
}

function monthBounds(): { from: string; to: string } {
  const now = new Date();
  const first = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
  const last = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 0));
  return { from: first.toISOString().slice(0, 10), to: last.toISOString().slice(0, 10) };
}

// AA-1037 extras G5: one CSV of the invoices issued, or the payments received, in a period (at most 366 days).
function ExportModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT(MSG);
  const toast = useToast();
  const [what, setWhat] = useState<ExportWhat>("invoices");
  const [range, setRange] = useState(monthBounds);
  const run = useMutation({
    mutationFn: () => agencyBillingApi.exportCsv(what, range.from, range.to),
    onSuccess: (blob) => {
      saveBlob(blob, `agency-${what}-${range.from}_${range.to}.csv`);
      onClose();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const valid = /^\d{4}-\d{2}-\d{2}$/.test(range.from) && /^\d{4}-\d{2}-\d{2}$/.test(range.to) && range.from <= range.to;
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("Export for the accountant")}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>{t("Cancel")}</Button>
          <Button variant="primary" disabled={run.isPending || !valid} onClick={() => run.mutate()}>
            {run.isPending ? t("Preparing…") : t("Download CSV")}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label={t("What")} hint={t("Invoices: issued in the period (paid and void included, no drafts). Payments: received in the period, every status.")}>
          <Select
            value={what}
            onChange={(e) => setWhat(e.target.value as ExportWhat)}
            options={[{ value: "invoices", label: t("Invoices") }, { value: "payments", label: t("Payments") }]}
          />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label={t("From")}>
            <Input type="date" value={range.from} onChange={(e) => setRange({ ...range, from: e.target.value })} />
          </Field>
          <Field label={t("To (included)")}>
            <Input type="date" value={range.to} onChange={(e) => setRange({ ...range, to: e.target.value })} />
          </Field>
        </div>
      </div>
    </Modal>
  );
}

function RunDraftsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const toast = useToast();
  const qc = useQueryClient();
  const [kind, setKind] = useState<InvoiceKind>("saas");
  const [period, setPeriod] = useState(thisMonth());
  const run = useMutation({
    mutationFn: () => agencyBillingApi.run({ kind, period }),
    onSuccess: (r) => {
      toast.success(t("{0} drafts for {1} are queued; they appear in a minute", tx(KIND_LABEL[kind]), r.period));
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
      onClose();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("Run invoice drafts")}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>{t("Cancel")}</Button>
          <Button variant="primary" disabled={run.isPending || !/^\d{4}-\d{2}$/.test(period)} onClick={() => run.mutate()}>
            {run.isPending ? t("Queuing…") : t("Run")}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label={t("Kind")}>
          <Select
            value={kind}
            onChange={(e) => setKind(e.target.value as InvoiceKind)}
            options={[{ value: "saas", label: tx(KIND_LABEL.saas) }, { value: "settlement", label: tx(KIND_LABEL.settlement) }]}
          />
        </Field>
        <Field label={t("Month")} hint={t("YYYY-MM. Drafts are idempotent: running a month again only adds what is missing.")}>
          <Input value={period} onChange={(e) => setPeriod(e.target.value)} placeholder="2026-11" />
        </Field>
      </div>
    </Modal>
  );
}

// ── Payments ─────────────────────────────────────────────────────────────────

function PaymentsTab() {
  const t = useT(MSG);
  const tx = useTx();
  const { get, set } = useUrlFilters();
  const state = get("state") || "recorded";
  // AA-1159: "me" = only the payments I may approve (not recorded by me), the view the My work tile counts.
  const approvable = get("approvable") === "me" ? "me" : undefined;
  const paging = useOffsetPage();
  const me = useStaff();
  const self = useSelfApproval(); // AA-1158
  const canApprove = useCan("agency_payment.approve");
  const toast = useToast();
  const qc = useQueryClient();
  const [rejecting, setRejecting] = useState<AgencyPayment | null>(null);
  const list = useQuery({
    queryKey: ["agency-billing", "payments", state, approvable, paging.pageIndex, paging.pageSize],
    queryFn: () =>
      agencyBillingApi.payments({ state, approvable, limit: paging.pageSize, offset: paging.pageIndex * paging.pageSize }),
  });
  const approve = useMutation({
    mutationFn: (p: AgencyPayment) => agencyBillingApi.approve(p.id),
    onSuccess: () => {
      toast.success(t("Payment approved"));
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const reject = useMutation({
    mutationFn: ({ p, reason }: { p: AgencyPayment; reason: string }) => agencyBillingApi.reject(p.id, reason),
    onSuccess: () => {
      toast.success(t("Payment rejected"));
      setRejecting(null);
      qc.invalidateQueries({ queryKey: ["agency-billing"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const columns: ColumnDef<AgencyPayment, unknown>[] = [
    {
      id: "invoice",
      header: t("Invoice"),
      cell: ({ row }) => (
        <Link href={`/agency-billing/invoices/${row.original.invoice_number ?? row.original.invoice_id}`} className="font-semibold whitespace-nowrap">
          {row.original.invoice_number}
        </Link>
      ),
    },
    { id: "agency", header: t("Agency"), cell: ({ row }) => row.original.agency_name },
    { id: "amount", header: t("Amount"), cell: ({ row }) => <Money amount={row.original.amount} currency={row.original.currency} strong /> },
    { id: "received", header: t("Received"), cell: ({ row }) => <DateText value={row.original.received_on} /> },
    { id: "method", header: t("Method"), cell: ({ row }) => tx(AGENCY_PAYMENT_METHOD_LABEL[row.original.method]) },
    { id: "document", header: t("Evidence"), cell: ({ row }) => row.original.document_ref },
    { id: "by", header: t("Recorded by"), cell: ({ row }) => paymentRecorder(row.original) },
    { id: "status", header: t("Status"), cell: ({ row }) => <PaymentStatusPill status={row.original.status} /> },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => {
        const p = row.original;
        if (!canApprove || p.status !== "recorded") return null;
        if (p.recorded_by === me.cis_user_id && !self.approve) return <span className="text-[12px] text-muted">{t("Another person approves")}</span>;
        if (p.recorded_by === me.cis_user_id) {
          // AA-1158: the business owner approves a payment she recorded (flagged); a rejection stays two-person.
          return <Button size="sm" variant="primary" disabled={approve.isPending} title={t("Recorded as a business-owner self-approval")} onClick={() => approve.mutate(p)}>{t("Approve")}</Button>;
        }
        return (
          <span className="flex gap-2">
            <Button size="sm" variant="primary" disabled={approve.isPending} onClick={() => approve.mutate(p)}>{t("Approve")}</Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(p)}>{t("Reject")}</Button>
          </span>
        );
      },
    },
  ];
  return (
    <Section title={t("Payments")}>
      <div className="mb-3 flex gap-3">
        <Field label={t("Status")}>
          <Select
            value={state}
            onChange={(e) => set("state", e.target.value)}
            options={[
              { value: "recorded", label: t("Waiting for approval") },
              { value: "approved", label: t("Approved") },
              { value: "rejected", label: t("Rejected") },
              { value: "all", label: t("All") },
            ]}
          />
        </Field>
        <Field label={t("Recorded by")}>
          <Select
            value={approvable ?? ""}
            onChange={(e) => set("approvable", e.target.value)}
            options={[
              { value: "", label: t("Anyone") },
              { value: "me", label: t("Someone else (I can approve)") },
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
        emptyTitle={t("No payments here")}
        emptyDescription={t("A payment is recorded on an issued invoice; it counts only once a second person approves it.")}
        tableMinWidth={1100}
      />
      <ConfirmDialog
        open={rejecting !== null}
        onClose={() => setRejecting(null)}
        onConfirm={(reason) => rejecting && reject.mutate({ p: rejecting, reason })}
        title={t("Reject payment")}
        message={t("The payment will not count. Say why (for example: not on the bank statement).")}
        confirmLabel={t("Reject")}
        destructive
        reason={{ label: t("Reason"), required: true, maxLength: 500 }}
        busy={reject.isPending}
      />
    </Section>
  );
}

// ── Agencies ─────────────────────────────────────────────────────────────────

const STATUS_COLUMNS: ColumnDef<BillingStatusRow, unknown>[] = [
  { id: "agency", header: "Agency", cell: ({ row }) => row.original.agency_name },
  { id: "status", header: "Billing status", cell: ({ row }) => <HealthPill status={row.original.status} /> },
  { id: "oldest", header: "Oldest overdue", cell: ({ row }) => (row.original.oldest_overdue_days ? <Phrase text={`${row.original.oldest_overdue_days} days`} /> : "—") },
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
  const t = useT(MSG);
  const list = useQuery({ queryKey: ["agency-billing", "status"], queryFn: agencyBillingApi.status });
  return (
    <Section
      title={t("Agencies")}
      description={t("CIS reads this status every hour: a warning moves the agency's account to its grace state and rings its bell; past the suspend days the account is suspended until the money is approved here.")}
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
        emptyTitle={t("No agencies yet")}
        emptyDescription={t("Every CIS tenant gets a partner row from the tenant projection; they show here.")}
        tableMinWidth={900}
      />
    </Section>
  );
}
