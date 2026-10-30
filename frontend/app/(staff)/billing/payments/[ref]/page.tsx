"use client";
// app/(staff)/billing/payments/[ref]/page.tsx — Payment (AA-682 S3, bk-028; S252 design §6, UI spec §3). One recorded
// payment: amount, method, day, reference, note, who recorded / verified / voided it, the evidence files (add while it
// waits for verification; download with a 5-minute link) and the actions: edit the reference / note before verify,
// verify (a second admin, never the recorder: maker-checker), void (reason, final). GET /booking/payments/{ref}.
// AA-996 (Figma PAYMENT DETAILS): two columns: the transaction and its evidence on the left, the "Related trip" (code,
// status, style, dates, party, invoice total) and "Customer" (name, code, e-mail and phone with customer.read) cards
// on the right, each with an open link. A B2B trip has no customer card (the agency settles outside this module).

import { use, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ExternalLink, Mail, Phone } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { billingApi, PAYMENT_METHOD_LABEL, type Payment } from "@/lib/api/billing";
import {
  Input,
  Textarea,
  Button,
  DateText,
  EmptyState,
  FormDrawer,
  InvoiceStatusBadge,
  Money,
  PageFrame,
  PaymentStateBadge,
  Section,
  Skeleton,
  TripCaseStatusBadge,
  useCan,
  useStaff,
  useToast,
} from "@/ui";
import { QueryError } from "../../../cases/_parts/QueryError";
import { useBillingRefresh } from "../../_parts/InvoiceView";
import { EvidenceFiles, VerifyPaymentDialog, VoidPaymentDialog, errorText, fieldCls } from "../../_parts/PaymentDialogs";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[140px_minmax(0,1fr)] gap-3 py-1.5 text-[13px]">
      <span className="text-muted">{label}</span>
      <span className="min-w-0 break-words text-body">{children}</span>
    </div>
  );
}

export default function PaymentPage({ params }: { params: Promise<{ ref: string }> }) {
  const { ref } = use(params);
  const key = decodeURIComponent(ref);
  const canRead = useCan("billing.read");
  const me = useStaff();
  const toast = useToast();
  const q = useQuery({ queryKey: ["billing", "payment", key], queryFn: () => billingApi.payment(key), enabled: canRead });
  const refreshAll = useBillingRefresh(q.data?.trip_case.code);
  const refresh = () => {
    refreshAll();
    q.refetch();
  };
  const [dialog, setDialog] = useState<"verify" | "void" | "edit" | null>(null);
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: () => billingApi.updatePayment(key, { reference: reference.trim() || null, note: note.trim() || null }),
    onSuccess: () => {
      toast.success("Payment saved");
      setDialog(null);
      refresh();
    },
  });
  const crumbs = [{ label: "Payments & invoices", href: "/billing?tab=payments" }, { label: key }];
  if (!canRead) {
    return (
      <PageFrame title="Payment" breadcrumbs={crumbs}>
        <EmptyState title="Payments need the billing role" description="Admin and sales staff see invoices and payments." />
      </PageFrame>
    );
  }
  if (q.isLoading) {
    return (
      <PageFrame title={key} breadcrumbs={crumbs}>
        <Skeleton h={240} r={12} />
      </PageFrame>
    );
  }
  if (q.isError || !q.data) {
    const missing = q.error instanceof ApiError && q.error.status === 404;
    return (
      <PageFrame title={key} breadcrumbs={crumbs}>
        {missing ? (
          <EmptyState title={`Payment ${key} not found`} description="The reference may be wrong." />
        ) : (
          <QueryError error={q.error} onRetry={() => q.refetch()} what="this payment" />
        )}
      </PageFrame>
    );
  }
  const p = q.data;
  const ownRecord = p.recorded_by?.id === me.cis_user_id;
  return (
    <PageFrame
      title={`Payment ${p.ref}`}
      breadcrumbs={crumbs}
      description={
        <span className="inline-flex flex-wrap items-center gap-2">
          {p.kind === "refund" ? "Refund paid back on" : "Money received on"}{" "}
          <Link href={`/billing/invoices/${encodeURIComponent(p.invoice.number)}`} className="font-medium text-accent-deep hover:underline">
            {p.invoice.number}
          </Link>
          <InvoiceStatusBadge status={p.invoice.status} /> · trip{" "}
          <Link href={`/cases/${encodeURIComponent(p.trip_case.code)}?tab=billing`} className="font-medium text-accent-deep hover:underline">
            {p.trip_case.code}
          </Link>
        </span>
      }
      actions={
        <>
          {p.actions.edit && (
            <Button
              onClick={() => {
                setReference(p.reference ?? "");
                setNote(p.note ?? "");
                setDialog("edit");
              }}
            >
              Edit
            </Button>
          )}
          {p.actions.verify && (
            <Button variant="primary" onClick={() => setDialog("verify")}>
              Verify
            </Button>
          )}
          {p.actions.void && (
            <Button variant="ghost" onClick={() => setDialog("void")}>
              Void
            </Button>
          )}
        </>
      }
    >
      <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]">
        <div className="flex min-w-0 flex-col gap-5">
          <Section
            title={
              <span className="flex flex-wrap items-center gap-2">
                <Money amount={p.kind === "refund" ? `-${p.amount}` : p.amount} currency={p.currency} strong />
                <PaymentStateBadge state={p.state} />
              </span>
            }
            description={
              p.state === "recorded"
                ? ownRecord
                  ? "You recorded it: another admin verifies it before it counts."
                  : p.needs_evidence
                    ? "Waiting for the evidence file, then for an admin to verify it."
                    : "Waiting for an admin (not the person who recorded it) to verify it."
                : p.state === "verified"
                  ? "Verified: it counts on the invoice."
                  : "Voided: it no longer counts."
            }
          >
            <Row label="Method">{PAYMENT_METHOD_LABEL[p.method]}</Row>
            <Row label={p.kind === "refund" ? "Paid back on" : "Received on"}>
              <DateText value={p.received_on} />
            </Row>
            <Row label="Reference">{p.reference ?? "—"}</Row>
            {p.received_amount && (
              <Row label="Arrived as">
                <Money amount={p.received_amount} currency={p.received_currency} />
              </Row>
            )}
            <Row label="Note">{p.note ? <span className="whitespace-pre-line">{p.note}</span> : "—"}</Row>
            <Row label="Recorded">
              {p.recorded_by?.name ?? p.recorded_by?.id ?? "—"} · <DateText value={p.recorded_at} withTime />
            </Row>
            {p.verified_at && (
              <Row label="Verified">
                {p.verified_by?.name ?? p.verified_by?.id} · <DateText value={p.verified_at} withTime />
              </Row>
            )}
            {p.voided_at && (
              <Row label="Voided">
                {p.voided_by?.name ?? p.voided_by?.id} · <DateText value={p.voided_at} withTime /> · {p.void_reason}
              </Row>
            )}
            {p.invoice.balance !== undefined && (
              <Row label="Invoice">
                paid <Money amount={p.invoice.net_paid} currency={p.invoice.currency} /> of{" "}
                <Money amount={p.invoice.total_amount} currency={p.invoice.currency} />
              </Row>
            )}
          </Section>
          <Section title="Evidence" description="Bank slip or receipt. Needed to verify, except for cash.">
            <EvidenceFiles payment={p} onChanged={refresh} />
          </Section>
        </div>
        <div className="flex min-w-0 flex-col gap-5">
          <RelatedTrip payment={p} />
          {p.customer && <CustomerCard customer={p.customer} />}
        </div>
      </div>
      <VerifyPaymentDialog payment={dialog === "verify" ? p : null} onClose={() => setDialog(null)} onDone={refresh} />
      <VoidPaymentDialog payment={dialog === "void" ? p : null} onClose={() => setDialog(null)} onDone={refresh} />
      <FormDrawer
        open={dialog === "edit"}
        onClose={() => setDialog(null)}
        title={`Edit ${p.ref}`}
        onSubmit={() => save.mutate()}
        busy={save.isPending}
        error={save.isError ? errorText(save.error) : null}
      >
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">Bank or receipt reference</span>
          <Input size="sm" className={fieldCls} value={reference} maxLength={100} onChange={(e) => setReference(e.target.value)} />
          <span className="text-xs text-muted">Never a card number.</span>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">Note</span>
          <Textarea rows={3} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
      </FormDrawer>
    </PageFrame>
  );
}

function CardLink({ href, label }: { href: string; label: string }) {
  return (
    <Link
      href={href}
      aria-label={label}
      title={label}
      className="grid h-8 w-8 place-items-center rounded-md border border-line text-muted hover:border-accent-border hover:text-accent-deep"
    >
      <ExternalLink size={14} />
    </Link>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 text-[13px]">
      <span className="shrink-0 text-muted">{label}</span>
      <span className="min-w-0 break-words text-right text-ink">{children}</span>
    </div>
  );
}

function RelatedTrip({ payment: p }: { payment: Payment }) {
  const t = p.trip_case;
  return (
    <Section
      title="Related trip"
      actions={<CardLink href={`/cases/${encodeURIComponent(t.code)}`} label={`Open trip case ${t.code}`} />}
    >
      <div data-testid="payment-related-trip">
        <Fact label="Trip case">
          <Link href={`/cases/${encodeURIComponent(t.code)}`} className="font-mono font-medium text-accent-deep hover:underline">
            {t.code}
          </Link>
        </Fact>
        {t.status && (
          <Fact label="Status">
            <TripCaseStatusBadge status={t.status} />
          </Fact>
        )}
        <Fact label="Trip style">{t.trip_style ?? "—"}</Fact>
        <Fact label="Dates">
          {t.start_date ? (
            <>
              <DateText value={t.start_date} />
              {t.end_date && t.end_date !== t.start_date ? (
                <>
                  {" – "}
                  <DateText value={t.end_date} />
                </>
              ) : null}
            </>
          ) : (
            "Not set"
          )}
        </Fact>
        {t.pax ? <Fact label="Travellers">{t.pax}</Fact> : null}
        <div className="my-2 border-t border-line2" />
        <Fact label="Invoice total">
          {p.invoice.total_amount !== undefined ? <Money amount={p.invoice.total_amount} currency={p.invoice.currency} strong /> : "—"}
        </Fact>
      </div>
    </Section>
  );
}

function CustomerCard({ customer: c }: { customer: NonNullable<Payment["customer"]> }) {
  const name = c.name ?? c.code ?? "Customer";
  const initials = name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase())
    .join("");
  return (
    <Section title="Customer" actions={<CardLink href={`/customers/${encodeURIComponent(c.code ?? c.id)}`} label={`Open customer ${name}`} />}>
      <div className="flex flex-col gap-3" data-testid="payment-customer">
        <div className="flex min-w-0 items-center gap-3">
          <span aria-hidden="true" className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-accent-tint text-[13px] font-semibold text-accent-deep">
            {initials || "?"}
          </span>
          <span className="flex min-w-0 flex-col">
            <span className="truncate text-[14px] font-medium text-ink">{name}</span>
            {c.code && <span className="font-mono text-xs text-muted">{c.code}</span>}
          </span>
        </div>
        {(c.email || c.phone) && (
          <div className="flex flex-col gap-1.5 text-[13px] text-body">
            {c.email && (
              <span className="flex min-w-0 items-center gap-2">
                <Mail size={14} className="shrink-0 text-muted" aria-hidden="true" />
                <span className="min-w-0 break-all">{c.email}</span>
              </span>
            )}
            {c.phone && (
              <span className="flex items-center gap-2">
                <Phone size={14} className="shrink-0 text-muted" aria-hidden="true" />
                {c.phone}
              </span>
            )}
          </div>
        )}
      </div>
    </Section>
  );
}
