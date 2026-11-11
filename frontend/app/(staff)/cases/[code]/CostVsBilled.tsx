"use client";
// app/(staff)/cases/[code]/CostVsBilled.tsx
// AA-682 S4 (bk-030, S252 design §2 / §6) — the case's cost vs billed panel on its Suppliers tab: per supplier booking
// the expected net (from the approved version's costs) next to what suppliers billed (lines of bills not cancelled)
// and the variance, totals per currency. Net amounts are commercial: shown only with pricing.read (and bill.read).
// No FX conversion: a bill in another currency than the net is flagged, not converted. "Supplier bills" opens the bill
// list filtered to this case (?case=); "Enter bill" (bill.write) opens the bill form with this case filled in.

import { useState } from "react";
import Link from "next/link";
import { Plus } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BillForm } from "@/app/(staff)/billing/_parts/BillDrawer";
import { ApiError } from "@/lib/api/client";
import type { ColumnDef } from "@tanstack/react-table";
import { supplierBillsApi, type CaseCostLine } from "@/lib/api/supplierBills";
import type { TripCaseDetail } from "@/lib/api/types";
import { Button, DataTable, Money, Pill, Section, formatMoney, useCan } from "@/ui";

function tone(variance: string | null): string {
  if (variance === null) return "text-muted";
  const n = Number(variance);
  if (n > 0) return "font-semibold text-danger";
  if (n < 0) return "text-success";
  return "text-body";
}

const COLUMNS: ColumnDef<CaseCostLine, unknown>[] = [
  {
    id: "booking",
    header: "Booking",
    cell: ({ row }) => (
      <span className="flex min-w-0 flex-col">
        <span className="block max-w-[240px] truncate text-[13px] text-ink" title={row.original.item_description}>
          {row.original.item_description}
        </span>
        <span className="text-xs text-muted">{row.original.supplier_booking.code}</span>
      </span>
    ),
  },
  {
    id: "supplier",
    header: "Supplier",
    cell: ({ row }) => <span className="block max-w-[180px] truncate text-[13px]">{row.original.supplier.name}</span>,
  },
  {
    id: "net",
    header: "Expected net",
    cell: ({ row }) => <Money amount={row.original.net_amount} currency={row.original.net_currency} className="text-[13px]" />,
  },
  {
    id: "billed",
    header: "Billed",
    cell: ({ row }) => {
      const l = row.original;
      if (l.billed.length === 0)
        return l.unbilled ? (
          <Pill tone="neutral" dot={false}>
            Not billed yet
          </Pill>
        ) : (
          <span className="text-muted">—</span>
        );
      return (
        <span className="flex flex-col text-[13px]">
          {l.billed.map((b) => (
            <span key={b.currency} title={b.bills.join(", ")}>
              {formatMoney(b.amount, b.currency)}
            </span>
          ))}
        </span>
      );
    },
  },
  {
    id: "variance",
    header: "Variance",
    cell: ({ row }) => {
      const l = row.original;
      if (l.currency_mismatch)
        return (
          <Pill tone="warning" dot={false}>
            Other currency
          </Pill>
        );
      return <span className={`text-[13px] ${tone(l.variance)}`}>{l.variance === null ? "—" : formatMoney(l.variance, l.net_currency)}</span>;
    },
  },
  {
    // AA-1074 (bk-045): the booking in USD at its snapshot rates; the realised FX difference once billed.
    id: "usd",
    header: "USD",
    cell: ({ row }) => {
      const u = row.original.usd;
      if (!u) return <span className="text-muted">—</span>;
      return (
        <span className="flex flex-col text-[13px]">
          <span>{u.cost === null ? "—" : formatMoney(u.cost, "USD")}</span>
          {u.fx_difference !== null && u.fx_difference !== "0.00" && <span className="text-xs text-muted">FX {formatMoney(u.fx_difference, "USD")}</span>}
        </span>
      );
    },
  },
];

export function CostVsBilled({ c }: { c: TripCaseDetail }) {
  const canBills = useCan("bill.read");
  const canPricing = useCan("pricing.read");
  const canWrite = useCan("bill.write");
  const canSee = canBills && canPricing;
  const qc = useQueryClient();
  const [entering, setEntering] = useState(false);
  const q = useQuery({ queryKey: ["supplier-costs", c.code], queryFn: () => supplierBillsApi.caseCosts(c.code), enabled: canSee });
  if (!canSee) return null;
  const data = q.data;
  const billsHref = `/billing?tab=supplier-bills&case=${encodeURIComponent(c.code)}`;
  return (
    <Section
      title="Cost vs billed"
      description="Expected supplier net of each booking next to what the suppliers billed (bills not cancelled). A positive variance means a supplier billed more than costed."
      actions={
        <div className="flex flex-wrap items-center gap-3">
          <Link href={billsHref} className="text-[13px] font-semibold text-accent-deep hover:underline">
            Bills of this case
          </Link>
          {canWrite && (
            <Button size="sm" onClick={() => setEntering(true)}>
              <Plus size={13} /> Enter bill
            </Button>
          )}
        </div>
      }
    >
      {q.isLoading && <p className="text-[13px] text-muted">Loading…</p>}
      {q.isError && <p className="text-[13px] text-danger">{q.error instanceof ApiError ? q.error.message : "Could not load the costs"}</p>}
      {data && data.lines.length === 0 && <p className="text-[13px] text-muted">No supplier booking yet.</p>}
      {data && data.lines.length > 0 && (
        <div className="flex flex-col gap-3">
          <DataTable<CaseCostLine>
            searchable={false}
            tableId="case-cost-vs-billed"
            data={data.lines}
            columns={COLUMNS}
            getRowId={(l) => l.supplier_booking.id}
            tableMinWidth={680}
          />
          {data.usd && (
            <div className="flex flex-wrap gap-4 text-[13px]" data-testid="case-costs-usd">
              <span>
                <span className="text-muted">USD:</span> planned {formatMoney(data.usd.planned, "USD")} · billed{" "}
                {formatMoney(data.usd.billed, "USD")} ·{" "}
                <span className={tone(data.usd.variance === "0" || data.usd.variance === "0.00" ? null : data.usd.variance)}>
                  variance {formatMoney(data.usd.variance, "USD")}
                </span>{" "}
                · FX difference {formatMoney(data.usd.fx_difference, "USD")}
              </span>
              <span>
                <span className="text-muted">Best cost:</span> <strong>{formatMoney(data.usd.cost, "USD")}</strong>
                {Number(data.usd.shared) > 0 && ` (departure share ${formatMoney(data.usd.shared, "USD")})`}
              </span>
              {data.usd.fx_missing && (
                <Pill tone="warning" dot={false}>
                  A currency has no approved FX rate
                </Pill>
              )}
            </div>
          )}
          {data.shared_costs && data.shared_costs.length > 0 && (
            <ul className="flex flex-col gap-1 text-[13px] text-body">
              {data.shared_costs.map((s) => (
                <li key={s.departure_cost_id}>
                  Shared: {s.item_description} · {s.seats} of {s.departure_seats} seats · {formatMoney(s.share_net, s.currency)}
                </li>
              ))}
            </ul>
          )}
          <div className="flex flex-wrap gap-4 text-[13px]">
            {data.totals.map((t) => (
              <span key={t.currency}>
                <span className="text-muted">{t.currency}:</span> net {formatMoney(t.net, t.currency)} · billed {formatMoney(t.billed, t.currency)} ·{" "}
                <span className={tone(t.variance)}>variance {t.variance === null ? "—" : formatMoney(t.variance, t.currency)}</span>
              </span>
            ))}
          </div>
        </div>
      )}
      {entering && (
        <BillForm
          open
          supplier={null}
          tripCase={c.code}
          onClose={() => setEntering(false)}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["supplier-costs", c.code] });
            qc.invalidateQueries({ queryKey: ["supplier-bills"] });
          }}
        />
      )}
    </Section>
  );
}
