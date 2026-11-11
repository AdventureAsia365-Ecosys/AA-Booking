"use client";
// app/(staff)/finance/_parts/ProfitTab.tsx
// AA-1074 (bk-045, bk-08) — profit per trip case, departure and agency month (profit.read). Revenue is the confirmed
// quote total (or the cancellation fee), costs are the best estimate: billed, else planned, else the quote while no
// supplier booking exists. All in USD at the approved plain rates; a case also shows its quote currency.

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import {
  financeApi,
  marginLabel,
  type AgencyProfitMonth,
  type CaseProfit,
  type DepartureProfit,
  type ProfitTotals,
} from "@/lib/api/finance";
import Link from "next/link";
import { DataTable, DateText, FilterBar, KpiTile, Money, PERIOD_OPTIONS, Pill, Section, periodWindow, useUrlFilters } from "@/ui";

function errorText(e: unknown, fallback: string): string | null {
  if (!e) return null;
  return e instanceof ApiError ? e.message : fallback;
}

function Totals({ totals }: { totals: ProfitTotals | undefined }) {
  const loading = totals ? null : undefined;
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <KpiTile label="Revenue (USD)" value={totals ? <Money amount={totals.revenue_usd} currency="USD" /> : loading} />
      <KpiTile label="Cost (USD)" value={totals ? <Money amount={totals.cost_usd} currency="USD" /> : loading} />
      <KpiTile
        label="Profit (USD)"
        value={totals ? <Money amount={totals.profit_usd} currency="USD" /> : loading}
        valueTone={totals && Number(totals.profit_usd) < 0 ? "danger" : undefined}
      />
      <KpiTile
        label="Margin"
        value={totals ? marginLabel(totals.margin) : loading}
        hint={totals?.fx_missing ? "A currency has no approved FX rate: its USD part is left out" : undefined}
        hintTone={totals?.fx_missing ? "danger" : "neutral"}
      />
    </div>
  );
}

function ProfitCell({ value }: { value: string | null }) {
  if (value === null) return <span className="text-muted">—</span>;
  return <Money amount={value} currency="USD" strong className={`text-[13px] ${Number(value) < 0 ? "text-danger" : ""}`} />;
}

const CASE_COLUMNS: ColumnDef<CaseProfit, unknown>[] = [
  {
    id: "case",
    header: "Trip case",
    cell: ({ row }) => (
      <span className="flex flex-col">
        <Link href={`/cases/${row.original.code}`} className="font-semibold whitespace-nowrap">
          {row.original.code}
        </Link>
        <span className="block max-w-[200px] truncate text-xs text-muted">{row.original.agency_name ?? "—"}</span>
      </span>
    ),
  },
  { id: "start", header: "Starts", cell: ({ row }) => (row.original.start_date ? <DateText value={row.original.start_date} /> : "—") },
  {
    id: "revenue",
    header: "Revenue",
    cell: ({ row }) => (
      <span className="flex flex-col text-[13px]">
        <Money amount={row.original.revenue_usd} currency="USD" />
        {row.original.currency !== "USD" && <Money amount={row.original.revenue} currency={row.original.currency} className="text-xs text-muted" />}
        {row.original.cancelled && (
          <Pill tone="warning" dot={false}>
            Cancellation fee
          </Pill>
        )}
      </span>
    ),
  },
  { id: "planned", header: "Planned cost", cell: ({ row }) => <Money amount={row.original.planned_cost_usd} currency="USD" className="text-[13px]" /> },
  { id: "actual", header: "Billed cost", cell: ({ row }) => <Money amount={row.original.actual_cost_usd} currency="USD" className="text-[13px]" /> },
  {
    id: "cost",
    header: "Best cost",
    cell: ({ row }) => (
      <span className="flex flex-col text-[13px]">
        <Money amount={row.original.cost_usd} currency="USD" />
        <span className="text-xs text-muted">
          {row.original.cost_basis === "quote" ? "From the quote" : row.original.unbilled_bookings ? `${row.original.unbilled_bookings} not billed yet` : "Bookings"}
        </span>
      </span>
    ),
  },
  { id: "profit", header: "Profit", cell: ({ row }) => <ProfitCell value={row.original.profit_usd} /> },
  { id: "margin", header: "Margin", cell: ({ row }) => <span className="text-[13px]">{marginLabel(row.original.margin)}</span> },
];

const DEPARTURE_COLUMNS: ColumnDef<DepartureProfit, unknown>[] = [
  { id: "start", header: "Departure", cell: ({ row }) => <DateText value={row.original.start_date} className="font-semibold" /> },
  { id: "tour", header: "Tour", cell: ({ row }) => <span className="block max-w-[200px] truncate text-[13px]">{row.original.trip_plan_source_id}</span> },
  { id: "seats", header: "Sold seats", cell: ({ row }) => `${row.original.sold_seats} / ${row.original.capacity}` },
  { id: "revenue", header: "Revenue", cell: ({ row }) => <Money amount={row.original.revenue_usd} currency="USD" className="text-[13px]" /> },
  { id: "shared", header: "Shared cost", cell: ({ row }) => <Money amount={row.original.shared_cost_usd} currency="USD" className="text-[13px]" /> },
  { id: "cost", header: "Total cost", cell: ({ row }) => <Money amount={row.original.cost_usd} currency="USD" className="text-[13px]" /> },
  { id: "profit", header: "Profit", cell: ({ row }) => <ProfitCell value={row.original.profit_usd} /> },
  { id: "seat", header: "Per seat", cell: ({ row }) => <ProfitCell value={row.original.profit_per_seat_usd} /> },
  { id: "margin", header: "Margin", cell: ({ row }) => marginLabel(row.original.margin) },
];

const AGENCY_COLUMNS: ColumnDef<AgencyProfitMonth, unknown>[] = [
  { id: "agency", header: "Agency", cell: ({ row }) => <span className="font-semibold">{row.original.agency_name ?? "—"}</span> },
  { id: "month", header: "Travel month", cell: ({ row }) => row.original.travel_month.slice(0, 7) },
  {
    id: "cases",
    header: "Trip cases",
    cell: ({ row }) => `${row.original.cases}${row.original.cancelled_cases ? ` (${row.original.cancelled_cases} cancelled)` : ""}`,
  },
  { id: "revenue", header: "Revenue", cell: ({ row }) => <Money amount={row.original.revenue_usd} currency="USD" className="text-[13px]" /> },
  { id: "cost", header: "Cost", cell: ({ row }) => <Money amount={row.original.cost_usd} currency="USD" className="text-[13px]" /> },
  { id: "profit", header: "Profit", cell: ({ row }) => <ProfitCell value={row.original.profit_usd} /> },
  { id: "margin", header: "Margin", cell: ({ row }) => marginLabel(row.original.margin) },
];

export function ProfitTab() {
  const { get } = useUrlFilters();
  const view = get("view") || "cases";
  const span = periodWindow(get("period") || "next_3", new Date());
  const q = { ...span, limit: 200 };
  const cases = useQuery({ queryKey: ["finance", "profit", "cases", q], queryFn: () => financeApi.profitCases(q), enabled: view === "cases" });
  const deps = useQuery({ queryKey: ["finance", "profit", "deps", q], queryFn: () => financeApi.profitDepartures(q), enabled: view === "departures" });
  const agencies = useQuery({ queryKey: ["finance", "profit", "agencies", span], queryFn: () => financeApi.profitAgencies(span), enabled: view === "agencies" });
  const totals = view === "cases" ? cases.data?.totals : view === "departures" ? deps.data?.totals : agencies.data?.totals;
  const filters = (
    <FilterBar
      selects={[
        {
          key: "view",
          label: "Per",
          allLabel: "Trip case",
          options: [
            { value: "departures", label: "Departure" },
            { value: "agencies", label: "Agency and month" },
          ],
          width: 170,
        },
        { key: "period", label: "Travel dates", allLabel: "Next 3 months", options: PERIOD_OPTIONS, width: 170 },
      ]}
    />
  );
  return (
    <Section
      title="Profit"
      description="Revenue of the confirmed quote (or the cancellation fee) less the supplier cost: billed where a bill is approved, else planned, else the quote while nothing is booked yet. In USD at the approved FX rates."
    >
      <div className="flex flex-col gap-4">
        <Totals totals={totals} />
        {view === "cases" && (
          <DataTable<CaseProfit>
            filters={filters}
            searchable={false}
            enableCsv
            csvFilename="profit-per-trip-case.csv"
            tableId="finance-profit-cases"
            data={cases.data?.items ?? []}
            columns={CASE_COLUMNS}
            getRowId={(r) => r.trip_case_id}
            loading={cases.isLoading}
            error={errorText(cases.error, "Could not load the profit")}
            onRetry={() => cases.refetch()}
            emptyTitle="No sold trip case in this period"
            emptyDescription="A trip case shows here once its agency confirms a hold."
            tableMinWidth={980}
          />
        )}
        {view === "departures" && (
          <DataTable<DepartureProfit>
            filters={filters}
            searchable={false}
            enableCsv
            csvFilename="profit-per-departure.csv"
            tableId="finance-profit-departures"
            data={deps.data?.items ?? []}
            columns={DEPARTURE_COLUMNS}
            getRowId={(r) => r.departure_id}
            loading={deps.isLoading}
            error={errorText(deps.error, "Could not load the profit")}
            onRetry={() => deps.refetch()}
            emptyTitle="No departure with a sale or a shared cost in this period"
            tableMinWidth={980}
          />
        )}
        {view === "agencies" && (
          <DataTable<AgencyProfitMonth>
            filters={filters}
            searchable={false}
            enableCsv
            csvFilename="profit-per-agency-month.csv"
            tableId="finance-profit-agencies"
            data={agencies.data?.items ?? []}
            columns={AGENCY_COLUMNS}
            getRowId={(r) => `${r.partner_id}-${r.travel_month}`}
            loading={agencies.isLoading}
            error={errorText(agencies.error, "Could not load the profit")}
            onRetry={() => agencies.refetch()}
            emptyTitle="No agency sale in this period"
            tableMinWidth={820}
          />
        )}
      </div>
    </Section>
  );
}
