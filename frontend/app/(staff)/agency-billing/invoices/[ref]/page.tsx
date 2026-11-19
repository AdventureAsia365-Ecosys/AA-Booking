"use client";
// app/(staff)/agency-billing/invoices/[ref]/page.tsx — one agency invoice (AA-1037 R4, bk-041 / bk-042).
// Draft: lines (remove; add an adjustment such as a contract price or a credit), Issue (the next gap-free number, the
// issuer and bill-to copies, the tax rule in force, the due date), Delete. Issued: the legal VAT e-invoice number the
// accountant issued in the e-invoice software (recorded once), Record payment (maker; a document reference is required),
// Void (no approved payment). Payments: approve / reject by a second person (never the recorder). The money state is
// derived by the database: the invoice turns paid when approved payments cover it. Reminders (AA-1037 extras G4): the
// overdue e-mails the daily job sent (or skipped) for this invoice, one per step.

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  AGENCY_PAYMENT_METHOD_LABEL,
  KIND_LABEL,
  LINE_ROLE_LABEL,
  agencyBillingApi,
  periodLabel,
  type AgencyInvoiceDetail,
  type AgencyInvoiceLine,
  type AgencyPayment,
  type AgencyPaymentMethod,
  type InvoiceReminder,
  paymentRecorder,
} from "@/lib/api/agencyBilling";
import {
  Button,
  ConfirmDialog,
  DataTable,
  DateText,
  Field,
  FormDrawer,
  Input,
  Money,
  PageFrame,
  Section,
  Select,
  Textarea,
  useCan,
  useStaff,
  useToast,
} from "@/ui";
import { InvoiceStatusPill, PaymentStatusPill, errorText, newKey } from "../../_parts/shared";
import { useLang, useT } from "@/ui/core/useI18n";
import { Phrase } from "@/ui/local/Phrase";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./page.messages";

export default function AgencyInvoicePage() {
  const t = useT(MSG);
  const { ref } = useParams<{ ref: string }>();
  const q = useQuery({ queryKey: ["agency-billing", "invoice", ref], queryFn: () => agencyBillingApi.invoice(ref) });
  if (q.isLoading) return <PageFrame title={t("Agency invoice")}><p className="text-[13px]">{t("Loading…")}</p></PageFrame>;
  if (q.isError || !q.data) {
    return (
      <PageFrame title={t("Agency invoice")} breadcrumbs={[{ label: t("Settlement"), href: "/agency-billing" }]}>
        <p className="text-[13px]">{q.isError ? errorText(q.error) : t("Not found")}</p>
      </PageFrame>
    );
  }
  return <InvoiceView inv={q.data} />;
}

function InvoiceView({ inv }: { inv: AgencyInvoiceDetail }) {
  const t = useT(MSG);
  const tx = useTx();
  const lang = useLang();
  const router = useRouter();
  const toast = useToast();
  const qc = useQueryClient();
  const canWrite = useCan("agency_billing.write");
  const ref = inv.id;
  const [voidOpen, setVoidOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [payOpen, setPayOpen] = useState(false);
  const [eInvoice, setEInvoice] = useState("");
  const [line, setLine] = useState({ description: "", quantity: "1", unit_price: "" });
  const [due, setDue] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["agency-billing"] });
  const done = (msg: string) => () => {
    toast.success(msg);
    refresh();
  };
  const fail = (e: unknown) => toast.error(errorText(e));
  const issue = useMutation({ mutationFn: () => agencyBillingApi.issue(ref, due || undefined), onSuccess: (r) => {
    toast.success(t("Issued as {0}", r.number));
    refresh();
    if (r.number) router.replace(`/agency-billing/invoices/${r.number}`);
  }, onError: fail });
  const voidIt = useMutation({ mutationFn: (reason: string) => agencyBillingApi.void(ref, reason), onSuccess: () => {
    setVoidOpen(false);
    done(t("Invoice voided"))();
  }, onError: fail });
  const remove = useMutation({ mutationFn: () => agencyBillingApi.deleteDraft(ref), onSuccess: () => {
    toast.success(t("Draft deleted"));
    refresh();
    router.replace("/agency-billing");
  }, onError: fail });
  const eNo = useMutation({ mutationFn: () => agencyBillingApi.patch(ref, { e_invoice_no: eInvoice.trim() }), onSuccess: done(t("E-invoice number recorded")), onError: fail });
  const addLine = useMutation({
    mutationFn: () => agencyBillingApi.addLine(ref, { description: line.description.trim(), quantity: line.quantity || "1", unit_price: line.unit_price }),
    onSuccess: () => {
      setLine({ description: "", quantity: "1", unit_price: "" });
      done(t("Line added"))();
    },
    onError: fail,
  });
  const removeLine = useMutation({ mutationFn: (n: number) => agencyBillingApi.removeLine(ref, n), onSuccess: done(t("Line removed")), onError: fail });
  const draft = inv.status === "draft";
  const lineColumns: ColumnDef<AgencyInvoiceLine, unknown>[] = [
    { id: "no", header: "#", cell: ({ row }) => row.original.line_no },
    { id: "role", header: t("Type"), cell: ({ row }) => tx(LINE_ROLE_LABEL[row.original.line_role]) ?? row.original.line_role },
    { id: "description", header: t("Description"), cell: ({ row }) => row.original.description },
    { id: "qty", header: t("Qty"), cell: ({ row }) => Number(row.original.quantity) },
    { id: "unit", header: t("Unit price"), cell: ({ row }) => <Money amount={row.original.unit_price} currency={inv.currency} /> },
    { id: "amount", header: t("Amount"), cell: ({ row }) => <Money amount={row.original.amount} currency={inv.currency} strong /> },
    {
      id: "actions",
      header: "",
      cell: ({ row }) =>
        draft && canWrite ? (
          <Button size="sm" variant="ghost" onClick={() => removeLine.mutate(row.original.line_no)}>{t("Remove")}</Button>
        ) : null,
    },
  ];
  const title = inv.number ?? t("Draft {0}", tx(KIND_LABEL[inv.invoice_kind]).toLowerCase());
  return (
    <PageFrame
      title={tx(title)}
      description={`${inv.agency_name} · ${tx(KIND_LABEL[inv.invoice_kind])} · ${periodLabel(inv.period_start, lang)}`}
      breadcrumbs={[{ label: t("Settlement"), href: "/agency-billing" }, { label: tx(title) }]}
      actions={
        canWrite ? (
          <span className="flex flex-wrap gap-2">
            {draft && <Button variant="ghost" onClick={() => setDeleteOpen(true)}>{t("Delete draft")}</Button>}
            {draft && <Button variant="primary" disabled={issue.isPending} onClick={() => issue.mutate()}>{t("Issue invoice")}</Button>}
            {inv.status === "issued" && <Button variant="primary" onClick={() => setPayOpen(true)}>{t("Record payment")}</Button>}
            {inv.status === "issued" && <Button variant="ghost" onClick={() => setVoidOpen(true)}>{t("Void")}</Button>}
          </span>
        ) : undefined
      }
    >
      <Section title={t("Summary")}>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-[13px] md:grid-cols-4">
          <Fact label={t("Status")}><InvoiceStatusPill status={inv.status} overdueDays={inv.overdue_days} /></Fact>
          <Fact label={t("Total")}><Money amount={inv.total} currency={inv.currency} strong /></Fact>
          <Fact label={t("Tax")}><Money amount={inv.tax_amount} currency={inv.currency} /></Fact>
          <Fact label={t("Left to pay")}>{inv.status === "issued" ? <Money amount={inv.balance} currency={inv.currency} /> : "—"}</Fact>
          <Fact label={t("Issued")}>{inv.issued_on ? <DateText value={inv.issued_on} /> : "—"}</Fact>
          <Fact label={t("Due")}>{inv.due_date ? <DateText value={inv.due_date} /> : "—"}</Fact>
          <Fact label={t("VAT e-invoice no.")}>{inv.e_invoice_no ?? "—"}</Fact>
          <Fact label={t("Waiting approval")}><Money amount={inv.waiting} currency={inv.currency} /></Fact>
        </dl>
        {inv.bill_to && (
          <p className="mt-3 text-[13px]">
            {t("Bill to: {0}{1}{2}", inv.bill_to.legal_entity_name ?? inv.bill_to.agency_name ?? "", inv.bill_to.tax_id ? `, tax ID ${inv.bill_to.tax_id}` : "", inv.bill_to.billing_country ? `, ${inv.bill_to.billing_country}` : "")}
          </p>
        )}
        {inv.footer_wording && <p className="mt-2 text-[12px] text-muted">{inv.footer_wording}</p>}
        {inv.void_reason && <p className="mt-2 text-[13px]">{t("Voided: {0}", inv.void_reason)}</p>}
        {draft && canWrite && (
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <Field label={t("Due date (optional)")} hint={t("Empty: the agency's terms decide")}>
              <Input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
            </Field>
          </div>
        )}
        {(inv.status === "issued" || inv.status === "paid") && !inv.e_invoice_no && canWrite && (
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <Field label={t("VAT e-invoice number")} hint={t("The number from the e-invoice software; recorded once")}>
              <Input value={eInvoice} onChange={(e) => setEInvoice(e.target.value)} />
            </Field>
            <Button disabled={!eInvoice.trim() || eNo.isPending} onClick={() => eNo.mutate()}>{t("Record")}</Button>
          </div>
        )}
      </Section>
      <Section title={t("Lines")}>
        <DataTable<AgencyInvoiceLine>
          searchable={false}
          tableId="agency-invoice-lines"
          data={inv.lines}
          columns={lineColumns}
          getRowId={(r) => String(r.line_no)}
          emptyTitle={t("No lines")}
          tableMinWidth={760}
        />
        {draft && canWrite && (
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <Field label={t("Adjustment")}>
              <Input value={line.description} placeholder={t("Contract price, credit…")} onChange={(e) => setLine({ ...line, description: e.target.value })} />
            </Field>
            <Field label={t("Qty")}>
              <Input type="number" min={0.01} step="0.01" value={line.quantity} onChange={(e) => setLine({ ...line, quantity: e.target.value })} />
            </Field>
            <Field label={t("Unit price ({0})", inv.currency)} hint={t("Negative for a credit")}>
              <Input type="number" step="0.01" value={line.unit_price} onChange={(e) => setLine({ ...line, unit_price: e.target.value })} />
            </Field>
            <Button disabled={!line.description.trim() || line.unit_price === "" || addLine.isPending} onClick={() => addLine.mutate()}>
              {t("Add line")}
            </Button>
          </div>
        )}
      </Section>
      <PaymentsSection inv={inv} onChange={refresh} />
      {(inv.reminders?.length ?? 0) > 0 && <RemindersSection reminders={inv.reminders ?? []} />}
      <ConfirmDialog
        open={voidOpen}
        onClose={() => setVoidOpen(false)}
        onConfirm={(reason) => voidIt.mutate(reason)}
        title={t("Void {0}", inv.number ?? t("invoice"))}
        message={t("The number stays used; the billed items can be drafted again. Only an invoice without an approved payment is voided.")}
        confirmLabel={t("Void invoice")}
        destructive
        reason={{ label: t("Reason"), required: true, maxLength: 500 }}
        busy={voidIt.isPending}
      />
      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={() => remove.mutate()}
        title={t("Delete draft")}
        message={t("The draft holds no number; the next draft run can create it again.")}
        confirmLabel={t("Delete")}
        destructive
        busy={remove.isPending}
      />
      <RecordPaymentDrawer inv={inv} open={payOpen} onClose={() => setPayOpen(false)} onDone={refresh} />
    </PageFrame>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  const tx = useTx();
  return (
    <div>
      <dt className="text-[11.5px] text-muted">{tx(label)}</dt>
      <dd className="mt-0.5">{children}</dd>
    </div>
  );
}

function PaymentsSection({ inv, onChange }: { inv: AgencyInvoiceDetail; onChange: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const me = useStaff();
  const canApprove = useCan("agency_payment.approve");
  const toast = useToast();
  const [rejecting, setRejecting] = useState<AgencyPayment | null>(null);
  const approve = useMutation({
    mutationFn: (p: AgencyPayment) => agencyBillingApi.approve(p.id),
    onSuccess: () => {
      toast.success(t("Payment approved"));
      onChange();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const reject = useMutation({
    mutationFn: ({ p, reason }: { p: AgencyPayment; reason: string }) => agencyBillingApi.reject(p.id, reason),
    onSuccess: () => {
      toast.success(t("Payment rejected"));
      setRejecting(null);
      onChange();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const columns: ColumnDef<AgencyPayment, unknown>[] = [
    { id: "received", header: t("Received"), cell: ({ row }) => <DateText value={row.original.received_on} /> },
    { id: "amount", header: t("Amount"), cell: ({ row }) => <Money amount={row.original.amount} currency={row.original.currency} strong /> },
    { id: "method", header: t("Method"), cell: ({ row }) => tx(AGENCY_PAYMENT_METHOD_LABEL[row.original.method]) },
    { id: "reference", header: t("Reference"), cell: ({ row }) => row.original.reference ?? "—" },
    { id: "document", header: t("Evidence"), cell: ({ row }) => row.original.document_ref },
    { id: "by", header: t("Recorded by"), cell: ({ row }) => paymentRecorder(row.original) },
    { id: "status", header: t("Status"), cell: ({ row }) => <PaymentStatusPill status={row.original.status} /> },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => {
        const p = row.original;
        if (!canApprove || p.status !== "recorded") return p.reject_reason ?? null;
        if (p.recorded_by === me.cis_user_id) return <span className="text-[12px] text-muted">{t("Another person approves")}</span>;
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
    <Section title={t("Payments")} description={t("A payment counts only once a second person approves it.")}>
      <DataTable<AgencyPayment>
        searchable={false}
        tableId="agency-invoice-payments"
        data={inv.payments}
        columns={columns}
        getRowId={(r) => r.id}
        emptyTitle={t("No payment recorded")}
        tableMinWidth={980}
      />
      <ConfirmDialog
        open={rejecting !== null}
        onClose={() => setRejecting(null)}
        onConfirm={(reason) => rejecting && reject.mutate({ p: rejecting, reason })}
        title={t("Reject payment")}
        message={t("The payment will not count. Say why.")}
        confirmLabel={t("Reject")}
        destructive
        reason={{ label: t("Reason"), required: true, maxLength: 500 }}
        busy={reject.isPending}
      />
    </Section>
  );
}

function RecordPaymentDrawer({ inv, open, onClose, onDone }: { inv: AgencyInvoiceDetail; open: boolean; onClose: () => void; onDone: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const toast = useToast();
  const left = (Number(inv.balance) - Number(inv.waiting)).toFixed(2);
  const empty = { amount: left, method: "bank_transfer" as AgencyPaymentMethod, received_on: new Date().toISOString().slice(0, 10), reference: "", document_ref: "", note: "" };
  const [form, setForm] = useState(empty);
  const [key, setKey] = useState(newKey);
  const save = useMutation({
    mutationFn: () =>
      agencyBillingApi.recordPayment(
        inv.id,
        {
          amount: form.amount,
          method: form.method,
          received_on: form.received_on,
          reference: form.reference.trim() || undefined,
          document_ref: form.document_ref.trim(),
          note: form.note.trim() || undefined,
        },
        key,
      ),
    onSuccess: () => {
      toast.success(t("Payment recorded; a second person approves it"));
      setForm(empty);
      setKey(newKey());
      onDone();
      onClose();
    },
  });
  return (
    <FormDrawer
      open={open}
      onClose={onClose}
      title={t("Record payment · {0}", inv.number ?? "")}
      onSubmit={() => save.mutate()}
      submitLabel={t("Record payment")}
      busy={save.isPending}
      error={save.isError ? errorText(save.error) : null}
    >
      <div className="flex flex-col gap-3">
        <Field label={t("Amount ({0})", inv.currency)} required hint={t("At most {0} is left to record", left)}>
          <Input type="number" min={0.01} step="0.01" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
        </Field>
        <Field label={t("Method")} required>
          <Select
            value={form.method}
            onChange={(e) => setForm({ ...form, method: e.target.value as AgencyPaymentMethod })}
            options={Object.entries(AGENCY_PAYMENT_METHOD_LABEL).map(([value, label]) => ({ value, label: tx(label) }))}
          />
        </Field>
        <Field label={t("Received on")} required>
          <Input type="date" value={form.received_on} onChange={(e) => setForm({ ...form, received_on: e.target.value })} />
        </Field>
        <Field label={t("Bank or receipt reference")}>
          <Input value={form.reference} onChange={(e) => setForm({ ...form, reference: e.target.value })} />
        </Field>
        <Field label={t("Evidence")} required hint={t("Bank statement line, slip or receipt number; never a card number")}>
          <Input value={form.document_ref} onChange={(e) => setForm({ ...form, document_ref: e.target.value })} />
        </Field>
        <Field label={t("Note")}>
          <Textarea value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
        </Field>
      </div>
    </FormDrawer>
  );
}

const REMINDER_COLUMNS: ColumnDef<InvoiceReminder, unknown>[] = [
  {
    id: "step",
    header: "Step",
    cell: ({ row }) => <Phrase text={row.original.step_kind === "credit_stop" ? `Holds paused (${row.original.step_days} d)` : `Reminder (${row.original.step_days} d)`} />,
  },
  { id: "overdue", header: "Days overdue", cell: ({ row }) => row.original.days_overdue },
  {
    id: "outcome",
    header: "Outcome",
    cell: ({ row }) =>
      row.original.outcome === "sent"
        ? `Sent to ${row.original.sent_to ?? "—"}${row.original.redirected ? " (staff inbox, test environment)" : ""}`
        : "Not sent: no billing e-mail on the invoice",
  },
  { id: "at", header: "When", cell: ({ row }) => <DateText value={row.original.created_at} /> },
];

function RemindersSection({ reminders }: { reminders: InvoiceReminder[] }) {
  const t = useT(MSG);
  return (
    <Section title={t("Reminders")} description={t("Overdue e-mails sent by the daily job, at most one per step.")}>
      <DataTable<InvoiceReminder>
        searchable={false}
        tableId="agency-invoice-reminders"
        data={reminders}
        columns={REMINDER_COLUMNS}
        getRowId={(r) => String(r.step_days)}
        emptyTitle={t("No reminder")}
      />
    </Section>
  );
}
