"use client";
// app/(staff)/billing/_parts/InvoiceForm.tsx
// AA-682 S3 (bk-028, S252 design §6 "Create Invoice stepper") — four steps in one modal: 1. the approved version (its
// total is the first, locked line), 2. adjustments (each with a reason: discount, extra night, transfer…), 3. the
// payment schedule (the default deposit / balance from the settings, or rows typed by hand that add up to the total),
// 4. review, then "Create draft" or "Create and send". The same form edits a draft (PATCH) and, with `amend`, appends
// lines to a sent invoice (the amendment rule: what is already paid stays). One Idempotency-Key per opening of the form.

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Check, Plus, Trash2 } from "lucide-react";
import {
  billingApi,
  newIdempotencyKey,
  SCHEDULE_KIND_LABEL,
  type AdjustmentIn,
  type CaseBilling,
  type InvoiceDetail,
  type ScheduleKind,
  type ScheduleRowIn,
} from "@/lib/api/billing";
import { Button, Modal, Money, formatMoney, useToast } from "@/ui";
import { errorText, fieldCls } from "./PaymentDialogs";
import { fromCents, lineCents, sumCents, toCents } from "./money";
import { DateField } from "@/ui/booking/DateField";
import { formatDay } from "@/ui/booking/dates";

type Mode = { kind: "create"; billing: CaseBilling } | { kind: "edit"; invoice: InvoiceDetail } | { kind: "amend"; invoice: InvoiceDetail };

interface AdjRow {
  description: string;
  quantity: string;
  unit_price: string;
  reason: string;
}

const STEPS = ["Approved version", "Adjustments", "Payment schedule", "Review"] as const;
const AMEND_STEPS = ["Added lines", "Payment schedule", "Review"] as const;

function emptyAdj(): AdjRow {
  return { description: "", quantity: "1", unit_price: "", reason: "" };
}

export function InvoiceFormModal({
  mode,
  open,
  onClose,
  onDone,
}: {
  mode: Mode;
  open: boolean;
  onClose: () => void;
  onDone: (invoice: InvoiceDetail) => void;
}) {
  const title =
    mode.kind === "create"
      ? `New invoice for ${mode.billing.trip_case.code}`
      : mode.kind === "edit"
        ? `Edit draft ${mode.invoice.invoice_number}`
        : `Amend ${mode.invoice.invoice_number}`;
  return (
    <Modal open={open} onClose={onClose} title={title} width={760}>
      {open && <FormBody mode={mode} onClose={onClose} onDone={onDone} />}
    </Modal>
  );
}

function FormBody({ mode, onClose, onDone }: { mode: Mode; onClose: () => void; onDone: (invoice: InvoiceDetail) => void }) {
  const toast = useToast();
  const amend = mode.kind === "amend";
  const steps: readonly string[] = amend ? AMEND_STEPS : STEPS;
  const [key] = useState(() => newIdempotencyKey("inv"));
  const [step, setStep] = useState(0);
  const currency = mode.kind === "create" ? (mode.billing.defaults?.currency ?? "USD") : mode.invoice.currency;
  const baseTotal =
    mode.kind === "create"
      ? (mode.billing.defaults?.version_total ?? "0")
      : mode.kind === "edit"
        ? (mode.invoice.items.find((i) => i.kind === "version_total")?.amount ?? "0")
        : mode.invoice.total_amount;
  const [adjs, setAdjs] = useState<AdjRow[]>(() =>
    mode.kind === "edit"
      ? mode.invoice.items
          .filter((i) => i.kind === "adjustment")
          .map((i) => ({ description: i.description, quantity: i.quantity, unit_price: i.unit_price, reason: i.reason ?? "" }))
      : amend
        ? [emptyAdj()]
        : [],
  );
  // Schedule: the default (server-computed on the final total) or rows typed by hand.
  const frozen = mode.kind === "amend" ? mode.invoice.schedule.filter((r) => Number(r.covered_amount) > 0) : [];
  const [customSchedule, setCustomSchedule] = useState(mode.kind === "edit");
  const [rows, setRows] = useState<ScheduleRowIn[]>(() => {
    if (mode.kind === "create") return (mode.billing.defaults?.schedule ?? []).map((r) => ({ kind: r.kind, amount: r.amount, due_date: r.due_date }));
    if (mode.kind === "edit") return mode.invoice.schedule.map((r) => ({ kind: r.kind, amount: r.amount, due_date: r.due_date }));
    return mode.invoice.schedule
      .filter((r) => Number(r.covered_amount) === 0)
      .map((r) => ({ kind: r.kind, amount: r.amount, due_date: r.due_date }));
  });
  const [notes, setNotes] = useState(mode.kind === "edit" ? (mode.invoice.notes ?? "") : "");
  const [terms, setTerms] = useState(mode.kind === "edit" ? (mode.invoice.terms ?? "") : "");
  const [sendNow, setSendNow] = useState(false);

  const adjCents = adjs.map((a) => lineCents(a.quantity, a.unit_price));
  const adjValid = adjs.every((a, i) => a.description.trim() && a.reason.trim() && adjCents[i] !== null && adjCents[i] !== BigInt(0));
  const base = toCents(baseTotal) ?? BigInt(0);
  const total = sumCents([base, ...adjCents]);
  const frozenCents = sumCents(frozen.map((r) => toCents(r.amount)));
  const rowsCents = sumCents(rows.map((r) => toCents(r.amount)));
  const target = total !== null && frozenCents !== null ? total - frozenCents : null;
  const scheduleOk = !customSchedule || (rowsCents !== null && target !== null && rowsCents === target && rows.every((r) => r.due_date));
  const canNext = step === (amend ? 0 : 1) ? adjValid && (!amend || adjs.length > 0) : step === (amend ? 1 : 2) ? scheduleOk : true;

  const body = (): { adjustments: AdjustmentIn[]; schedule: ScheduleRowIn[] | undefined } => ({
    adjustments: adjs.map((a) => ({
      description: a.description.trim(),
      quantity: a.quantity.trim() || "1",
      unit_price: a.unit_price.trim(),
      reason: a.reason.trim(),
    })),
    schedule: customSchedule ? rows : undefined,
  });

  const run = useMutation({
    mutationFn: async (send: boolean) => {
      const b = body();
      if (mode.kind === "create") {
        return billingApi.create(
          mode.billing.trip_case.code,
          { adjustments: b.adjustments, schedule: b.schedule ?? null, notes: notes.trim() || null, terms: terms.trim() || null, send_now: send },
          key,
        );
      }
      if (mode.kind === "edit") {
        return billingApi.update(mode.invoice.invoice_number, {
          adjustments: b.adjustments,
          ...(b.schedule ? { schedule: b.schedule } : {}),
          notes: notes.trim() || null,
          terms: terms.trim() || null,
        });
      }
      return billingApi.amend(mode.invoice.invoice_number, { adjustments: b.adjustments, ...(b.schedule ? { schedule: b.schedule } : {}) });
    },
    onSuccess: (inv) => {
      toast.success(
        mode.kind === "create"
          ? inv.status === "sent"
            ? `${inv.invoice_number} sent: the customer sees it in My Trips (no e-mail is sent)`
            : `${inv.invoice_number} saved as a draft`
          : mode.kind === "edit"
            ? `${inv.invoice_number} saved`
            : `${inv.invoice_number} amended: the customer sees the new total`,
      );
      onDone(inv);
      onClose();
    },
  });

  const versionLine = mode.kind === "create" ? mode.billing.version : null;
  const setAdj = (i: number, patch: Partial<AdjRow>) => setAdjs((xs) => xs.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  const setRow = (i: number, patch: Partial<ScheduleRowIn>) => setRows((xs) => xs.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  const adjustmentsStep = (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted">
        {amend
          ? "Lines are only added after sending (an extra night, a discount, a bank fee). What is already paid stays as it is."
          : "Optional: a discount, an extra service or a transfer, each with a reason. A negative price lowers the total."}
      </p>
      {adjs.map((a, i) => (
        <div key={i} className="grid gap-2 rounded-md border border-line2 p-3 sm:grid-cols-[1fr_80px_120px_auto]">
          <input className={fieldCls} placeholder="What it is for" value={a.description} maxLength={500} onChange={(e) => setAdj(i, { description: e.target.value })} />
          <input className={fieldCls} inputMode="decimal" aria-label="Quantity" value={a.quantity} onChange={(e) => setAdj(i, { quantity: e.target.value })} />
          <input className={fieldCls} inputMode="decimal" placeholder={`Price (${currency})`} value={a.unit_price} onChange={(e) => setAdj(i, { unit_price: e.target.value })} />
          <Button variant="ghost" size="sm" ariaLabel="Remove line" onClick={() => setAdjs((xs) => xs.filter((_, j) => j !== i))}>
            <Trash2 size={14} />
          </Button>
          <input className={`${fieldCls} sm:col-span-3`} placeholder="Reason (required)" value={a.reason} maxLength={500} onChange={(e) => setAdj(i, { reason: e.target.value })} />
          <span className="self-center text-right text-xs text-muted">{adjCents[i] !== null ? formatMoney(fromCents(adjCents[i] as bigint), currency) : "—"}</span>
        </div>
      ))}
      <div>
        <Button size="sm" onClick={() => setAdjs((xs) => [...xs, emptyAdj()])}>
          <Plus size={13} /> Add a line
        </Button>
      </div>
    </div>
  );

  const scheduleStep = (
    <div className="flex flex-col gap-3">
      {frozen.length > 0 && (
        <div className="rounded-md bg-line2 px-3 py-2 text-xs text-body">
          Already covered by verified payments and kept:{" "}
          {frozen.map((r) => `${SCHEDULE_KIND_LABEL[r.kind]} ${formatMoney(r.amount, currency)} (${formatDay(r.due_date)})`).join(", ")}.
        </div>
      )}
      <label className="flex items-center gap-2 text-[13px] text-body">
        <input type="checkbox" checked={!customSchedule} onChange={(e) => setCustomSchedule(!e.target.checked)} />
        {amend
          ? "Put the difference on the last unpaid payment (or a new one)"
          : "Use the default schedule (deposit now, balance before the trip; one payment when the trip is close)"}
      </label>
      {customSchedule && (
        <>
          {rows.map((r, i) => (
            <div key={i} className="grid gap-2 sm:grid-cols-[160px_1fr_160px_auto]">
              <select className={fieldCls} value={r.kind} onChange={(e) => setRow(i, { kind: e.target.value as ScheduleKind })}>
                {Object.entries(SCHEDULE_KIND_LABEL).map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
              <input className={fieldCls} inputMode="decimal" aria-label="Amount" value={r.amount} onChange={(e) => setRow(i, { amount: e.target.value })} />
              <DateField className={fieldCls} aria-label="Due date" value={r.due_date} onChange={(v) => setRow(i, { due_date: v })} />
              <Button variant="ghost" size="sm" ariaLabel="Remove payment" onClick={() => setRows((xs) => xs.filter((_, j) => j !== i))}>
                <Trash2 size={14} />
              </Button>
            </div>
          ))}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Button size="sm" disabled={rows.length + frozen.length >= 10} onClick={() => setRows((xs) => [...xs, { kind: "other", amount: "", due_date: "" }])}>
              <Plus size={13} /> Add a payment
            </Button>
            <span className={`text-xs ${scheduleOk ? "text-success" : "text-danger"}`}>
              Rows {rowsCents !== null ? formatMoney(fromCents(rowsCents), currency) : "—"} of{" "}
              {target !== null ? formatMoney(fromCents(target), currency) : "—"}
            </span>
          </div>
        </>
      )}
      {mode.kind === "create" && !customSchedule && mode.billing.defaults && (
        <ul className="text-[13px] text-body">
          {mode.billing.defaults.schedule.map((r) => (
            <li key={r.seq}>
              {SCHEDULE_KIND_LABEL[r.kind]}: {total === base ? formatMoney(r.amount, currency) : "recomputed on the new total"} due {formatDay(r.due_date)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );

  const review = (
    <div className="flex flex-col gap-3 text-[13px] text-body">
      <div className="flex items-center justify-between rounded-md border border-line2 px-3 py-2">
        <span>{amend ? "New total" : "Invoice total"}</span>
        <strong>{total !== null ? formatMoney(fromCents(total), currency) : "—"}</strong>
      </div>
      {!amend && (
        <>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">Payment terms (printed on the payment request)</span>
            <textarea rows={2} maxLength={2000} value={terms} onChange={(e) => setTerms(e.target.value)} className="rounded-md border border-line bg-card px-3 py-2 text-sm text-ink outline-none focus:border-accent-border" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">Internal note (never shown to the customer)</span>
            <textarea rows={2} maxLength={2000} value={notes} onChange={(e) => setNotes(e.target.value)} className="rounded-md border border-line bg-card px-3 py-2 text-sm text-ink outline-none focus:border-accent-border" />
          </label>
        </>
      )}
      {mode.kind === "create" && (
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={sendNow} onChange={(e) => setSendNow(e.target.checked)} /> Send it now: the customer sees the
          payment request in My Trips (no e-mail is sent)
        </label>
      )}
      <p className="text-xs text-muted">A payment request (proforma), prices include taxes. It is not a VAT invoice.</p>
    </div>
  );

  const content = amend
    ? [adjustmentsStep, scheduleStep, review][step]
    : [
        <div key="v" className="flex flex-col gap-2 text-[13px] text-body">
          {versionLine ? (
            <>
              <p>
                The invoice starts from the approved proposal{" "}
                <strong>
                  V{versionLine.number}
                  {versionLine.title ? ` · ${versionLine.title}` : ""}
                </strong>
                : <Money amount={versionLine.total_price} currency={versionLine.currency} strong />, a locked line.
              </p>
              <p className="text-xs text-muted">Changes to the trip go in as adjustments on the next step.</p>
            </>
          ) : (
            <p>
              Version total: <Money amount={baseTotal} currency={currency} strong /> (locked line).
            </p>
          )}
        </div>,
        adjustmentsStep,
        scheduleStep,
        review,
      ][step];

  const last = step === steps.length - 1;
  return (
    <div className="flex flex-col gap-4 font-sans">
      <ol className="flex flex-wrap gap-2 text-xs">
        {steps.map((s, i) => (
          <li
            key={s}
            className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 ${
              i === step ? "bg-accent-tint font-semibold text-accent-deep" : i < step ? "text-success" : "text-muted"
            }`}
          >
            {i < step ? <Check size={12} /> : <span>{i + 1}.</span>} {s}
          </li>
        ))}
      </ol>
      <div className="min-h-[180px]">{content}</div>
      {run.isError && <p className="text-[13px] text-danger">{errorText(run.error)}</p>}
      <div className="flex flex-wrap justify-between gap-2">
        <Button variant="ghost" onClick={step === 0 ? onClose : () => setStep(step - 1)} disabled={run.isPending}>
          {step === 0 ? "Cancel" : "Back"}
        </Button>
        {!last ? (
          <Button variant="primary" disabled={!canNext} onClick={() => setStep(step + 1)}>
            Next
          </Button>
        ) : (
          <Button variant="primary" disabled={run.isPending || !adjValid || !scheduleOk} onClick={() => run.mutate(sendNow)}>
            {run.isPending ? "Saving…" : mode.kind === "create" ? (sendNow ? "Create and send" : "Create draft") : mode.kind === "edit" ? "Save draft" : "Amend invoice"}
          </Button>
        )}
      </div>
    </div>
  );
}
