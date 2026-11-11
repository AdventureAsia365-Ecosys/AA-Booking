"use client";
// app/(staff)/finance/_parts/OrdersTab.tsx
// AA-1074 (bk-045, bk-06) — supplier purchase orders. "Create orders" groups the pending supplier bookings of the
// trip cases (and departures) staff name into one draft order per supplier + currency + service month. A row opens
// the order drawer (?order=PO-…): its lines, the printable order, Send (the e-mail job; outside prod it goes to the
// staff inbox) and Cancel. The suppliers' answers are recorded on each booking; confirmed / partly confirmed follow.
// B2C history gate: the API orders the bookings of B2B cases and departures only (a b2c case is never ordered).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import { ORDER_STATE_LABEL, financeApi, type OrderLine, type OrderState, type SupplierOrder } from "@/lib/api/finance";
import { Button, DataTable, DateText, Drawer, Field, FilterBar, Input, Modal, Money, Pill, Section, Textarea, useCan, useToast, useUrlFilters } from "@/ui";
import { ReasonDialog } from "./DeparturesTab";

function errorText(e: unknown, fallback = "Something went wrong"): string {
  return e instanceof ApiError ? e.message : fallback;
}

const STATE_TONE: Record<OrderState, "neutral" | "info" | "success" | "warning" | "danger"> = {
  draft: "neutral",
  sending: "info",
  sent: "info",
  partly_confirmed: "warning",
  confirmed: "success",
  cancelled: "danger",
};

function StatePill({ state }: { state: OrderState }) {
  return (
    <Pill tone={STATE_TONE[state]} dot={false}>
      {ORDER_STATE_LABEL[state]}
    </Pill>
  );
}

const COLUMNS: ColumnDef<SupplierOrder, unknown>[] = [
  {
    id: "code",
    header: "Order",
    cell: ({ row }) => (
      <span className="flex flex-col">
        <span className="font-semibold whitespace-nowrap">{row.original.code}</span>
        <span className="text-xs text-muted">{row.original.service_month.slice(0, 7)}</span>
      </span>
    ),
  },
  { id: "supplier", header: "Supplier", cell: ({ row }) => <span className="block max-w-[220px] truncate text-[13px]">{row.original.supplier.name}</span> },
  { id: "lines", header: "Lines", cell: ({ row }) => (typeof row.original.lines === "number" ? row.original.lines : row.original.lines.length) },
  {
    id: "net",
    header: "Net",
    cell: ({ row }) => {
      const amount = row.original.total_net ?? row.original.live_net;
      return amount ? <Money amount={amount} currency={row.original.currency} className="text-[13px]" /> : "—";
    },
  },
  { id: "sent", header: "Sent", cell: ({ row }) => (row.original.sent_at ? <DateText value={row.original.sent_at} /> : "—") },
  { id: "state", header: "Status", cell: ({ row }) => <StatePill state={row.original.state} /> },
];

export function OrdersTab() {
  const canWrite = useCan("ops.write");
  const { get, setMany } = useUrlFilters();
  const [creating, setCreating] = useState(false);
  const q = { status: get("order_status") || "open", limit: 100 };
  const list = useQuery({ queryKey: ["finance", "orders", q], queryFn: () => financeApi.orders(q) });
  return (
    <Section
      title="Purchase orders"
      description="One order per supplier, currency and service month, grouping the pending supplier bookings of trip cases and departures. Sending it asks the supplier for every line at once."
      actions={
        canWrite ? (
          <Button variant="primary" onClick={() => setCreating(true)}>
            Create orders
          </Button>
        ) : null
      }
    >
      <DataTable<SupplierOrder>
        filters={
          <FilterBar
            selects={[
              {
                key: "order_status",
                label: "Status",
                allLabel: "Open (not cancelled)",
                options: [
                  { value: "draft", label: "Draft" },
                  { value: "sent", label: "Sent" },
                  { value: "cancelled", label: "Cancelled" },
                ],
                width: 190,
              },
            ]}
          />
        }
        searchable={false}
        enableCsv
        csvFilename="purchase-orders.csv"
        tableId="finance-orders"
        data={list.data?.items ?? []}
        columns={COLUMNS}
        getRowId={(r) => r.id}
        onRowClick={(r) => setMany({ order: r.code })}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error, "Could not load the purchase orders") : null}
        onRetry={() => list.refetch()}
        emptyTitle="No purchase order"
        emptyDescription="Create orders from the pending supplier bookings of sold trip cases."
        tableMinWidth={760}
      />
      <OrderDrawer orderRef={get("order") || null} onClose={() => setMany({ order: null })} />
      {creating && <CreateOrdersModal onClose={() => setCreating(false)} onCreated={(code) => setMany({ order: code })} />}
    </Section>
  );
}

function CreateOrdersModal({ onClose, onCreated }: { onClose: () => void; onCreated: (code: string | null) => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [cases, setCases] = useState("");
  const [error, setError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () =>
      financeApi.createOrders({
        trip_cases: cases
          .split(/[\s,;]+/)
          .map((c) => c.trim())
          .filter(Boolean),
      }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["finance"] });
      const skipped = r.skipped.no_net + r.skipped.no_date;
      toast.success(
        r.lines
          ? `${r.lines} booking(s) on ${r.orders.length} order(s)${skipped ? `; ${skipped} left out (no net or no day)` : ""}`
          : "No pending supplier booking to order",
      );
      onCreated(r.orders.length === 1 ? r.orders[0]!.code : null);
      onClose();
    },
    onError: (e) => setError(errorText(e)),
  });
  return (
    <Modal
      open
      onClose={onClose}
      title="Create purchase orders"
      footer={
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={() => create.mutate()} disabled={!cases.trim() || create.isPending}>
            Create
          </Button>
        </div>
      }
    >
      <Field
        label="Trip cases"
        hint="Codes separated by commas or spaces. Departure shared costs are ordered from the departure drawer."
        error={error ?? undefined}
      >
        <Input value={cases} onChange={(e) => setCases(e.target.value)} placeholder="TC-000123, TC-000124" />
      </Field>
    </Modal>
  );
}

const LINE_COLUMNS: ColumnDef<OrderLine, unknown>[] = [
  {
    id: "line",
    header: "Line",
    cell: ({ row }) => (
      <span className="flex flex-col">
        <span className="font-semibold">{row.original.code}</span>
        <span className="text-xs text-muted">{row.original.trip_case_code ?? `Group departure ${row.original.departure_start_date ?? ""}`}</span>
      </span>
    ),
  },
  { id: "what", header: "Service", cell: ({ row }) => <span className="block max-w-[220px] truncate text-[13px]">{row.original.item_description}</span> },
  { id: "day", header: "Day", cell: ({ row }) => (row.original.service_date ? <DateText value={row.original.service_date} /> : "—") },
  { id: "pax", header: "Pax", cell: ({ row }) => row.original.pax ?? "—" },
  {
    id: "net",
    header: "Net",
    cell: ({ row }) => (row.original.net_amount ? <Money amount={row.original.net_amount} currency={row.original.net_currency ?? "USD"} className="text-[13px]" /> : "—"),
  },
  {
    id: "answer",
    header: "Supplier answer",
    cell: ({ row }) => (
      <span className="text-[13px]">
        {row.original.status}
        {row.original.confirmation_ref ? ` · ${row.original.confirmation_ref}` : ""}
      </span>
    ),
  },
];

function OrderDrawer({ orderRef, onClose }: { orderRef: string | null; onClose: () => void }) {
  const canWrite = useCan("ops.write");
  const qc = useQueryClient();
  const toast = useToast();
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [note, setNote] = useState("");
  const q = useQuery({ queryKey: ["finance", "order", orderRef], queryFn: () => financeApi.order(orderRef!), enabled: !!orderRef });
  const send = useMutation({
    mutationFn: () => financeApi.sendOrder(orderRef!, note.trim() || undefined),
    onSuccess: () => {
      toast.success("Sending: the e-mail goes out within a minute");
      setSending(false);
      qc.invalidateQueries({ queryKey: ["finance"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const o = q.data;
  return (
    <Drawer open={!!orderRef} onClose={onClose} width={760} title={o ? `${o.code} · ${o.supplier.name}` : "Purchase order"}>
      {q.isLoading && <p className="text-[13px] text-muted">Loading…</p>}
      {q.isError && <p className="text-[13px] text-danger">{errorText(q.error, "Could not load the order")}</p>}
      {o && (
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-3 text-[13px] text-body">
            <StatePill state={o.state} />
            <span>Service month {o.service_month.slice(0, 7)}</span>
            <span>
              Total <Money amount={o.total_net ?? o.live_net ?? "0"} currency={o.currency} strong />
            </span>
            {o.sent_to && (
              <span>
                To {o.sent_to}
                {o.redirected ? " (test copy sent to the staff inbox)" : ""}
              </span>
            )}
          </div>
          {o.send_error && (
            <Pill tone="danger" dot={false}>
              Not sent: {o.send_error}
            </Pill>
          )}
          {!o.supplier.email && o.status === "draft" && (
            <Pill tone="warning" dot={false}>
              The supplier has no contact e-mail: add one on the supplier page to send
            </Pill>
          )}
          <div className="flex flex-wrap gap-2">
            <a href={financeApi.documentHref(o.code)} target="_blank" rel="noreferrer" className="text-[13px] font-semibold text-accent-deep hover:underline">
              Printable order
            </a>
            {canWrite && o.actions.send && (
              <Button size="sm" variant="primary" onClick={() => setSending(true)}>
                Send to supplier
              </Button>
            )}
            {canWrite && o.actions.cancel && (
              <Button size="sm" variant="ghost" onClick={() => setCancelling(true)}>
                Cancel order
              </Button>
            )}
          </div>
          <DataTable<OrderLine>
            searchable={false}
            tableId="finance-order-lines"
            data={o.lines}
            columns={LINE_COLUMNS}
            getRowId={(r) => r.id}
            emptyTitle="No line"
            tableMinWidth={680}
          />
        </div>
      )}
      {sending && o && (
        <Modal
          open
          onClose={() => setSending(false)}
          title={`Send ${o.code}`}
          footer={
            <div className="flex justify-end gap-2">
              <Button onClick={() => setSending(false)}>Back</Button>
              <Button variant="primary" onClick={() => send.mutate()} disabled={send.isPending}>
                Send
              </Button>
            </div>
          }
        >
          <Field label="Note to the supplier (optional)" hint="Printed on the order. No traveller or agency names.">
            <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} maxLength={2000} />
          </Field>
        </Modal>
      )}
      {cancelling && o && (
        <ReasonDialog
          title={`Cancel ${o.code}`}
          confirmLabel="Cancel order"
          onClose={() => setCancelling(false)}
          onConfirm={async (reason) => {
            await financeApi.cancelOrder(o.code, reason);
            toast.success("Order cancelled");
            qc.invalidateQueries({ queryKey: ["finance"] });
          }}
        />
      )}
    </Drawer>
  );
}
