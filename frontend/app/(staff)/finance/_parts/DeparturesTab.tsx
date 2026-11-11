"use client";
// app/(staff)/finance/_parts/DeparturesTab.tsx
// AA-1074 (bk-045, bk-05 / bk-07) — departure shared costs. The list: departures by travel date with sold seats,
// shared cost lines and profit (USD). A row opens the departure drawer (?departure=<id>): its shared costs (rate band
// for the sold seats, or by hand; Order makes the supplier booking, which freezes the price once requested), each
// sold trip case with its share, the bills of the departure, and planned against actual in USD.
// B2C history gate: ops.write here writes departure-level costs and bookings only, never a trip case.
// Realised revenue, profit and margin show only with profit.read (admin, finance); the API sends them as null to the
// other roles (sales, contracting), which see the costs.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import {
  BASIS_LABEL,
  financeApi,
  marginLabel,
  type CostBasis,
  type DepartureCaseRow,
  type DepartureCost,
  type FinanceDeparture,
} from "@/lib/api/finance";
import { supplyApi } from "@/lib/api/supply";
import { suppliersApi } from "@/lib/api/suppliers";
import {
  Button,
  DataTable,
  DateField,
  DateText,
  Drawer,
  Field,
  FilterBar,
  Input,
  KpiTile,
  Modal,
  Money,
  PERIOD_OPTIONS,
  Pill,
  Section,
  Select,
  Textarea,
  periodWindow,
  useCan,
  useToast,
  useUrlFilters,
} from "@/ui";

function errorText(e: unknown, fallback = "Something went wrong"): string {
  return e instanceof ApiError ? e.message : fallback;
}

// Columns of realised revenue / profit: shown only with profit.read.
const PROFIT_COLUMN_IDS = new Set(["profit", "revenue"]);

function withoutProfit<T>(columns: ColumnDef<T, unknown>[], canProfit: boolean): ColumnDef<T, unknown>[] {
  return canProfit ? columns : columns.filter((c) => !PROFIT_COLUMN_IDS.has(c.id ?? ""));
}

const LIST_COLUMNS: ColumnDef<FinanceDeparture, unknown>[] = [
  { id: "start", header: "Departure", cell: ({ row }) => <DateText value={row.original.start_date} className="font-semibold" /> },
  { id: "tour", header: "Tour", cell: ({ row }) => <span className="block max-w-[220px] truncate text-[13px]">{row.original.trip_plan_source_id}</span> },
  { id: "seats", header: "Sold seats", cell: ({ row }) => `${row.original.sold_seats} / ${row.original.capacity}` },
  { id: "costs", header: "Shared costs", cell: ({ row }) => row.original.shared_cost_lines || "—" },
  {
    id: "cost",
    header: "Cost (USD)",
    cell: ({ row }) => (row.original.cost_usd ? <Money amount={row.original.cost_usd} currency="USD" className="text-[13px]" /> : "—"),
  },
  {
    id: "profit",
    header: "Profit (USD)",
    cell: ({ row }) =>
      row.original.profit_usd ? (
        <Money amount={row.original.profit_usd} currency="USD" strong className={`text-[13px] ${Number(row.original.profit_usd) < 0 ? "text-danger" : ""}`} />
      ) : (
        "—"
      ),
  },
  {
    id: "status",
    header: "Status",
    cell: ({ row }) => (
      <Pill tone={row.original.status === "cancelled" ? "danger" : row.original.status === "guaranteed" ? "success" : "neutral"} dot={false}>
        {row.original.status}
      </Pill>
    ),
  },
];

export function DeparturesTab() {
  const { get, setMany } = useUrlFilters();
  const canProfit = useCan("profit.read");
  const span = periodWindow(get("period") || "next_3", new Date());
  const q = { ...span, limit: 100 };
  const list = useQuery({ queryKey: ["finance", "departures", q], queryFn: () => financeApi.departures(q) });
  const open = get("departure");
  return (
    <Section
      title="Departure costs"
      description="Costs shared by the whole group of a departure (coach, guide, group entrance), priced from the supplier rate for the seats sold and split to the trip cases by seats."
    >
      <DataTable<FinanceDeparture>
        filters={<FilterBar selects={[{ key: "period", label: "Travel dates", allLabel: "Next 3 months", options: PERIOD_OPTIONS, width: 170 }]} />}
        searchable={false}
        enableCsv
        csvFilename="departure-costs.csv"
        tableId="finance-departures"
        data={list.data?.items ?? []}
        columns={withoutProfit(LIST_COLUMNS, canProfit)}
        getRowId={(r) => r.id}
        onRowClick={(r) => setMany({ departure: r.id })}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error, "Could not load the departures") : null}
        onRetry={() => list.refetch()}
        emptyTitle="No departure in this period"
        emptyDescription="Departures are set on the trip plans of the catalog."
        tableMinWidth={860}
      />
      <DepartureDrawer departureId={open || null} onClose={() => setMany({ departure: null })} />
    </Section>
  );
}

function CostActions({ cost, departureId }: { cost: DepartureCost; departureId: string }) {
  const canWrite = useCan("ops.write");
  const qc = useQueryClient();
  const toast = useToast();
  const [removing, setRemoving] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: ["finance"] });
  const order = useMutation({
    mutationFn: () => financeApi.orderCost(cost.id),
    onSuccess: (c) => {
      toast.success(`Supplier booking ${c.supplier_booking?.code ?? ""} made`);
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  if (!canWrite) return null;
  return (
    <span className="flex flex-wrap gap-2">
      {cost.actions.order && (
        <Button size="sm" onClick={() => order.mutate()} disabled={order.isPending}>
          Order
        </Button>
      )}
      {cost.actions.remove && (
        <Button size="sm" variant="ghost" onClick={() => setRemoving(true)}>
          Remove
        </Button>
      )}
      {removing && (
        <ReasonDialog
          title="Remove this shared cost"
          confirmLabel="Remove"
          onClose={() => setRemoving(false)}
          onConfirm={async (reason) => {
            await financeApi.removeCost(cost.id, reason);
            toast.success("Cost removed");
            refresh();
            qc.invalidateQueries({ queryKey: ["finance", "departure", departureId] });
          }}
        />
      )}
    </span>
  );
}

export function ReasonDialog({
  title,
  confirmLabel,
  onClose,
  onConfirm,
}: {
  title: string;
  confirmLabel: string;
  onClose: () => void;
  onConfirm: (reason: string) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      footer={
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Back</Button>
          <Button
            variant="danger"
            disabled={!reason.trim() || busy}
            onClick={async () => {
              setBusy(true);
              try {
                await onConfirm(reason.trim());
                onClose();
              } catch (e) {
                setError(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {confirmLabel}
          </Button>
        </div>
      }
    >
      <Field label="Reason" error={error ?? undefined}>
        <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} maxLength={500} />
      </Field>
    </Modal>
  );
}

function costColumns(departureId: string): ColumnDef<DepartureCost, unknown>[] {
  return [
    {
      id: "what",
      header: "Cost",
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <span className="block max-w-[220px] truncate text-[13px] text-ink" title={row.original.item_description}>
            {row.original.item_description}
          </span>
          <span className="text-xs text-muted">
            {row.original.supplier.name} · {BASIS_LABEL[row.original.basis]} x {row.original.quantity}
            {row.original.pricing === "rate" ? ` · rate for ${row.original.priced_for_pax} seats` : " · by hand"}
          </span>
        </span>
      ),
    },
    { id: "day", header: "Day", cell: ({ row }) => <DateText value={row.original.service_date} /> },
    {
      id: "net",
      header: "Net",
      cell: ({ row }) => (
        <span className="flex flex-col text-[13px]">
          <Money amount={row.original.net_amount} currency={row.original.currency} />
          {row.original.currency !== "USD" && row.original.planned_usd && (
            <Money amount={row.original.planned_usd} currency="USD" className="text-xs text-muted" />
          )}
        </span>
      ),
    },
    {
      id: "billed",
      header: "Billed (USD)",
      cell: ({ row }) =>
        row.original.billed && row.original.billed_usd ? (
          <Money amount={row.original.billed_usd} currency="USD" className="text-[13px]" />
        ) : (
          <span className="text-xs text-muted">{row.original.status === "cancelled" ? "Removed" : "Not billed yet"}</span>
        ),
    },
    {
      id: "booking",
      header: "Supplier booking",
      cell: ({ row }) => {
        const b = row.original.supplier_booking;
        if (!b) return <span className="text-xs text-muted">Not ordered</span>;
        return (
          <span className="flex flex-col text-[13px]">
            <span>
              {b.code} · {b.status}
            </span>
            <span className="text-xs text-muted">
              {row.original.frozen_at ? "Price fixed" : "Price follows the seats"}
              {b.order_code ? ` · ${b.order_code}` : ""}
            </span>
          </span>
        );
      },
    },
    { id: "actions", header: "", cell: ({ row }) => (row.original.status === "active" ? <CostActions cost={row.original} departureId={departureId} /> : null) },
  ];
}

const CASE_COLUMNS: ColumnDef<DepartureCaseRow, unknown>[] = [
  {
    id: "case",
    header: "Trip case",
    cell: ({ row }) => (
      <span className="flex flex-col">
        <span className="font-semibold">{row.original.code}</span>
        <span className="block max-w-[180px] truncate text-xs text-muted">{row.original.agency_name ?? "—"}</span>
      </span>
    ),
  },
  { id: "seats", header: "Seats", cell: ({ row }) => row.original.seats },
  {
    id: "revenue",
    header: "Revenue",
    cell: ({ row }) => (row.original.revenue_usd ? <Money amount={row.original.revenue_usd} currency="USD" className="text-[13px]" /> : "—"),
  },
  { id: "own", header: "Own cost", cell: ({ row }) => <Money amount={String(Number(row.original.cost_usd) - Number(row.original.shared_cost_usd))} currency="USD" className="text-[13px]" /> },
  { id: "share", header: "Shared share", cell: ({ row }) => <Money amount={row.original.shared_cost_usd} currency="USD" className="text-[13px]" /> },
  {
    id: "profit",
    header: "Profit",
    cell: ({ row }) => (row.original.profit_usd ? <Money amount={row.original.profit_usd} currency="USD" strong className="text-[13px]" /> : "—"),
  },
];

function DepartureDrawer({ departureId, onClose }: { departureId: string | null; onClose: () => void }) {
  const canWrite = useCan("ops.write");
  const canProfit = useCan("profit.read");
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const q = useQuery({
    queryKey: ["finance", "departure", departureId],
    queryFn: () => financeApi.departureCosts(departureId!),
    enabled: !!departureId,
  });
  const orders = useMutation({
    mutationFn: () => financeApi.createOrders({ departures: [departureId!] }),
    onSuccess: (r) => {
      toast.success(r.lines ? `${r.lines} booking(s) put on ${r.orders.length} purchase order(s)` : "No pending booking to order");
      qc.invalidateQueries({ queryKey: ["finance"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const d = q.data;
  const t = d?.totals_usd;
  return (
    <Drawer open={!!departureId} onClose={onClose} width={880} title={d ? `Departure ${d.departure.start_date}` : "Departure"}>
      {q.isLoading && <p className="text-[13px] text-muted">Loading…</p>}
      {q.isError && <p className="text-[13px] text-danger">{errorText(q.error, "Could not load the departure")}</p>}
      {d && t && (
        <div className="flex flex-col gap-4">
          <p className="text-[13px] text-body">
            {d.departure.trip_plan_source_id} · {d.departure.sold_seats} of {d.departure.capacity} seats sold · {d.departure.status}
          </p>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {canProfit && t.revenue !== null && <KpiTile label="Revenue (USD)" value={<Money amount={t.revenue} currency="USD" />} size="sm" />}
            <KpiTile label="Shared cost (USD)" value={<Money amount={t.shared_cost} currency="USD" />} hint={`Planned ${t.shared_planned} · billed ${t.shared_billed}`} size="sm" />
            {canProfit && t.profit !== null ? (
              <>
                <KpiTile label="Profit (USD)" value={<Money amount={t.profit} currency="USD" />} valueTone={Number(t.profit) < 0 ? "danger" : undefined} size="sm" />
                <KpiTile label="Per seat · margin" value={`${t.profit_per_seat ?? "—"} · ${marginLabel(t.margin)}`} size="sm" />
              </>
            ) : (
              <KpiTile label="Total cost (USD)" value={<Money amount={t.cost} currency="USD" />} hint={`Own costs of the cases ${t.case_cost}`} size="sm" />
            )}
          </div>
          <Section
            title="Shared costs"
            actions={
              canWrite ? (
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" onClick={() => orders.mutate()} disabled={orders.isPending}>
                    Create purchase orders
                  </Button>
                  <Button size="sm" variant="primary" onClick={() => setAdding(true)} disabled={d.departure.status === "cancelled"}>
                    Add shared cost
                  </Button>
                </div>
              ) : null
            }
          >
            <DataTable<DepartureCost>
              searchable={false}
              tableId="finance-departure-costs"
              data={d.shared_costs}
              columns={costColumns(d.departure.id)}
              getRowId={(r) => r.id}
              emptyTitle="No shared cost yet"
              emptyDescription="Add the coach, guide or group entrance this departure shares."
              tableMinWidth={820}
            />
          </Section>
          <Section title="Trip cases on this departure" description="Each case carries its share of the shared costs by seats.">
            <DataTable<DepartureCaseRow>
              searchable={false}
              tableId="finance-departure-cases"
              data={d.cases}
              columns={withoutProfit(CASE_COLUMNS, canProfit)}
              getRowId={(r) => r.trip_case_id}
              emptyTitle="No trip case sold yet"
              tableMinWidth={680}
            />
          </Section>
          {d.bills.length > 0 && (
            <Section title="Bills of this departure">
              <ul className="flex flex-col gap-1 text-[13px]">
                {d.bills.map((b) => (
                  <li key={b.id}>
                    {b.code} · {b.supplier_name} · <Money amount={b.amount} currency={b.currency} /> · {b.status}
                  </li>
                ))}
              </ul>
            </Section>
          )}
          {t.fx_missing && (
            <Pill tone="warning" dot={false}>
              A currency has no approved FX rate: its USD part is left out
            </Pill>
          )}
        </div>
      )}
      {adding && departureId && d && (
        <AddCostModal
          departureId={departureId}
          startDate={d.departure.start_date}
          onClose={() => setAdding(false)}
          onSaved={() => qc.invalidateQueries({ queryKey: ["finance"] })}
        />
      )}
    </Drawer>
  );
}

function AddCostModal({
  departureId,
  startDate,
  onClose,
  onSaved,
}: {
  departureId: string;
  startDate: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [pricing, setPricing] = useState<"rate" | "manual">("rate");
  const [serviceId, setServiceId] = useState("");
  const [supplierId, setSupplierId] = useState("");
  const [basis, setBasis] = useState<CostBasis>("group");
  const [quantity, setQuantity] = useState("1");
  const [unit, setUnit] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [description, setDescription] = useState("");
  const [day, setDay] = useState(startDate);
  const [error, setError] = useState<string | null>(null);
  const services = useQuery({ queryKey: ["supply", "options", "service"], queryFn: () => supplyApi.options({ kind: "service", limit: 200 }) });
  const suppliers = useQuery({ queryKey: ["suppliers", "pick"], queryFn: () => suppliersApi.list({ status: "active", limit: 200 }), enabled: pricing === "manual" });
  const save = useMutation({
    mutationFn: () =>
      financeApi.addCost(departureId, {
        pricing,
        service_id: serviceId || undefined,
        supplier_id: pricing === "manual" ? supplierId || undefined : undefined,
        basis: pricing === "manual" ? basis : undefined,
        unit_amount: pricing === "manual" ? unit : undefined,
        currency: pricing === "manual" ? currency.trim().toUpperCase() : undefined,
        quantity: Number(quantity) || 1,
        item_description: description.trim() || undefined,
        service_date: day,
      }),
    onSuccess: (c) => {
      toast.success(`Added: ${c.net_amount} ${c.currency}`);
      onSaved();
      onClose();
    },
    onError: (e) => setError(errorText(e)),
  });
  return (
    <Modal
      open
      onClose={onClose}
      title="Add shared cost"
      width={520}
      footer={
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending}>
            Add
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label="Priced">
          <Select
            value={pricing}
            onChange={(e) => setPricing(e.target.value as "rate" | "manual")}
            options={[
              { value: "rate", label: "From the supplier rate for the seats sold" },
              { value: "manual", label: "By hand" },
            ]}
          />
        </Field>
        <Field label={pricing === "rate" ? "Service" : "Service (optional)"}>
          <Select
            value={serviceId}
            onChange={(e) => setServiceId(e.target.value)}
            placeholder="Choose a service…"
            options={(services.data?.items ?? []).map((s) => ({ value: s.id, label: `${s.label} · ${s.supplier_name}` }))}
          />
        </Field>
        {pricing === "manual" && (
          <>
            <Field label="Supplier">
              <Select
                value={supplierId}
                onChange={(e) => setSupplierId(e.target.value)}
                placeholder="Choose a supplier…"
                options={(suppliers.data?.items ?? []).map((s) => ({ value: s.id, label: s.company_name }))}
              />
            </Field>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Basis">
                <Select value={basis} onChange={(e) => setBasis(e.target.value as CostBasis)} options={Object.entries(BASIS_LABEL).map(([value, label]) => ({ value, label }))} />
              </Field>
              <Field label="Unit amount">
                <Input value={unit} onChange={(e) => setUnit(e.target.value)} inputMode="decimal" placeholder="0.00" />
              </Field>
              <Field label="Currency">
                <Input value={currency} onChange={(e) => setCurrency(e.target.value)} maxLength={3} />
              </Field>
            </div>
          </>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Quantity">
            <Input value={quantity} onChange={(e) => setQuantity(e.target.value)} inputMode="numeric" />
          </Field>
          <Field label="Service day">
            <DateField value={day} onChange={setDay} aria-label="Service day" />
          </Field>
        </div>
        <Field label="Description" hint="Defaults to the service name">
          <Input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={500} />
        </Field>
        {error && <p className="text-[13px] text-danger">{error}</p>}
      </div>
    </Modal>
  );
}

