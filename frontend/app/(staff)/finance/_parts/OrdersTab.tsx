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
import { useT } from "@/ui/core/useI18n";
import { Phrase } from "@/ui/local/Phrase";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./OrdersTab.messages";

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
  const tx = useTx();
  return (
    <Pill tone={STATE_TONE[state]} dot={false}>
      {tx(ORDER_STATE_LABEL[state])}
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
  const t = useT(MSG);
  const canWrite = useCan("ops.write");
  const { get, setMany } = useUrlFilters();
  const [creating, setCreating] = useState(false);
  const q = { status: get("order_status") || "open", limit: 100 };
  const list = useQuery({ queryKey: ["finance", "orders", q], queryFn: () => financeApi.orders(q) });
  return (
    <Section
      title={t("Purchase orders")}
      description={t("One order per supplier, currency and service month, grouping the pending supplier bookings of trip cases and departures. Sending it asks the supplier for every line at once.")}
      actions={
        canWrite ? (
          <Button variant="primary" onClick={() => setCreating(true)}>
            {t("Create orders")}
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
                label: t("Status"),
                allLabel: t("Open (not cancelled)"),
                options: [
                  { value: "draft", label: t("Draft") },
                  { value: "sent", label: t("Sent") },
                  { value: "cancelled", label: t("Cancelled") },
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
        error={list.isError ? errorText(list.error, t("Could not load the purchase orders")) : null}
        onRetry={() => list.refetch()}
        emptyTitle={t("No purchase order")}
        emptyDescription={t("Create orders from the pending supplier bookings of sold trip cases.")}
        tableMinWidth={760}
      />
      <OrderDrawer orderRef={get("order") || null} onClose={() => setMany({ order: null })} />
      {creating && <CreateOrdersModal onClose={() => setCreating(false)} onCreated={(code) => setMany({ order: code })} />}
    </Section>
  );
}

function CreateOrdersModal({ onClose, onCreated }: { onClose: () => void; onCreated: (code: string | null) => void }) {
  const t = useT(MSG);
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
          ? t("{0} booking(s) on {1} order(s){2}", r.lines, r.orders.length, skipped ? t("; {0} left out (no net or no day)", skipped) : "")
          : t("No pending supplier booking to order"),
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
      title={t("Create purchase orders")}
      footer={
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t("Cancel")}</Button>
          <Button variant="primary" onClick={() => create.mutate()} disabled={!cases.trim() || create.isPending}>
            {t("Create")}
          </Button>
        </div>
      }
    >
      <Field
        label={t("Trip cases")}
        hint={t("Codes separated by commas or spaces. Departure shared costs are ordered from the departure drawer.")}
        error={error ?? undefined}
      >
        <Input value={cases} onChange={(e) => setCases(e.target.value)} placeholder={t("TC-000123, TC-000124")} />
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
        <span className="text-xs text-muted">{row.original.trip_case_code ?? <Phrase text={`Group departure ${row.original.departure_start_date ?? ""}`} />}</span>
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
  const t = useT(MSG);
  const tx = useTx();
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
      toast.success(t("Sending: the e-mail goes out within a minute"));
      setSending(false);
      qc.invalidateQueries({ queryKey: ["finance"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const o = q.data;
  return (
    <Drawer open={!!orderRef} onClose={onClose} width={760} title={o ? `${o.code} · ${o.supplier.name}` : t("Purchase order")}>
      {q.isLoading && <p className="text-[13px] text-muted">{t("Loading…")}</p>}
      {q.isError && <p className="text-[13px] text-danger">{tx(errorText(q.error, t("Could not load the order")))}</p>}
      {o && (
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-3 text-[13px] text-body">
            <StatePill state={o.state} />
            <span>{t("Service month")} {o.service_month.slice(0, 7)}</span>
            <span>
              {t("Total")} <Money amount={o.total_net ?? o.live_net ?? "0"} currency={o.currency} strong />
            </span>
            {o.sent_to && (
              <span>
                {t("To {0}{1}", o.sent_to, o.redirected ? t(" (test copy sent to the staff inbox)") : "")}
              </span>
            )}
          </div>
          {o.send_error && (
            <Pill tone="danger" dot={false}>
              {t("Not sent: {0}", o.send_error)}
            </Pill>
          )}
          {!o.supplier.email && o.status === "draft" && (
            <Pill tone="warning" dot={false}>
              {t("The supplier has no contact e-mail: add one on the supplier page to send")}
            </Pill>
          )}
          <div className="flex flex-wrap gap-2">
            <a href={financeApi.documentHref(o.code)} target="_blank" rel="noreferrer" className="text-[13px] font-semibold text-accent-deep hover:underline">
              {t("Printable order")}
            </a>
            {canWrite && o.actions.send && (
              <Button size="sm" variant="primary" onClick={() => setSending(true)}>
                {t("Send to supplier")}
              </Button>
            )}
            {canWrite && o.actions.cancel && (
              <Button size="sm" variant="ghost" onClick={() => setCancelling(true)}>
                {t("Cancel order")}
              </Button>
            )}
          </div>
          <DataTable<OrderLine>
            searchable={false}
            tableId="finance-order-lines"
            data={o.lines}
            columns={LINE_COLUMNS}
            getRowId={(r) => r.id}
            emptyTitle={t("No line")}
            tableMinWidth={680}
          />
        </div>
      )}
      {sending && o && (
        <Modal
          open
          onClose={() => setSending(false)}
          title={t("Send {0}", o.code)}
          footer={
            <div className="flex justify-end gap-2">
              <Button onClick={() => setSending(false)}>{t("Back")}</Button>
              <Button variant="primary" onClick={() => send.mutate()} disabled={send.isPending}>
                {t("Send")}
              </Button>
            </div>
          }
        >
          <Field label={t("Note to the supplier (optional)")} hint={t("Printed on the order. No traveller or agency names.")}>
            <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} maxLength={2000} />
          </Field>
        </Modal>
      )}
      {cancelling && o && (
        <ReasonDialog
          title={t("Cancel {0}", o.code)}
          confirmLabel={t("Cancel order")}
          onClose={() => setCancelling(false)}
          onConfirm={async (reason) => {
            await financeApi.cancelOrder(o.code, reason);
            toast.success(t("Order cancelled"));
            qc.invalidateQueries({ queryKey: ["finance"] });
          }}
        />
      )}
    </Drawer>
  );
}
