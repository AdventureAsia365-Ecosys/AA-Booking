"use client";
// app/(staff)/billing/_parts/PaymentDialogs.tsx
// AA-682 S3 (bk-028, S252 design §6) — Record payment (receipt or refund, Idempotency-Key per opening of the dialog,
// optional evidence file uploaded right after), Verify (a second admin; the recorder never verifies) and Void (reason,
// final) dialogs, plus the evidence upload / download row. No payment provider: staff mark money by hand. Copy never
// says "marked as paid" before a verified payment, and never claims an e-mail.

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Download, FileUp } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  billingApi,
  newIdempotencyKey,
  PAYMENT_METHOD_LABEL,
  type InvoiceSummary,
  type Payment,
  type PaymentKind,
  type PaymentMethod,
} from "@/lib/api/billing";
import type { OpsFile } from "@/lib/api/supplierBookings";
import { Button, ConfirmDialog, Modal, Money, formatMoney, useToast } from "@/ui";
import { prepareUpload } from "../../_parts/SupplyContent";

const MAX_FILE_BYTES = 3 * 1024 * 1024;
export const fieldCls =
  "h-9 w-full min-w-0 rounded-md border border-line bg-card px-3 text-sm text-ink outline-none focus:border-accent-border";

export function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

function toBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(blob);
  });
}

/** A PDF is sent as it is (≤ 3 MB); a photo of a slip is re-encoded in the browser first (no EXIF reaches S3). */
async function prepareEvidence(file: File): Promise<{ content_type: string; data_base64: string }> {
  if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
    if (file.size > MAX_FILE_BYTES) throw new Error("A PDF is at most 3 MB here. Save a smaller copy or a photo of it.");
    return { content_type: "application/pdf", data_base64: await toBase64(file) };
  }
  return prepareUpload(file, MAX_FILE_BYTES);
}

export async function uploadEvidence(paymentId: string, file: File): Promise<OpsFile> {
  const doc = await prepareEvidence(file);
  return billingApi.uploadEvidence({ entity_id: paymentId, file_name: file.name, ...doc });
}

function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Record a receipt or a refund against a sent invoice. One Idempotency-Key per opening: a retry of the same save
 *  (double click, time-out) returns the same payment instead of a second one. */
export function RecordPaymentModal({
  invoice,
  open,
  onClose,
  onDone,
}: {
  invoice: Pick<InvoiceSummary, "invoice_number" | "currency" | "balance" | "net_paid">;
  open: boolean;
  onClose: () => void;
  onDone: (p: Payment) => void;
}) {
  return (
    <Modal open={open} onClose={onClose} title={`Record a payment on ${invoice.invoice_number}`} width={560}>
      {open && <RecordBody invoice={invoice} onClose={onClose} onDone={onDone} />}
    </Modal>
  );
}

function RecordBody({
  invoice,
  onClose,
  onDone,
}: {
  invoice: Pick<InvoiceSummary, "invoice_number" | "currency" | "balance" | "net_paid">;
  onClose: () => void;
  onDone: (p: Payment) => void;
}) {
  const toast = useToast();
  const [key] = useState(() => newIdempotencyKey("pmt"));
  const [kind, setKind] = useState<PaymentKind>("receipt");
  const [amount, setAmount] = useState(() => (invoice.balance && Number(invoice.balance) > 0 ? invoice.balance : ""));
  const [method, setMethod] = useState<PaymentMethod>("bank_transfer");
  const [receivedOn, setReceivedOn] = useState(today);
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [other, setOther] = useState(false);
  const [receivedAmount, setReceivedAmount] = useState("");
  const [receivedCurrency, setReceivedCurrency] = useState("VND");
  const [file, setFile] = useState<File | null>(null);
  const [allowDuplicate, setAllowDuplicate] = useState(false);
  const run = useMutation({
    mutationFn: async () => {
      const p = await billingApi.record(
        invoice.invoice_number,
        {
          kind,
          amount: amount.trim(),
          method,
          received_on: receivedOn,
          reference: reference.trim() || null,
          note: note.trim() || null,
          received_amount: other && receivedAmount.trim() ? receivedAmount.trim() : null,
          received_currency: other && receivedAmount.trim() ? receivedCurrency.trim().toUpperCase() : null,
          allow_duplicate_reference: allowDuplicate,
        },
        key,
      );
      let evidenceError: string | null = null;
      if (file && p.state === "recorded" && !p.evidence_file_id) {
        try {
          await uploadEvidence(p.id, file);
        } catch (e) {
          evidenceError = errorText(e);
        }
      }
      return { p, evidenceError };
    },
    onSuccess: ({ p, evidenceError }) => {
      toast.success(`${p.ref} recorded: another admin verifies it before it counts`);
      if (evidenceError) toast.error(`The evidence file was not added: ${evidenceError}. Add it from the payment.`);
      onDone(p);
      onClose();
    },
  });
  const duplicate = run.error instanceof ApiError && run.error.code === "duplicate_reference";
  const valid = /^\d+(\.\d{1,2})?$/.test(amount.trim()) && Number(amount) > 0 && !!receivedOn;
  return (
    <div className="flex flex-col gap-3 text-[13px] text-body">
      <p className="text-xs text-muted">
        Paid so far (verified): <Money amount={invoice.net_paid} currency={invoice.currency} />. Left to pay:{" "}
        <Money amount={invoice.balance} currency={invoice.currency} />. A payment counts only once a second admin has
        verified it.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">Type</span>
          <select className={fieldCls} value={kind} onChange={(e) => setKind(e.target.value as PaymentKind)}>
            <option value="receipt">Money received</option>
            <option value="refund">Refund paid back</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">
            Amount ({invoice.currency}) <span className="text-danger">*</span>
          </span>
          <input className={fieldCls} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="0.00" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">Method</span>
          <select className={fieldCls} value={method} onChange={(e) => setMethod(e.target.value as PaymentMethod)}>
            {Object.entries(PAYMENT_METHOD_LABEL).map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">
            Day the money moved <span className="text-danger">*</span>
          </span>
          <input type="date" className={fieldCls} value={receivedOn} onChange={(e) => setReceivedOn(e.target.value)} />
        </label>
      </div>
      <label className="flex flex-col gap-1">
        <span className="text-xs font-semibold text-muted">Bank or receipt reference</span>
        <input className={fieldCls} value={reference} maxLength={100} onChange={(e) => setReference(e.target.value)} placeholder="e.g. VCB FT2630012345" />
        <span className="text-xs text-muted">Never a card number.</span>
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-xs font-semibold text-muted">Note</span>
        <textarea rows={2} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} className="rounded-md border border-line bg-card px-3 py-2 text-sm text-ink outline-none focus:border-accent-border" />
      </label>
      <label className="flex items-center gap-2 text-xs text-body">
        <input type="checkbox" checked={other} onChange={(e) => setOther(e.target.checked)} /> The money arrived in another currency
      </label>
      {other && (
        <div className="grid gap-3 sm:grid-cols-[1fr_120px]">
          <input className={fieldCls} inputMode="decimal" value={receivedAmount} onChange={(e) => setReceivedAmount(e.target.value)} placeholder="Amount that arrived" />
          <input className={fieldCls} value={receivedCurrency} maxLength={3} onChange={(e) => setReceivedCurrency(e.target.value.toUpperCase())} aria-label="Currency that arrived" />
        </div>
      )}
      {method !== "cash" && (
        <div className="flex flex-col gap-2 rounded-md border border-line2 p-3">
          <span className="flex items-center gap-1.5 text-xs font-semibold text-muted">
            <FileUp size={13} /> Evidence: bank slip or receipt (PDF or a photo, at most 3 MB)
          </span>
          <input type="file" accept="application/pdf,image/jpeg,image/png,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-xs" />
          <span className="text-xs text-muted">Needed before the payment can be verified. Stored privately.</span>
        </div>
      )}
      {run.isError && (
        <div className="text-danger">
          <p>{errorText(run.error)}</p>
          {duplicate && (
            <label className="mt-1 flex items-center gap-2 text-xs text-body">
              <input type="checkbox" checked={allowDuplicate} onChange={(e) => setAllowDuplicate(e.target.checked)} /> It is another payment with
              the same reference: record it anyway
            </label>
          )}
        </div>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose} disabled={run.isPending}>
          Cancel
        </Button>
        <Button variant="primary" disabled={run.isPending || !valid} onClick={() => run.mutate()}>
          {run.isPending ? "Saving…" : kind === "refund" ? "Record refund" : "Record payment"}
        </Button>
      </div>
    </div>
  );
}

export function VerifyPaymentDialog({ payment, onClose, onDone }: { payment: Payment | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const run = useMutation({
    mutationFn: (p: Payment) => billingApi.verify(p.ref),
    onSuccess: (p) => {
      toast.success(`${p.ref} verified: it now counts on ${p.invoice.number}`);
      onDone();
      onClose();
    },
  });
  return (
    <ConfirmDialog
      open={!!payment}
      onClose={onClose}
      onConfirm={() => payment && run.mutate(payment)}
      title={payment ? `Verify ${payment.ref}` : "Verify"}
      confirmLabel="Verify payment"
      busy={run.isPending}
      error={run.isError ? errorText(run.error) : null}
      message={
        payment ? (
          <>
            You confirm that{" "}
            <strong>
              {formatMoney(payment.amount, payment.currency)} {payment.kind === "refund" ? "was paid back" : "arrived"}
            </strong>{" "}
            ({PAYMENT_METHOD_LABEL[payment.method]}, {payment.received_on}
            {payment.reference ? `, ref ${payment.reference}` : ""}), as the evidence shows. Recorded by{" "}
            {payment.recorded_by?.name ?? payment.recorded_by?.id ?? "someone else"}.
          </>
        ) : null
      }
    />
  );
}

export function VoidPaymentDialog({ payment, onClose, onDone }: { payment: Payment | null; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const run = useMutation({
    mutationFn: ({ p, reason }: { p: Payment; reason: string }) => billingApi.void(p.ref, reason),
    onSuccess: (p) => {
      toast.success(`${p.ref} voided`);
      onDone();
      onClose();
    },
  });
  return (
    <ConfirmDialog
      open={!!payment}
      onClose={onClose}
      onConfirm={(reason) => payment && run.mutate({ p: payment, reason })}
      title={payment ? `Void ${payment.ref}` : "Void"}
      confirmLabel="Void payment"
      danger
      busy={run.isPending}
      error={run.isError ? errorText(run.error) : null}
      reason={{ label: "Why is it voided?", required: true, placeholder: "e.g. recorded twice, the transfer bounced" }}
      message={
        payment ? (
          <>
            {formatMoney(payment.amount, payment.currency)} stops counting on {payment.invoice.number}
            {payment.state === "verified" ? "; the invoice and the trip status step back" : ""}. A void is final.
          </>
        ) : null
      }
    />
  );
}

/** Evidence files of a payment: download (5-minute link), and add one while the payment waits for verification. */
export function EvidenceFiles({ payment, onChanged }: { payment: Payment; onChanged: () => void }) {
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const open = useMutation({
    mutationFn: (f: OpsFile) => billingApi.download(f.id),
    onSuccess: (r) => window.open(r.url, "_blank", "noopener"),
    onError: (e) => toast.error(errorText(e)),
  });
  const add = useMutation({
    mutationFn: (f: File) => uploadEvidence(payment.id, f),
    onSuccess: () => {
      toast.success("Evidence added");
      setFile(null);
      onChanged();
    },
  });
  return (
    <div className="flex flex-col gap-2">
      {payment.files.length === 0 ? (
        <p className="text-[13px] text-muted">{payment.method === "cash" ? "Cash: no evidence needed." : "No evidence yet."}</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {payment.files.map((f) => (
            <li key={f.id}>
              <button
                type="button"
                onClick={() => open.mutate(f)}
                className="inline-flex max-w-full cursor-pointer items-center gap-1.5 truncate text-left text-[13px] text-accent-deep hover:underline"
                title="Download (link valid 5 minutes)"
              >
                <Download size={13} className="shrink-0" />
                <span className="truncate">{f.file_name}</span>
                {f.id === payment.evidence_file_id && <span className="text-xs text-muted">(evidence)</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      {payment.actions.attach && (
        <div className="flex flex-wrap items-center gap-2">
          <input type="file" accept="application/pdf,image/jpeg,image/png,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="min-w-0 max-w-full text-xs" />
          <Button size="sm" disabled={!file || add.isPending} onClick={() => file && add.mutate(file)}>
            <FileUp size={13} /> {add.isPending ? "Uploading…" : "Add evidence"}
          </Button>
          {add.isError && <span className="text-xs text-danger">{errorText(add.error)}</span>}
        </div>
      )}
    </div>
  );
}
