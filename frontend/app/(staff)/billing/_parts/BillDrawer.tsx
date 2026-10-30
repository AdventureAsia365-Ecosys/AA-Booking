"use client";
// app/(staff)/billing/_parts/BillDrawer.tsx
// AA-682 S4 (bk-030, S252 design §6) — the supplier bill drawer (facts, lines matched to confirmed supplier bookings,
// the supplier's document, payouts), the Enter / Edit bill form (one Idempotency-Key per opening; lines picked from the
// supplier's confirmed bookings), Approve (another person: the one who entered it, or last changed its amounts or lines,
// gets "maker_checker"), Cancel with a reason (never "Delete", Q23), and the Record payout modal (Idempotency-Key, bank
// slip uploaded right after; an admin, never the person who entered the bill or last changed its amounts). "Paid" is never set by hand: the database derives it from the payouts.

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Download, FileUp, Pencil } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { newIdempotencyKey, PAYMENT_METHOD_LABEL, type PaymentMethod } from "@/lib/api/billing";
import { suppliersApi } from "@/lib/api/suppliers";
import type { OpsFile } from "@/lib/api/supplierBookings";
import {
  APPROVE_BLOCKER_LABEL,
  supplierBillsApi,
  type BillableBooking,
  type SupplierBillDetail,
  type SupplierBillLine,
  type SupplierPayout,
} from "@/lib/api/supplierBills";
import {
  Checkbox,
  Input,
  Select,
  Textarea,
  Button,
  ConfirmDialog,
  DataTable,
  DateText,
  Drawer,
  FormDrawer,
  Modal,
  Money,
  PayoutStateBadge,
  SupplierBillStatusBadge,
  formatMoney,
  useCan,
  useToast,
  DateField,
} from "@/ui";
import { fromCents, sumCents, toCents } from "./money";
import { errorText, fieldCls, prepareEvidence, today } from "./PaymentDialogs";
import { formatDay } from "@/ui/booking/dates";

const AMOUNT_RE = /^\d{1,14}(\.\d{1,2})?$/;

function Field({ label, required = false, children, hint }: { label: string; required?: boolean; children: React.ReactNode; hint?: string }) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-xs font-semibold text-muted">
        {label} {required && <span className="text-danger">*</span>}
      </span>
      {children}
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </label>
  );
}

async function uploadFor(kind: "bill" | "payout", entityId: string, file: File): Promise<OpsFile> {
  const doc = await prepareEvidence(file);
  const body = { entity_id: entityId, file_name: file.name, ...doc };
  return kind === "bill" ? supplierBillsApi.uploadDocument(body) : supplierBillsApi.uploadPayoutEvidence(body);
}

function Files({
  files,
  current,
  canRemove,
  onChanged,
}: {
  files: OpsFile[];
  current: string | null;
  canRemove: boolean;
  onChanged: () => void;
}) {
  const toast = useToast();
  const open = useMutation({
    mutationFn: (f: OpsFile) => supplierBillsApi.download(f.id),
    onSuccess: (r) => window.open(r.url, "_blank", "noopener"),
    onError: (e) => toast.error(errorText(e)),
  });
  // Removal is final (bk-026) and goes into the audit log with the reason the user types (as Cancel / Void).
  const [removing, setRemoving] = useState<OpsFile | null>(null);
  const remove = useMutation({
    mutationFn: ({ f, reason }: { f: OpsFile; reason: string }) => supplierBillsApi.removeFile(f.id, reason),
    onSuccess: () => {
      toast.success("File removed");
      setRemoving(null);
      onChanged();
    },
  });
  if (files.length === 0) return <p className="text-[13px] text-muted">No file yet.</p>;
  return (
    <>
    <ul className="flex flex-col gap-1">
      {files.map((f) => (
        <li key={f.id} className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            onClick={() => open.mutate(f)}
            className="inline-flex min-w-0 cursor-pointer items-center gap-1.5 truncate text-left text-[13px] text-accent-deep hover:underline"
            title="Download (link valid 5 minutes)"
          >
            <Download size={13} className="shrink-0" />
            <span className="truncate">{f.file_name}</span>
            {f.id === current && <span className="text-xs text-muted">(current)</span>}
          </button>
          {canRemove && f.id !== current && (
            <button type="button" className="text-xs text-muted hover:text-danger" onClick={() => setRemoving(f)} disabled={remove.isPending}>
              Remove
            </button>
          )}
        </li>
      ))}
    </ul>
    <ConfirmDialog
      open={!!removing}
      onClose={() => {
        setRemoving(null);
        remove.reset();
      }}
      onConfirm={(reason) => removing && remove.mutate({ f: removing, reason })}
      title={removing ? `Remove ${removing.file_name}` : "Remove file"}
      confirmLabel="Remove file"
      danger
      busy={remove.isPending}
      error={remove.isError ? errorText(remove.error) : null}
      reason={{ label: "Why is it removed?", required: true, placeholder: "e.g. wrong file, replaced by a clearer scan" }}
      message="The file leaves this record for good (a removal is final); the reason goes into the audit log."
    />
    </>
  );
}

function AddFile({ label, onUpload }: { label: string; onUpload: (f: File) => Promise<unknown> }) {
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const add = useMutation({
    mutationFn: (f: File) => onUpload(f),
    onSuccess: () => {
      toast.success("File added");
      setFile(null);
    },
  });
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input type="file" accept="application/pdf,image/jpeg,image/png,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="min-w-0 max-w-full text-xs" />
      <Button size="sm" disabled={!file || add.isPending} onClick={() => file && add.mutate(file)}>
        <FileUp size={13} /> {add.isPending ? "Uploading…" : label}
      </Button>
      {add.isError && <span className="text-xs text-danger">{errorText(add.error)}</span>}
    </div>
  );
}

function lineColumns(currency: string, canPricing: boolean): ColumnDef<SupplierBillLine, unknown>[] {
  return [
    {
      id: "booking",
      header: "Booking",
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <span className="block max-w-[220px] truncate text-[13px] text-ink" title={row.original.item_description}>
            {row.original.item_description}
          </span>
          <span className="text-xs text-muted">
            {row.original.code}
            {row.original.service_date ? ` · ${formatDay(row.original.service_date)}` : ""}
          </span>
        </span>
      ),
    },
    { id: "case", header: "Trip case", cell: ({ row }) => <span className="text-[13px]">{row.original.trip_case_code}</span> },
    ...(canPricing
      ? [
          {
            id: "net",
            header: "Expected net",
            cell: ({ row }) => <Money amount={row.original.net_amount} currency={row.original.net_currency} className="text-[13px]" />,
          } satisfies ColumnDef<SupplierBillLine, unknown>,
        ]
      : []),
    { id: "billed", header: "Billed", cell: ({ row }) => <Money amount={row.original.amount} currency={currency} className="text-[13px]" /> },
  ];
}

// ── Drawer ───────────────────────────────────────────────────────────────────

type Dialog = { kind: "approve" } | { kind: "cancel" } | { kind: "pay" } | { kind: "edit" } | { kind: "void"; payout: SupplierPayout } | null;

export function BillDrawer({ billRef, onClose }: { billRef: string | null; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const canPricing = useCan("pricing.read");
  const q = useQuery({ queryKey: ["supplier-bill", billRef], queryFn: () => supplierBillsApi.get(billRef as string), enabled: !!billRef });
  const [dialog, setDialog] = useState<Dialog>(null);
  const b = q.data;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["supplier-bill", billRef] });
    qc.invalidateQueries({ queryKey: ["supplier-bills"] });
  };
  const approve = useMutation({
    mutationFn: (id: string) => supplierBillsApi.approve(id),
    onSuccess: (r) => {
      toast.success(`${r.code} approved: an admin can now record the payout`);
      refresh();
      setDialog(null);
    },
  });
  const cancel = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => supplierBillsApi.cancel(id, reason),
    onSuccess: (r) => {
      toast.success(`${r.code} cancelled`);
      refresh();
      setDialog(null);
    },
  });
  const voidPayout = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => supplierBillsApi.voidPayout(id, reason),
    onSuccess: (r) => {
      toast.success(`${r.code} voided: the bill is back to "To pay"`);
      refresh();
      setDialog(null);
    },
  });

  return (
    <Drawer open={!!billRef} onClose={onClose} title={b ? `${b.code} · ${b.supplier.name}` : "Supplier bill"} width={640}>
      {q.isLoading && <p className="text-[13px] text-muted">Loading…</p>}
      {q.isError && <p className="text-[13px] text-danger">{errorText(q.error)}</p>}
      {b && (
        <div className="flex flex-col gap-5 text-[13px] text-body">
          <div className="flex flex-wrap items-center gap-2">
            <SupplierBillStatusBadge status={b.status} overdue={b.overdue} />
            <span className="text-muted">
              Supplier bill <strong className="text-ink">{b.bill_number}</strong>
            </span>
            {b.trip_case && (
              <Link href={`/cases/${encodeURIComponent(b.trip_case.code)}?tab=suppliers`} className="text-accent-deep hover:underline">
                {b.trip_case.code}
              </Link>
            )}
          </div>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
            <Fact label="Amount">
              <Money amount={b.amount} currency={b.currency} strong />
            </Fact>
            <Fact label="Paid out">
              <Money amount={b.paid_amount} currency={b.currency} />
            </Fact>
            <Fact label="Left to pay">
              <Money amount={b.balance} currency={b.currency} strong />
            </Fact>
            <Fact label="Issued">
              <DateText value={b.issue_date} />
            </Fact>
            <Fact label="Due">
              <DateText value={b.due_date} className={b.overdue ? "font-semibold text-danger" : ""} />
            </Fact>
            <Fact label="Entered by">{b.created_by?.name ?? b.created_by?.id ?? "—"}</Fact>
            {b.facts_edited_by && b.facts_edited_by.id !== b.created_by?.id && (
              <Fact label="Amounts last changed by">{b.facts_edited_by.name ?? b.facts_edited_by.id}</Fact>
            )}
            {b.approved_by && <Fact label="Approved by">{b.approved_by.name ?? b.approved_by.id}</Fact>}
            {b.cancel_reason && <Fact label="Cancelled because">{b.cancel_reason}</Fact>}
          </dl>
          {b.notes && <p className="whitespace-pre-wrap rounded-md bg-surface px-3 py-2 text-[13px]">{b.notes}</p>}

          <div className="flex flex-wrap gap-2">
            {b.actions.edit && (
              <Button variant="ghost" onClick={() => setDialog({ kind: "edit" })}>
                <Pencil size={13} /> Edit
              </Button>
            )}
            {b.status === "received" && (
              <Button variant="primary" disabled={!b.actions.approve} onClick={() => setDialog({ kind: "approve" })}>
                Approve
              </Button>
            )}
            {b.actions.pay && (
              <Button variant="primary" onClick={() => setDialog({ kind: "pay" })}>
                Record payout
              </Button>
            )}
            {b.actions.cancel && (
              <Button variant="ghost" onClick={() => setDialog({ kind: "cancel" })}>
                Cancel bill
              </Button>
            )}
          </div>
          {b.status === "received" && b.approve_blockers.length > 0 && (
            <ul className="list-disc pl-5 text-xs text-muted">
              {b.approve_blockers.map((k) => (
                <li key={k}>{APPROVE_BLOCKER_LABEL[k]}</li>
              ))}
            </ul>
          )}
          {b.status === "approved" && !b.actions.pay && (
            <p className="text-xs text-muted">An admin who did not enter this bill or change its amounts records the payout.</p>
          )}

          <section className="flex flex-col gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Lines (confirmed supplier bookings)</h3>
            {b.lines.length === 0 ? (
              <p className="text-muted">No line yet: edit the bill and pick the bookings it covers.</p>
            ) : (
              <DataTable<SupplierBillLine>
                searchable={false}
                tableId="supplier-bill-lines"
                data={b.lines}
                columns={lineColumns(b.currency, canPricing)}
                getRowId={(l) => l.supplier_booking_id}
                tableMinWidth={520}
              />
            )}
            <p className="text-xs text-muted">
              Lines total {formatMoney(b.lines_amount, b.currency)} of {formatMoney(b.amount, b.currency)}.
            </p>
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Supplier&apos;s document</h3>
            <Files files={b.files} current={b.document_file_id} canRemove={b.actions.attach} onChanged={refresh} />
            {b.actions.attach &&
              (b.trip_case?.channel === "b2b" || b.lines.some((l) => l.trip_case_channel === "b2b") ? (
                <p className="text-xs text-muted">A bill for a B2B trip keeps no document (traveller names stay in the agency&apos;s CRM).</p>
              ) : (
                <AddFile label="Add document" onUpload={async (f) => { await uploadFor("bill", b.id, f); refresh(); }} />
              ))}
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Payouts</h3>
            {b.payouts.length === 0 ? (
              <p className="text-muted">No payout yet.</p>
            ) : (
              <ul className="flex flex-col divide-y divide-line2">
                {b.payouts.map((p) => (
                  <li key={p.id} className="flex flex-col gap-1.5 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <strong className="text-ink">{p.code}</strong>
                      <Money amount={p.amount} currency={p.currency} strong />
                      <span className="text-muted">
                        {PAYMENT_METHOD_LABEL[p.method]} · <DateText value={p.paid_on} />
                        {p.reference ? ` · ref ${p.reference}` : ""}
                      </span>
                      <PayoutStateBadge state={p.state} />
                      {p.needs_evidence && <span className="text-xs text-warning">No bank slip yet</span>}
                    </div>
                    <span className="text-xs text-muted">
                      Recorded by {p.recorded_by?.name ?? p.recorded_by?.id ?? "—"}
                      {p.void_reason ? ` · voided: ${p.void_reason}` : ""}
                    </span>
                    <Files files={p.files} current={p.evidence_file_id} canRemove={p.actions.attach} onChanged={refresh} />
                    <div className="flex flex-wrap items-center gap-2">
                      {p.actions.attach && (
                        <AddFile label="Add bank slip" onUpload={async (f) => { await uploadFor("payout", p.id, f); refresh(); }} />
                      )}
                      {p.actions.void && (
                        <Button size="sm" variant="ghost" onClick={() => setDialog({ kind: "void", payout: p })}>
                          Void payout
                        </Button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <ConfirmDialog
            open={dialog?.kind === "approve"}
            onClose={() => setDialog(null)}
            onConfirm={() => approve.mutate(b.id)}
            title={`Approve ${b.code}`}
            confirmLabel="Approve bill"
            busy={approve.isPending}
            error={approve.isError ? errorText(approve.error) : null}
            message={
              <>
                You checked the supplier&apos;s bill <strong>{b.bill_number}</strong> against the confirmed bookings: AA owes{" "}
                <strong>{formatMoney(b.amount, b.currency)}</strong> to {b.supplier.name}, due {formatDay(b.due_date)}. Entered by{" "}
                {b.created_by?.name ?? "someone else"}
                {b.facts_edited_by && b.facts_edited_by.id !== b.created_by?.id
                  ? `; amounts last changed by ${b.facts_edited_by.name ?? "someone else"}`
                  : ""}
                .
              </>
            }
          />
          <ConfirmDialog
            open={dialog?.kind === "cancel"}
            onClose={() => setDialog(null)}
            onConfirm={(reason) => cancel.mutate({ id: b.id, reason })}
            title={`Cancel ${b.code}`}
            confirmLabel="Cancel bill"
            danger
            busy={cancel.isPending}
            error={cancel.isError ? errorText(cancel.error) : null}
            reason={{ label: "Why is it cancelled?", required: true, placeholder: "e.g. entered twice, the supplier sent a corrected bill" }}
            message="The bill stays in the list as cancelled; its number can be entered again. A cancel is final."
          />
          <ConfirmDialog
            open={dialog?.kind === "void"}
            onClose={() => setDialog(null)}
            onConfirm={(reason) => dialog?.kind === "void" && voidPayout.mutate({ id: dialog.payout.id, reason })}
            title={dialog?.kind === "void" ? `Void ${dialog.payout.code}` : "Void payout"}
            confirmLabel="Void payout"
            danger
            busy={voidPayout.isPending}
            error={voidPayout.isError ? errorText(voidPayout.error) : null}
            reason={{ label: "Why is it voided?", required: true, placeholder: "e.g. recorded twice, the transfer was returned" }}
            message={
              dialog?.kind === "void" ? (
                <>
                  {formatMoney(dialog.payout.amount, dialog.payout.currency)} stops counting on {b.code}; the bill goes back
                  to To pay. A void is final.
                </>
              ) : null
            }
          />
          {dialog?.kind === "pay" && <PayoutModal bill={b} onClose={() => setDialog(null)} onDone={refresh} />}
          {dialog?.kind === "edit" && (
            <BillForm open supplier={b.supplier} bill={b} onClose={() => setDialog(null)} onSaved={() => refresh()} />
          )}
        </div>
      )}
    </Drawer>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="truncate text-[13px] text-ink">{children}</dd>
    </div>
  );
}

// ── Enter / edit ─────────────────────────────────────────────────────────────

interface LineState {
  checked: boolean;
  amount: string;
}

function suggested(b: BillableBooking, currency: string): string {
  if (!b.net_amount || (b.net_currency ?? "").trim() !== currency) return "";
  const net = toCents(b.net_amount);
  const billed = toCents(b.billed.find((x) => x.currency === currency)?.amount ?? "0");
  if (net === null || billed === null) return "";
  const left = net - billed;
  return left > BigInt(0) ? fromCents(left) : "";
}

/** Enter a bill (supplier fixed when opened from a supplier) or edit a received one (facts and lines; notes only once
 *  approved). Lines are the supplier's confirmed bookings; their total must reach the bill amount before approval. */
export function BillForm({
  open,
  supplier,
  bill,
  tripCase,
  onClose,
  onSaved,
}: {
  open: boolean;
  supplier: { id: string; name: string } | null;
  bill?: SupplierBillDetail;
  tripCase?: string;
  onClose: () => void;
  onSaved: (b: SupplierBillDetail) => void;
}) {
  const toast = useToast();
  const editing = !!bill;
  const [key] = useState(() => newIdempotencyKey("bill"));
  const suppliers = useQuery({
    queryKey: ["suppliers", "active-options"],
    queryFn: () => suppliersApi.list({ status: "active", limit: 100 }),
    enabled: open && !supplier,
  });
  const [supplierId, setSupplierId] = useState(supplier?.id ?? "");
  const [caseRef, setCaseRef] = useState(bill?.trip_case?.code ?? tripCase ?? "");
  const [number, setNumber] = useState(bill?.bill_number ?? "");
  const [currency, setCurrency] = useState(bill?.currency.trim() ?? "USD");
  const [amount, setAmount] = useState(bill?.amount ?? "");
  const [issue, setIssue] = useState(bill?.issue_date ?? today());
  const [due, setDue] = useState(bill?.due_date ?? today());
  const [notes, setNotes] = useState(bill?.notes ?? "");
  const [file, setFile] = useState<File | null>(null);
  const [lines, setLines] = useState<Record<string, LineState>>(() =>
    Object.fromEntries((bill?.lines ?? []).map((l) => [l.supplier_booking_id, { checked: true, amount: l.amount }])),
  );
  const billable = useQuery({
    queryKey: ["supplier-bills", "billable", supplierId, bill?.trip_case?.code ?? (caseRef || null)],
    queryFn: () => supplierBillsApi.billable({ supplier_id: supplierId, case: (bill?.trip_case?.code ?? caseRef) || undefined }),
    enabled: open && !!supplierId,
  });
  const options = useMemo(() => {
    const list = [...(billable.data?.items ?? [])];
    for (const l of bill?.lines ?? []) {
      if (!list.some((x) => x.id === l.supplier_booking_id)) {
        list.push({
          id: l.supplier_booking_id,
          code: l.code,
          item_description: l.item_description,
          service_date: l.service_date,
          nights: l.nights,
          pax: l.pax,
          net_amount: l.net_amount,
          net_currency: l.net_currency,
          confirmation_ref: null,
          trip_case_id: l.trip_case_id,
          trip_case_code: l.trip_case_code,
          trip_case_channel: l.trip_case_channel,
          billed: [],
        });
      }
    }
    return list;
  }, [billable.data, bill]);
  const locked = editing && bill.status !== "received";
  const picked = Object.entries(lines).filter(([, v]) => v.checked);
  const linesTotal = sumCents(picked.map(([, v]) => toCents(v.amount)));
  const amountCents = toCents(amount);
  const mismatch = picked.length > 0 && linesTotal !== null && amountCents !== null && linesTotal !== amountCents;
  const hasB2b = options.some((o) => lines[o.id]?.checked && o.trip_case_channel === "b2b");

  const save = useMutation({
    mutationFn: async () => {
      const lineBody = picked.map(([id, v]) => ({ supplier_booking_id: id, amount: v.amount.trim() }));
      let out: SupplierBillDetail;
      if (editing) {
        out = locked
          ? await supplierBillsApi.update(bill.id, { notes: notes.trim() || null })
          : await supplierBillsApi.update(bill.id, {
              bill_number: number.trim(),
              currency: currency.trim().toUpperCase(),
              amount: amount.trim(),
              issue_date: issue,
              due_date: due,
              notes: notes.trim() || null,
              lines: lineBody,
            });
      } else {
        out = await supplierBillsApi.create(
          {
            supplier_id: supplierId,
            trip_case: caseRef.trim() || null,
            bill_number: number.trim(),
            currency: currency.trim().toUpperCase(),
            amount: amount.trim(),
            issue_date: issue,
            due_date: due,
            notes: notes.trim() || null,
            lines: lineBody,
          },
          key,
        );
      }
      let fileError: string | null = null;
      if (file && out.status === "received") {
        try {
          await uploadFor("bill", out.id, file);
        } catch (e) {
          fileError = errorText(e);
        }
      }
      return { out, fileError };
    },
    onSuccess: ({ out, fileError }) => {
      toast.success(editing ? `${out.code} saved` : `${out.code} entered: another person approves it`);
      if (fileError) toast.error(`The document was not added: ${fileError}. Add it from the bill.`);
      onSaved(out);
      onClose();
    },
  });
  const valid =
    !!supplierId &&
    (locked ||
      (number.trim().length > 0 &&
        /^[A-Za-z]{3}$/.test(currency.trim()) &&
        AMOUNT_RE.test(amount.trim()) &&
        Number(amount) > 0 &&
        !!issue &&
        !!due &&
        due >= issue &&
        picked.every(([, v]) => AMOUNT_RE.test(v.amount.trim()) && Number(v.amount) > 0)));
  const error = save.isError ? errorText(save.error) : null;
  const fieldErrors = save.error instanceof ApiError ? save.error.fieldErrors : [];

  return (
    <FormDrawer
      open={open}
      onClose={onClose}
      title={editing ? `Edit ${bill.code}` : "Enter a supplier bill"}
      onSubmit={() => valid && save.mutate()}
      submitLabel={editing ? "Save" : "Enter bill"}
      busy={save.isPending}
      error={error}
      fieldErrors={fieldErrors}
      width={600}
    >
      <div className="flex flex-col gap-3 text-[13px] text-body">
        {locked && <p className="text-xs text-muted">This bill is approved: only its notes change. Cancel it to enter a corrected one.</p>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Supplier" required>
            {supplier || editing ? (
              <Input size="sm" className={fieldCls} value={supplier?.name ?? bill?.supplier.name ?? ""} disabled />
            ) : (
              <Select size="sm"
                className={fieldCls}
                value={supplierId}
                onChange={(e) => {
                  setSupplierId(e.target.value);
                  setLines({});
                }}
              >
                <option value="">Choose a supplier</option>
                {(suppliers.data?.items ?? []).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.company_name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label="Trip case" hint="Optional: leave empty for a statement covering several trips.">
            <Input size="sm"
              className={fieldCls}
              value={caseRef}
              disabled={editing}
              placeholder="TC-000123"
              onChange={(e) => {
                setCaseRef(e.target.value.toUpperCase());
                setLines({});
              }}
            />
          </Field>
          <Field label="Supplier's bill number" required>
            <Input size="sm" className={fieldCls} value={number} maxLength={100} disabled={locked} onChange={(e) => setNumber(e.target.value)} />
          </Field>
          <div className="grid grid-cols-[1fr_96px] gap-2">
            <Field label="Amount" required>
              <Input size="sm" className={fieldCls} inputMode="decimal" value={amount} disabled={locked} onChange={(e) => setAmount(e.target.value)} placeholder="0.00" />
            </Field>
            <Field label="Currency" required>
              <Input size="sm" className={fieldCls} value={currency} maxLength={3} disabled={locked} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
            </Field>
          </div>
          <Field label="Issue date" required>
            <DateField className={fieldCls} value={issue} disabled={locked} onChange={(v) => setIssue(v)} />
          </Field>
          <Field label="Due date" required>
            <DateField className={fieldCls} value={due} disabled={locked} onChange={(v) => setDue(v)} />
          </Field>
        </div>

        {!locked && (
          <div className="flex flex-col gap-2 rounded-md border border-line2 p-3">
            <span className="text-xs font-semibold text-muted">Bookings this bill covers (confirmed, this supplier)</span>
            {!supplierId ? (
              <p className="text-xs text-muted">Choose the supplier first.</p>
            ) : billable.isLoading ? (
              <p className="text-xs text-muted">Loading bookings…</p>
            ) : options.length === 0 ? (
              <p className="text-xs text-muted">No confirmed booking with this supplier{caseRef ? ` on ${caseRef}` : ""}.</p>
            ) : (
              <ul className="flex flex-col divide-y divide-line2">
                {options.map((o) => {
                  const st = lines[o.id] ?? { checked: false, amount: "" };
                  return (
                    <li key={o.id} className="flex flex-wrap items-center gap-2 py-1.5">
                      <Checkbox
                        aria-label={`Bill ${o.code}`}
                        checked={st.checked}
                        onChange={(e) =>
                          setLines((prev) => ({
                            ...prev,
                            [o.id]: { checked: e.target.checked, amount: st.amount || suggested(o, currency.trim()) },
                          }))
                        }
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-ink" title={o.item_description}>
                          {o.item_description}
                        </span>
                        <span className="text-xs text-muted">
                          {o.code} · {o.trip_case_code}
                          {o.service_date ? ` · ${formatDay(o.service_date)}` : ""}
                          {o.net_amount ? ` · net ${formatMoney(o.net_amount, o.net_currency)}` : ""}
                          {o.billed.length ? ` · already billed ${o.billed.map((x) => formatMoney(x.amount, x.currency)).join(" + ")}` : ""}
                        </span>
                      </span>
                      <Input size="sm"
                        className={`${fieldCls} w-[120px]`}
                        inputMode="decimal"
                        disabled={!st.checked}
                        value={st.amount}
                        aria-label={`Amount for ${o.code}`}
                        onChange={(e) => setLines((prev) => ({ ...prev, [o.id]: { checked: true, amount: e.target.value } }))}
                      />
                    </li>
                  );
                })}
              </ul>
            )}
            {picked.length > 0 && (
              <p className={`text-xs ${mismatch ? "text-warning" : "text-muted"}`}>
                Lines total {linesTotal === null ? "—" : formatMoney(fromCents(linesTotal), currency)}
                {mismatch ? ` — not the bill amount ${formatMoney(amount, currency)} yet: it can be saved, but not approved.` : "."}
              </p>
            )}
          </div>
        )}

        <Field label="Notes" hint={hasB2b || bill?.trip_case?.channel === "b2b" ? "No traveller names on a B2B trip." : undefined}>
          <Textarea rows={2} maxLength={2000} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </Field>
        {!locked && !hasB2b && (
          <Field label="Supplier's document (PDF or a photo, at most 3 MB)" hint="Optional now; the approver checks it. Stored privately.">
            <input type="file" accept="application/pdf,image/jpeg,image/png,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-xs" />
          </Field>
        )}
      </div>
    </FormDrawer>
  );
}

// ── Payout ───────────────────────────────────────────────────────────────────

/** Record money paid to the supplier. One Idempotency-Key per opening: a retry of the same save returns the same payout. */
export function PayoutModal({ bill, onClose, onDone }: { bill: SupplierBillDetail; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [key] = useState(() => newIdempotencyKey("spo"));
  const [amount, setAmount] = useState(bill.balance);
  const [method, setMethod] = useState<PaymentMethod>("bank_transfer");
  const [paidOn, setPaidOn] = useState(today);
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [allowDuplicate, setAllowDuplicate] = useState(false);
  const run = useMutation({
    mutationFn: async () => {
      const p = await supplierBillsApi.pay(
        bill.id,
        {
          amount: amount.trim(),
          method,
          paid_on: paidOn,
          reference: reference.trim() || null,
          note: note.trim() || null,
          allow_duplicate_reference: allowDuplicate,
        },
        key,
      );
      let fileError: string | null = null;
      if (file && p.state === "live" && !p.evidence_file_id) {
        try {
          await uploadFor("payout", p.id, file);
        } catch (e) {
          fileError = errorText(e);
        }
      }
      return { p, fileError };
    },
    onSuccess: ({ p, fileError }) => {
      toast.success(`${p.code} recorded: ${p.bill.status === "paid" ? "the bill is paid" : `${formatMoney(p.bill.balance, p.currency)} left to pay`}`);
      if (fileError) toast.error(`The bank slip was not added: ${fileError}. Add it from the payout.`);
      onDone();
      onClose();
    },
  });
  const duplicate = run.error instanceof ApiError && run.error.code === "duplicate_reference";
  const valid = AMOUNT_RE.test(amount.trim()) && Number(amount) > 0 && !!paidOn;
  return (
    <Modal open onClose={onClose} title={`Record a payout on ${bill.code}`} width={560}>
      <div className="flex flex-col gap-3 text-[13px] text-body">
        <p className="text-xs text-muted">
          {bill.supplier.name} · bill {bill.bill_number}. Left to pay: <Money amount={bill.balance} currency={bill.currency} />. The bill
          reads Paid once the payouts reach its amount.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={`Amount (${bill.currency})`} required>
            <Input size="sm" className={fieldCls} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
          </Field>
          <Field label="Method">
            <Select size="sm" className={fieldCls} value={method} onChange={(e) => setMethod(e.target.value as PaymentMethod)}>
              {Object.entries(PAYMENT_METHOD_LABEL).map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Day the money left" required>
            <DateField className={fieldCls} value={paidOn} onChange={(v) => setPaidOn(v)} />
          </Field>
          <Field label="Bank reference" hint="Never a card number.">
            <Input size="sm" className={fieldCls} value={reference} maxLength={100} onChange={(e) => setReference(e.target.value)} />
          </Field>
        </div>
        <Field label="Note">
          <Textarea rows={2} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <div className="flex flex-col gap-2 rounded-md border border-line2 p-3">
          <span className="flex items-center gap-1.5 text-xs font-semibold text-muted">
            <FileUp size={13} /> Bank slip (PDF or a photo, at most 3 MB)
          </span>
          <input type="file" accept="application/pdf,image/jpeg,image/png,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-xs" />
          <span className="text-xs text-muted">Kept with the payout as evidence. Stored privately.</span>
        </div>
        {run.isError && (
          <div className="text-danger">
            <p>{errorText(run.error)}</p>
            {duplicate && (
              <label className="mt-1 flex items-center gap-2 text-xs text-body">
                <Checkbox checked={allowDuplicate} onChange={(e) => setAllowDuplicate(e.target.checked)} /> It is another transfer
                with the same reference: record it anyway
              </label>
            )}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={run.isPending}>
            Cancel
          </Button>
          <Button variant="primary" disabled={run.isPending || !valid} onClick={() => run.mutate()}>
            {run.isPending ? "Saving…" : "Record payout"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
