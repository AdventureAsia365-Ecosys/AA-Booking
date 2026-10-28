"use client";
// app/(staff)/billing/_parts/InvoiceView.tsx
// AA-682 S3 (bk-028, S252 design §6) — one invoice: its state and money (total, paid = verified only, left to pay,
// waiting for verification), the lines (the locked version total + adjustments), the payment schedule with its derived
// state (scheduled / due soon / overdue / paid), the payments (record, verify by a second admin, void) and the
// bill-to / issuer / payment instructions as printed on the payment request. Used by /billing/invoices/[number] and the
// case Billing tab. "Paid" is never shown without verified money; no copy claims an e-mail.
// AA-996 (Figma View Details, SEND REMAINDER): an Issue date / Due date card with the PDF note, a totals footer under
// the lines, light tables (no Columns menu or pager) for the invoice's short lists, and a confirm before Remind (same
// endpoint as before).

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BellRing, Pencil, Plus, Send, Trash2 } from "lucide-react";
import {
  billingApi,
  PAYMENT_METHOD_LABEL,
  SCHEDULE_KIND_LABEL,
  type InvoiceDetail,
  type InvoiceLine,
  type Payment,
  type ScheduleRow,
} from "@/lib/api/billing";
import {
  Button,
  ConfirmDialog,
  DateText,
  InvoiceStatusBadge,
  KpiTile,
  LightTable,
  Money,
  PaymentStateBadge,
  Pill,
  ScheduleStateBadge,
  Section,
  formatDate,
  formatDay,
  formatMoney,
  useToast,
  type LightColumn,
} from "@/ui";
import { InvoiceFormModal } from "./InvoiceForm";
import { RecordPaymentModal, VerifyPaymentDialog, VoidPaymentDialog, errorText } from "./PaymentDialogs";
import { InvoiceDatesCard, InvoiceTotalsFooter } from "./InvoiceDocParts";

type Panel = "edit" | "amend" | "record" | "send" | "delete" | "cancel" | "remind" | null;

export function useBillingRefresh(caseCode?: string) {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ["billing"] });
    if (caseCode) {
      qc.invalidateQueries({ queryKey: ["trip-case", caseCode] });
    }
  };
}

export function InvoiceView({ invoice, onChanged }: { invoice: InvoiceDetail; onChanged: () => void }) {
  const toast = useToast();
  const [panel, setPanel] = useState<Panel>(null);
  const [verifying, setVerifying] = useState<Payment | null>(null);
  const [voiding, setVoiding] = useState<Payment | null>(null);
  const a = invoice.actions;
  const cur = invoice.currency;
  const done = () => onChanged();
  const send = useMutation({
    mutationFn: () => billingApi.send(invoice.invoice_number),
    onSuccess: (i) => {
      toast.success(`${i.invoice_number} sent: the customer sees it in My Trips (no e-mail is sent)`);
      setPanel(null);
      done();
    },
  });
  const remove = useMutation({
    mutationFn: () => billingApi.remove(invoice.invoice_number),
    onSuccess: () => {
      toast.success(`Draft ${invoice.invoice_number} deleted`);
      setPanel(null);
      done();
    },
  });
  const cancel = useMutation({
    mutationFn: (reason: string) => billingApi.cancel(invoice.invoice_number, reason),
    onSuccess: () => {
      toast.success(`${invoice.invoice_number} cancelled`);
      setPanel(null);
      done();
    },
  });
  const remind = useMutation({
    mutationFn: () => billingApi.remind(invoice.invoice_number),
    onSuccess: (r) => {
      toast.success(r.reminded ? "Reminder added to the customer's My Trips feed (no e-mail)" : "Already reminded today");
      setPanel(null);
      done();
    },
  });

  const lineCols: LightColumn<InvoiceLine>[] = [
    {
      id: "description",
      header: "Line",
      width: "minmax(0,2.4fr)",
      phoneLabel: false,
      cell: (r) => (
        <span className="flex min-w-0 flex-col">
          <span className="text-[13.5px] text-ink">{r.description}</span>
          {r.kind === "version_total" ? (
            <span className="text-xs text-muted">Approved proposal (locked)</span>
          ) : (
            <span className="text-xs text-muted">Adjustment: {r.reason}</span>
          )}
        </span>
      ),
    },
    { id: "qty", numeric: true, width: "minmax(0,0.5fr)", header: "Qty", cell: (r) => <span className="bk-tabular text-[13px]">{Number(r.quantity)}</span> },
    { id: "unit", numeric: true, header: "Unit price", cell: (r) => <Money amount={r.unit_price} currency={cur} className="text-[13px]" /> },
    { id: "amount", numeric: true, header: "Amount", cell: (r) => <Money amount={r.amount} currency={cur} strong className="text-[13px]" /> },
  ];
  const scheduleCols: LightColumn<ScheduleRow>[] = [
    { id: "kind", header: "Payment", cell: (r) => <span className="text-[13px] text-ink">{SCHEDULE_KIND_LABEL[r.kind]}</span> },
    { id: "due", header: "Due", cell: (r) => <DateText value={r.due_date} className="text-[13px]" /> },
    { id: "amount", numeric: true, header: "Amount", cell: (r) => <Money amount={r.amount} currency={cur} className="text-[13px]" /> },
    { id: "covered", numeric: true, header: "Paid", cell: (r) => <Money amount={r.covered_amount} currency={cur} className="text-[13px]" /> },
    {
      id: "state",
      header: "Status",
      cell: (r) => (
        <span className="flex flex-col gap-0.5">
          <ScheduleStateBadge state={r.state} />
          {r.days_overdue ? <span className="text-xs text-danger">{r.days_overdue} day(s) late</span> : null}
        </span>
      ),
    },
  ];
  const paymentCols: LightColumn<Payment>[] = [
    {
      id: "ref",
      header: "Payment",
      width: "minmax(0,1.5fr)",
      cell: (r) => (
        <span className="flex flex-col">
          <Link href={`/billing/payments/${encodeURIComponent(r.ref)}`} className="text-[13px] font-medium text-accent-deep hover:underline">
            {r.ref}
          </Link>
          <span className="text-xs text-muted">{r.kind === "refund" ? "Refund" : "Received"} · {PAYMENT_METHOD_LABEL[r.method]}</span>
        </span>
      ),
    },
    { id: "date", header: "Date", cell: (r) => <DateText value={r.received_on} className="text-[13px]" /> },
    {
      id: "amount",
      header: "Amount",
      numeric: true,
      cell: (r) => (
        <Money amount={r.kind === "refund" ? `-${r.amount}` : r.amount} currency={r.currency} className="text-[13px]" />
      ),
    },
    {
      id: "who",
      header: "Recorded / verified",
      cell: (r) => (
        <span className="text-xs text-body">
          {r.recorded_by?.name ?? "—"}
          {r.verified_by ? ` / ${r.verified_by.name ?? r.verified_by.id}` : ""}
        </span>
      ),
    },
    {
      id: "state",
      header: "Status",
      cell: (r) => (
        <span className="flex flex-col gap-0.5">
          <PaymentStateBadge state={r.state} />
          {r.state === "recorded" && r.needs_evidence && <span className="text-xs text-warning">Evidence missing</span>}
        </span>
      ),
    },
    {
      id: "actions",
      header: "",
      width: "auto",
      phoneLabel: false,
      cell: (r) => (
        <span className="flex justify-end gap-1.5">
          {r.actions.verify && (
            <Button size="sm" variant="primary" onClick={() => setVerifying(r)}>
              Verify
            </Button>
          )}
          {r.actions.void && (
            <Button size="sm" variant="ghost" onClick={() => setVoiding(r)}>
              Void
            </Button>
          )}
        </span>
      ),
    },
  ];

  const issuer = invoice.issuer;
  return (
    <div className="flex flex-col gap-5">
      <Section
        title={
          <span className="flex flex-wrap items-center gap-2">
            {invoice.invoice_number}
            <InvoiceStatusBadge status={invoice.status} overdue={invoice.overdue} />
            <Pill tone="neutral" dot={false}>
              Payment request (proforma)
            </Pill>
          </span>
        }
        description={
          invoice.status === "draft"
            ? "Draft: only staff see it. Send it to show it to the customer in My Trips."
            : invoice.issued_at
              ? `Sent ${formatDate(invoice.issued_at)}${invoice.sent_by?.name ? ` by ${invoice.sent_by.name}` : ""}. Trip ${invoice.trip_case.code}.`
              : undefined
        }
        actions={
          <>
            {a.edit && (
              <Button onClick={() => setPanel("edit")}>
                <Pencil size={14} /> Edit
              </Button>
            )}
            {a.send && (
              <Button variant="primary" onClick={() => setPanel("send")}>
                <Send size={14} /> Send
              </Button>
            )}
            {a.record_payment && (
              <Button variant="primary" onClick={() => setPanel("record")}>
                <Plus size={14} /> Record payment
              </Button>
            )}
            {a.amend && (
              <Button onClick={() => setPanel("amend")}>
                <Pencil size={14} /> Amend
              </Button>
            )}
            {a.remind && (
              <Button
                onClick={() => {
                  remind.reset(); // a failed send earlier must not show its error before this one is tried
                  setPanel("remind");
                }}
                disabled={remind.isPending}
              >
                <BellRing size={14} /> Remind
              </Button>
            )}
            {a.cancel && (
              <Button variant="ghost" onClick={() => setPanel("cancel")}>
                Cancel invoice
              </Button>
            )}
            {a.delete && (
              <Button variant="ghost" onClick={() => setPanel("delete")}>
                <Trash2 size={14} /> Delete draft
              </Button>
            )}
          </>
        }
      >
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <KpiTile label="Total" value={<Money amount={invoice.total_amount} currency={cur} />} />
          <KpiTile label="Paid (verified)" value={<Money amount={invoice.net_paid} currency={cur} />} hint={Number(invoice.verified_refunded) > 0 ? `after ${formatMoney(invoice.verified_refunded, cur)} refunded` : undefined} />
          <KpiTile
            label={invoice.balance && Number(invoice.balance) < 0 ? "Overpaid" : "Left to pay"}
            value={invoice.balance === null ? null : <Money amount={invoice.balance.replace("-", "")} currency={cur} />}
            hint={Number(invoice.overdue_amount) > 0 ? `${formatMoney(invoice.overdue_amount, cur)} overdue` : invoice.next_due_date ? `next due ${formatDay(invoice.next_due_date)}` : undefined}
            hintTone={Number(invoice.overdue_amount) > 0 ? "danger" : "neutral"}
          />
          <KpiTile
            label="Waiting for verification"
            value={invoice.unverified_count}
            hint={invoice.unverified_count ? `${formatMoney(invoice.unverified_amount, cur)}: a second admin verifies` : "Nothing waiting"}
            hintTone={invoice.unverified_count ? "warning" : "neutral"}
          />
        </div>
        {invoice.status === "cancelled" && invoice.cancel_reason && <p className="mt-3 text-[13px] text-danger">Cancelled: {invoice.cancel_reason}</p>}
      </Section>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-5">
          <Section title="Lines" padded={false}>
            <LightTable<InvoiceLine>
              label="Invoice lines"
              rows={invoice.items}
              columns={lineCols}
              rowKey={(r) => r.id}
              footer={<InvoiceTotalsFooter invoice={invoice} />}
            />
          </Section>
          <Section title="Payment schedule" description="Paid / due / overdue follows the verified payments, oldest due first." padded={false}>
            <LightTable<ScheduleRow> label="Payment schedule" rows={invoice.schedule} columns={scheduleCols} rowKey={(r) => r.id} />
          </Section>
          <Section title="Payments" description="Recorded by hand; each counts once a second admin has verified it." padded={false}>
            <LightTable<Payment>
              label="Payments"
              rows={invoice.payments}
              columns={paymentCols}
              rowKey={(r) => r.id}
              empty={
                <>
                  <span className="font-medium text-ink">No payment yet.</span>{" "}
                  {invoice.status === "draft" ? "Send the invoice first." : "Record a payment when money arrives."}
                </>
              }
            />
          </Section>
        </div>
        <div className="flex min-w-0 flex-col gap-5">
          <InvoiceDatesCard invoice={invoice} />
          <Section title="Bill to">
            {invoice.bill_to_name ? (
              <div className="flex flex-col gap-0.5 text-[13px] text-body">
                <span className="font-medium text-ink">{invoice.bill_to_name}</span>
                {invoice.bill_to_email && <span>{invoice.bill_to_email}</span>}
                {invoice.bill_to_address && <span className="whitespace-pre-line">{invoice.bill_to_address}</span>}
                <span className="mt-1 text-xs text-muted">Taken when the invoice was sent.</span>
              </div>
            ) : (
              <p className="text-[13px] text-body">
                {invoice.customer?.name ?? "The trip's customer"}
                <span className="block text-xs text-muted">Name, e-mail and address are taken when the invoice is sent.</span>
              </p>
            )}
          </Section>
          <Section title="On the payment request">
            <div className="flex flex-col gap-2 text-[13px] text-body">
              <span>
                <span className="text-xs text-muted">Issued by</span>
                <br />
                {issuer?.name ?? "—"}
                {issuer?.address ? <span className="block text-xs text-muted">{issuer.address}</span> : null}
              </span>
              <span>
                <span className="text-xs text-muted">How to pay</span>
                <br />
                {invoice.payment_instructions ?? "—"}
              </span>
              {invoice.terms && (
                <span>
                  <span className="text-xs text-muted">Terms</span>
                  <br />
                  <span className="whitespace-pre-line">{invoice.terms}</span>
                </span>
              )}
              {invoice.notes && (
                <span>
                  <span className="text-xs text-muted">Internal note</span>
                  <br />
                  <span className="whitespace-pre-line">{invoice.notes}</span>
                </span>
              )}
            </div>
          </Section>
        </div>
      </div>

      <InvoiceFormModal mode={{ kind: "edit", invoice }} open={panel === "edit"} onClose={() => setPanel(null)} onDone={done} />
      <InvoiceFormModal mode={{ kind: "amend", invoice }} open={panel === "amend"} onClose={() => setPanel(null)} onDone={done} />
      <RecordPaymentModal invoice={invoice} open={panel === "record"} onClose={() => setPanel(null)} onDone={done} />
      <VerifyPaymentDialog payment={verifying} onClose={() => setVerifying(null)} onDone={done} />
      <VoidPaymentDialog payment={voiding} onClose={() => setVoiding(null)} onDone={done} />
      <ConfirmDialog
        open={panel === "send"}
        onClose={() => setPanel(null)}
        onConfirm={() => send.mutate()}
        title={`Send ${invoice.invoice_number}`}
        confirmLabel="Send"
        busy={send.isPending}
        error={send.isError ? errorText(send.error) : null}
        message={
          <>
            The customer sees this payment request (<Money amount={invoice.total_amount} currency={cur} />) and its due dates in My
            Trips. No e-mail is sent. After sending, lines are only added by an amendment.
          </>
        }
      />
      <ConfirmDialog
        open={panel === "delete"}
        onClose={() => setPanel(null)}
        onConfirm={() => remove.mutate()}
        title={`Delete draft ${invoice.invoice_number}`}
        confirmLabel="Delete draft"
        danger
        busy={remove.isPending}
        error={remove.isError ? errorText(remove.error) : null}
        message="The draft was never shown to the customer. It is kept in the history as deleted."
      />
      <ConfirmDialog
        open={panel === "cancel"}
        onClose={() => setPanel(null)}
        onConfirm={(reason) => cancel.mutate(reason)}
        title={`Cancel ${invoice.invoice_number}`}
        confirmLabel="Cancel invoice"
        danger
        busy={cancel.isPending}
        error={cancel.isError ? errorText(cancel.error) : null}
        reason={{ label: "Why is it cancelled?", required: true }}
        message="Only an invoice with nothing paid can be cancelled: refund verified payments first. The customer's My Trips feed says it was cancelled."
      />
      <ConfirmDialog
        open={panel === "remind"}
        onClose={() => setPanel(null)}
        onConfirm={() => remind.mutate()}
        title={`Send a reminder for ${invoice.invoice_number}`}
        confirmLabel="Send reminder"
        busy={remind.isPending}
        error={remind.isError ? errorText(remind.error) : null}
        message={
          <>
            {invoice.bill_to_name ?? invoice.customer?.name ?? "The customer"} gets a reminder of this payment request in My
            Trips, with what is left to pay
            {invoice.balance && Number(invoice.balance) > 0 ? (
              <>
                {" "}
                (<Money amount={invoice.balance} currency={cur} />)
              </>
            ) : null}
            . No e-mail is sent, and a request is reminded at most once a day.
          </>
        }
      />
    </div>
  );
}
