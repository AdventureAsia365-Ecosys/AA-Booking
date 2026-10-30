"use client";
// app/(staff)/cases/[code]/SuppliersTab.tsx
// AA-682 S1 (bk-026, S252 design §6) — the Suppliers tab of the case workspace: one supplier booking per room / service
// of the approved version (Generate), plus extra items added by hand. Request → Confirm (the supplier's ref, and on a
// B2C case a voucher or confirmation file) or Decline (desk code); Cancel and Re-request with a reason. The header
// shows the computed supply badge ("Confirmed & Booked" when every booking is confirmed; never a case status).
// B2B cases: a booking of an on-request line IS the desk answer (one action, design D13): confirm accepts the line and
// declining declines it, the agency sees it on its event feed; no voucher or document is stored (DM-6.8).
// The case status follows by itself (confirmed → Supplier confirming on the first request); staff never pick it.
// AA-682 S4: below the bookings, the cost vs billed panel (bill.read + pricing.read; CostVsBilled.tsx).

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Download, FileUp, Plus, Send, Sparkles } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { suppliersApi } from "@/lib/api/suppliers";
import {
  DECLINE_CODES,
  DECLINE_LABEL,
  supplierBookingsApi,
  type CaseSupplierBookings,
  type DeclineCode,
  type NewSupplierBooking,
  type OpsFile,
  type SupplierBooking,
  type UnbookedItem,
} from "@/lib/api/supplierBookings";
import type { TripCaseDetail } from "@/lib/api/types";
import { CostVsBilled } from "./CostVsBilled";
import {
  Input,
  Select,
  Textarea,
  Button,
  ConfirmDialog,
  DataTable,
  DateText,
  FormDrawer,
  Modal,
  Money,
  Pill,
  Section,
  SupplierBookingStatusBadge,
  SupplyBadge,
  useCan,
  useToast,
  RowMenu,
  type RowMenuItem,
  DateField,
} from "@/ui";
import { prepareUpload } from "../../_parts/SupplyContent";

const MAX_FILE_BYTES = 3 * 1024 * 1024;
const fieldCls = "w-full";
const SKIP_LABEL: Record<NonNullable<UnbookedItem["skip_reason"]>, string> = {
  booked: "Booked",
  no_supplier: "No supplier on this item",
  b2b_line_closed: "Agency told: declined or cancelled",
};

function errorText(e: unknown): string {
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

/** A PDF is sent as it is (≤ 3 MB); a photo is re-encoded in the browser first (no EXIF reaches S3). */
async function prepareDocument(file: File): Promise<{ content_type: string; data_base64: string }> {
  if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
    if (file.size > MAX_FILE_BYTES) throw new Error("A PDF is at most 3 MB here. Ask the supplier for a smaller file.");
    return { content_type: "application/pdf", data_base64: await toBase64(file) };
  }
  return prepareUpload(file, MAX_FILE_BYTES);
}

type Dialog =
  | { kind: "confirm"; b: SupplierBooking }
  | { kind: "decline"; b: SupplierBooking }
  | { kind: "cancel"; b: SupplierBooking }
  | { kind: "rerequest"; b: SupplierBooking }
  | { kind: "add"; item: UnbookedItem | null }
  | null;

export function SuppliersTab({ c }: { c: TripCaseDetail }) {
  const canWrite = useCan("ops.write");
  const canDesk = useCan("b2b_desk.write");
  const qc = useQueryClient();
  const toast = useToast();
  const key = ["supplier-bookings", c.code];
  const q = useQuery({ queryKey: key, queryFn: () => supplierBookingsApi.forCase(c.code) });
  const [dialog, setDialog] = useState<Dialog>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: key });
    qc.invalidateQueries({ queryKey: ["trip-case", c.code] });
  };
  const generate = useMutation({
    mutationFn: () => supplierBookingsApi.generate(c.code),
    onSuccess: (r) => {
      toast.success(r.created ? `${r.created} supplier booking(s) added from the approved version` : "Nothing new to book");
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const request = useMutation({
    mutationFn: (b: SupplierBooking) => supplierBookingsApi.request(b.id),
    onSuccess: (b) => {
      toast.success(`${b.code} marked as requested from ${b.supplier.name}`);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const close = () => setDialog(null);
  const data = q.data;
  const bookable = (data?.unbooked_items ?? []).filter((i) => i.bookable);
  const sold = !["new_inquiry", "discovery", "building", "proposal_sent", "cancelled", "completed"].includes(c.status);

  const menu = (b: SupplierBooking): RowMenuItem[] => {
    const linked = !!b.b2b_line;
    const deskOk = !linked || canDesk;
    const items: RowMenuItem[] = [];
    if (b.status === "pending") items.push({ label: "Mark as requested", onSelect: () => request.mutate(b) });
    if (b.status === "requested") {
      items.push({ label: "Confirm…", onSelect: () => setDialog({ kind: "confirm", b }), disabled: !deskOk });
      items.push({ label: "Declined by supplier…", onSelect: () => setDialog({ kind: "decline", b }), disabled: !deskOk });
    }
    if (b.status === "confirmed" && !linked) items.push({ label: "Change with supplier…", onSelect: () => setDialog({ kind: "rerequest", b }) });
    if (b.live) {
      items.push({
        label: "Cancel booking…",
        danger: true,
        divider: true,
        onSelect: () => setDialog({ kind: "cancel", b }),
        disabled: linked && b.b2b_line?.status === "confirmed",
      });
    }
    return items;
  };

  const columns: ColumnDef<SupplierBooking, unknown>[] = [
    {
      id: "item",
      header: "Booking",
      accessorFn: (r) => r.item_position ?? 999,
      cell: ({ row }) => {
        const b = row.original;
        return (
          <span className="flex min-w-0 flex-col md:max-w-[260px]">
            <span className="truncate text-[13.5px] font-medium text-ink" title={b.item_description}>
              {b.item_description}
            </span>
            <span className="text-xs text-muted">
              {b.code}
              {b.item_position ? ` · item ${b.item_position}` : " · extra item"}
            </span>
          </span>
        );
      },
    },
    {
      id: "supplier",
      header: "Supplier",
      accessorFn: (r) => r.supplier.name,
      cell: ({ row }) => (
        <Link href={`/suppliers/${encodeURIComponent(row.original.supplier.code)}?tab=bookings`} className="text-[13px] text-accent-deep hover:underline">
          {row.original.supplier.name}
        </Link>
      ),
    },
    {
      id: "date",
      header: "Service date",
      accessorFn: (r) => r.service_date ?? "",
      cell: ({ row }) => {
        const b = row.original;
        return (
          <span className="flex flex-col text-[13px] text-body">
            {b.service_date ? <DateText value={b.service_date} /> : <span className="text-muted">Not set</span>}
            <span className="text-xs text-muted">
              {b.pax ? `${b.pax} pax` : ""}
              {b.nights ? ` · ${b.nights} night${b.nights === 1 ? "" : "s"}` : ""}
            </span>
          </span>
        );
      },
    },
    {
      id: "net",
      header: "Net",
      cell: ({ row }) =>
        row.original.net_amount ? <Money amount={row.original.net_amount} currency={row.original.net_currency} /> : <span className="text-muted">—</span>,
    },
    {
      id: "status",
      header: "Status",
      accessorFn: (r) => r.status,
      cell: ({ row }) => {
        const b = row.original;
        return (
          <span className="flex flex-col gap-1">
            <SupplierBookingStatusBadge status={b.status} />
            {b.confirmation_ref && <span className="text-xs text-muted">Ref {b.confirmation_ref}</span>}
            {b.decline_code && <span className="text-xs text-muted">{DECLINE_LABEL[b.decline_code]}</span>}
            {b.cancel_reason && (
              <span className="max-w-[200px] truncate text-xs text-muted" title={b.cancel_reason}>
                {b.cancel_reason}
              </span>
            )}
            {b.b2b_line && (
              <Link href="/b2b-desk" className="text-xs text-accent-deep hover:underline" title="This booking answers an on-request line of the agency">
                B2B line {b.b2b_line.line_ref}: {b.b2b_line.status.replace("_", " ")}
              </Link>
            )}
          </span>
        );
      },
    },
    {
      id: "docs",
      header: "Documents",
      enableSorting: false,
      cell: ({ row }) => <Files b={row.original} />,
    },
    ...(canWrite
      ? [
          {
            id: "actions",
            header: "",
            enableSorting: false,
            cell: ({ row }: { row: { original: SupplierBooking } }) => {
              const items = menu(row.original);
              return items.length ? (
                <span className="flex justify-end">
                  <RowMenu items={items} label={`Actions for ${row.original.code}`} />
                </span>
              ) : null;
            },
          } satisfies ColumnDef<SupplierBooking, unknown>,
        ]
      : []),
  ];

  return (
    <div className="flex flex-col gap-5">
      <Section
        title={
          <span className="flex flex-wrap items-center gap-2">
            Supplier bookings <SupplyBadge supply={data?.supply} />
          </span>
        }
        description={
          data?.trip_case.channel === "b2b"
            ? "Each booking of an on-request line answers the agency: Confirm accepts the line, Declined declines it. No vouchers or documents for B2B trips: keep the supplier's confirmation ref only."
            : "Book every room and service of the approved version with its supplier. The case moves to Supplier confirming on the first request; when every booking is confirmed it shows Confirmed & Booked."
        }
        actions={
          canWrite && sold ? (
            <span className="flex flex-wrap gap-2">
              <Button variant="ghost" onClick={() => setDialog({ kind: "add", item: null })}>
                <Plus size={14} /> Add booking
              </Button>
              <Button variant="primary" disabled={generate.isPending || bookable.length === 0} onClick={() => generate.mutate()}>
                <Sparkles size={14} /> Generate from approved version
              </Button>
            </span>
          ) : null
        }
      >
        <DataTable<SupplierBooking>
          searchable={false}
          tableId="case-supplier-bookings"
          data={data?.items ?? []}
          columns={columns}
          getRowId={(r) => r.id}
          loading={q.isLoading}
          error={q.isError ? errorText(q.error) : null}
          onRetry={() => q.refetch()}
          emptyTitle={sold ? "No supplier booking yet" : "Nothing to book before the trip is confirmed"}
          emptyDescription={
            sold
              ? "Generate one booking per room and service of the approved version, or add one by hand."
              : "Supplier bookings start once a proposal version is approved."
          }
          tableMinWidth={canWrite ? 1040 : 980}
        />
      </Section>

      {data && data.unbooked_items.length > 0 && (
        <Section title="Items without a live booking" description="Rooms and services of the approved version that no live supplier booking covers.">
          <ul className="flex flex-col divide-y divide-line2 text-[13px]">
            {data.unbooked_items.map((i) => (
              <li key={i.version_item_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <span className="min-w-0">
                  <span className="font-medium text-ink">Item {i.position}</span> · {i.supply_label ?? i.title}
                </span>
                {i.bookable ? (
                  canWrite &&
                  sold && (
                    <Button size="sm" variant="ghost" onClick={() => setDialog({ kind: "add", item: i })}>
                      Book with another supplier
                    </Button>
                  )
                ) : (
                  <Pill tone="neutral" dot={false}>
                    {SKIP_LABEL[i.skip_reason ?? "booked"]}
                  </Pill>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <CostVsBilled c={c} />

      {dialog?.kind === "confirm" && <ConfirmBooking b={dialog.b} documents={!!data?.documents_allowed} onClose={close} onDone={refresh} />}
      {dialog?.kind === "decline" && <DeclineBooking b={dialog.b} onClose={close} onDone={refresh} />}
      {dialog?.kind === "add" && data && <AddBooking c={c} data={data} item={dialog.item} onClose={close} onDone={refresh} />}
      <ReasonDialog
        open={dialog?.kind === "cancel"}
        b={dialog?.kind === "cancel" ? dialog.b : null}
        title="Cancel supplier booking"
        label="Why is it cancelled?"
        confirmLabel="Cancel booking"
        danger
        message="Cancel it with the supplier first if it was already requested or confirmed (fees may apply). The booking stays in the list as cancelled."
        run={(b, reason) => supplierBookingsApi.cancel(b.id, reason)}
        onClose={close}
        onDone={refresh}
      />
      <ReasonDialog
        open={dialog?.kind === "rerequest"}
        b={dialog?.kind === "rerequest" ? dialog.b : null}
        title="Change with supplier"
        label="What changes?"
        confirmLabel="Back to requested"
        message="The booking goes back to Requested until the supplier confirms the change. The old confirmation ref is kept in the activity log."
        run={(b, reason) => supplierBookingsApi.rerequest(b.id, reason)}
        onClose={close}
        onDone={refresh}
      />
    </div>
  );
}

function Files({ b }: { b: SupplierBooking }) {
  const toast = useToast();
  const open = useMutation({
    mutationFn: (f: OpsFile) => supplierBookingsApi.download(f.id),
    onSuccess: (r) => window.open(r.url, "_blank", "noopener"),
    onError: (e) => toast.error(errorText(e)),
  });
  if (!b.files.length) return <span className="text-xs text-muted">—</span>;
  return (
    <span className="flex flex-col gap-1">
      {b.files.map((f) => (
        <button
          key={f.id}
          type="button"
          onClick={() => open.mutate(f)}
          className="inline-flex max-w-[180px] cursor-pointer items-center gap-1 truncate text-left text-xs text-accent-deep hover:underline"
          title={`Download ${f.file_name} (link valid 5 minutes)`}
        >
          <Download size={12} className="shrink-0" />
          <span className="truncate">{f.kind === "supplier_voucher" ? "Voucher" : "Confirmation"} · {f.file_name}</span>
        </button>
      ))}
    </span>
  );
}

function ConfirmBooking({ b, documents, onClose, onDone }: { b: SupplierBooking; documents: boolean; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [ref, setRef] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState<"supplier_voucher" | "supplier_confirmation">("supplier_voucher");
  const run = useMutation({
    mutationFn: async () => {
      let voucher: string | null = null;
      if (file) {
        const doc = await prepareDocument(file);
        const up = await supplierBookingsApi.upload({ kind, entity_id: b.id, file_name: file.name, ...doc });
        if (kind === "supplier_voucher") voucher = up.id;
      }
      return supplierBookingsApi.confirm(b.id, ref.trim(), voucher);
    },
    onSuccess: (r) => {
      toast.success(r.b2b_line ? `${r.code} confirmed; the agency is told the line is confirmed` : `${r.code} confirmed by ${r.supplier.name}`);
      onDone();
      onClose();
    },
  });
  return (
    <Modal
      open
      onClose={run.isPending ? () => {} : onClose}
      title={`Confirm ${b.code}`}
      width={520}
      footer={
        <>
          <Button onClick={onClose} disabled={run.isPending}>
            Cancel
          </Button>
          <Button variant="primary" disabled={run.isPending || !ref.trim()} onClick={() => run.mutate()}>
            <Send size={14} /> {run.isPending ? "Saving…" : "Confirm booking"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3 text-[13px] text-body">
        <p>
          {b.item_description} with <strong>{b.supplier.name}</strong>
          {b.service_date ? (
            <>
              {" "}
              on <DateText value={b.service_date} />
            </>
          ) : null}
          .{b.b2b_line ? " This also confirms the agency's on-request line." : ""}
        </p>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">
            Supplier confirmation ref <span className="text-danger">*</span>
          </span>
          <Input size="sm" className={fieldCls} value={ref} maxLength={100} onChange={(e) => setRef(e.target.value)} placeholder="e.g. HTL-48213" />
        </label>
        {documents ? (
          <div className="flex flex-col gap-2 rounded-md border border-line2 p-3">
            <span className="flex items-center gap-1.5 text-xs font-semibold text-muted">
              <FileUp size={13} /> Document (optional): PDF, or a photo of it, at most 3 MB
            </span>
            <Select size="sm" className={fieldCls} value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
              <option value="supplier_voucher">Voucher</option>
              <option value="supplier_confirmation">Supplier confirmation</option>
            </Select>
            <input type="file" accept="application/pdf,image/jpeg,image/png,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-xs" />
            <span className="text-xs text-muted">Stored privately; staff download it with a link valid 5 minutes.</span>
          </div>
        ) : (
          <p className="text-xs text-muted">B2B trip: keep the confirmation ref only. Vouchers go to the agency outside Booking.</p>
        )}
        {run.isError && <p className="text-danger">{errorText(run.error)}</p>}
      </div>
    </Modal>
  );
}

function DeclineBooking({ b, onClose, onDone }: { b: SupplierBooking; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [code, setCode] = useState<DeclineCode>("supplier_unavailable");
  const run = useMutation({
    mutationFn: () => supplierBookingsApi.decline(b.id, code),
    onSuccess: (r) => {
      toast.success(r.b2b_line ? `${r.code} declined; the agency is told` : `${r.code} declined: book another supplier for this item`);
      onDone();
      onClose();
    },
  });
  return (
    <Modal
      open
      onClose={onClose}
      title={`Declined by ${b.supplier.name}`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="danger" disabled={run.isPending} onClick={() => run.mutate()}>
            Mark declined
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3 text-[13px] text-body">
        <p>
          {b.item_description} ({b.code}).{" "}
          {b.b2b_line ? "The agency's line is declined too; a B2B line cannot be booked again." : "You can then book the item with another supplier."}
        </p>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">Reason</span>
          <Select size="sm" className={fieldCls} value={code} onChange={(e) => setCode(e.target.value as DeclineCode)}>
            {DECLINE_CODES.map((d) => (
              <option key={d} value={d}>
                {DECLINE_LABEL[d]}
              </option>
            ))}
          </Select>
        </label>
        {run.isError && <p className="text-danger">{errorText(run.error)}</p>}
      </div>
    </Modal>
  );
}

function ReasonDialog({
  open,
  b,
  title,
  label,
  confirmLabel,
  message,
  danger = false,
  run,
  onClose,
  onDone,
}: {
  open: boolean;
  b: SupplierBooking | null;
  title: string;
  label: string;
  confirmLabel: string;
  message: string;
  danger?: boolean;
  run: (b: SupplierBooking, reason: string) => Promise<SupplierBooking>;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const m = useMutation({
    mutationFn: (reason: string) => run(b!, reason),
    onSuccess: (r) => {
      toast.success(`${r.code} updated`);
      onDone();
      onClose();
    },
  });
  return (
    <ConfirmDialog
      open={open}
      onClose={() => {
        m.reset();
        onClose();
      }}
      title={b ? `${title} ${b.code}` : title}
      message={message}
      reason={{ label, required: true }}
      confirmLabel={confirmLabel}
      danger={danger}
      busy={m.isPending}
      error={m.isError ? errorText(m.error) : null}
      onConfirm={(reason) => m.mutate(reason)}
    />
  );
}

function AddBooking({
  c,
  data,
  item,
  onClose,
  onDone,
}: {
  c: TripCaseDetail;
  data: CaseSupplierBookings;
  item: UnbookedItem | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const toast = useToast();
  const suppliers = useQuery({ queryKey: ["suppliers", "active-options"], queryFn: () => suppliersApi.list({ status: "active", limit: 100 }) });
  const [form, setForm] = useState<NewSupplierBooking>({
    supplier_id: "",
    version_item_id: item?.version_item_id ?? null,
    item_description: item ? (item.supply_label ?? item.title) : "",
    service_date: null,
    net_amount: null,
    net_currency: null,
    notes: null,
  });
  const set = (patch: Partial<NewSupplierBooking>) => setForm((f) => ({ ...f, ...patch }));
  const run = useMutation({
    mutationFn: () =>
      supplierBookingsApi.create(c.code, {
        ...form,
        version_item_id: form.version_item_id || null,
        net_amount: form.net_amount || null,
        net_currency: form.net_amount ? (form.net_currency || "USD").toUpperCase() : null,
      }),
    onSuccess: (b) => {
      toast.success(`${b.code} added for ${b.supplier.name}`);
      onDone();
      onClose();
    },
  });
  const bookable = data.unbooked_items.filter((i) => i.bookable);
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={item ? `Book item ${item.position}` : "Add supplier booking"}
      submitLabel="Add booking"
      busy={run.isPending}
      dirty
      error={run.isError ? errorText(run.error) : null}
      fieldErrors={run.error instanceof ApiError ? run.error.fieldErrors : undefined}
      onSubmit={() => run.mutate()}
    >
      <div className="flex flex-col gap-4 text-[13px]">
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">
            Supplier <span className="text-danger">*</span>
          </span>
          <Select size="sm" className={fieldCls} value={form.supplier_id} onChange={(e) => set({ supplier_id: e.target.value })} required>
            <option value="">Choose a supplier…</option>
            {(suppliers.data?.items ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.company_name} ({s.code})
              </option>
            ))}
          </Select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">Item of the approved version</span>
          <Select size="sm" className={fieldCls} value={form.version_item_id ?? ""} onChange={(e) => set({ version_item_id: e.target.value || null })}>
            <option value="">Extra item (not in the proposal)</option>
            {bookable.map((i) => (
              <option key={i.version_item_id} value={i.version_item_id}>
                Item {i.position} · {i.supply_label ?? i.title}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">What is booked</span>
          <Input size="sm" className={fieldCls} maxLength={500} value={form.item_description ?? ""} onChange={(e) => set({ item_description: e.target.value })} />
        </label>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">Service date</span>
            <DateField className={fieldCls} value={form.service_date ?? ""} onChange={(v) => set({ service_date: v || null })} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">Net amount</span>
            <Input size="sm" inputMode="decimal" className={fieldCls} value={form.net_amount ?? ""} onChange={(e) => set({ net_amount: e.target.value || null })} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">Currency</span>
            <Input size="sm" className={fieldCls} maxLength={3} placeholder="USD" value={form.net_currency ?? ""} onChange={(e) => set({ net_currency: e.target.value.toUpperCase() || null })} />
          </label>
        </div>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">Notes</span>
          <Textarea className="min-h-[72px] w-full" maxLength={2000} value={form.notes ?? ""} onChange={(e) => set({ notes: e.target.value || null })} />
          {c.channel === "b2b" && <span className="text-xs text-muted">B2B trip: never write traveller names here.</span>}
        </label>
        <p className="text-xs text-muted">For an item of the proposal, empty fields take the item&apos;s date, nights, pax and net cost.</p>
      </div>
    </FormDrawer>
  );
}
