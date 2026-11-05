"use client";
// app/(staff)/pricing/agency-tiers/_parts/TariffView.tsx — Pricing → Agency tiers → Tariff (AA-1030 G3).
// One agency's own price per tour, season and pax band (GET /booking/pricing/partner-tariffs/{partner_id}): every b2b
// window x pax band of the price matrix, priced by the B2B quote engine for the band's smallest party with the
// agency's tier or contract discount, so a quote answers the same amount. "Download CSV" saves the file the agency
// downloads itself from booking.b2b.v1 GET /tariff; "Print" prints this page (the print stylesheet in globals.css
// hides the shell and the toolbar). Rows the quote engine cannot price are listed apart with the reason (staff only).

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Download, Printer } from "lucide-react";
import { agencyPricingApi, TARIFF_CURRENCIES, type TariffItem, type UnpricedTariffRow } from "@/lib/api/agencyPricing";
import { pct } from "@/lib/proposalDraft";
import { Button, DataTable, DateText, EmptyState, Money, PageFrame, Section, Select, useToast, useUrlFilters, type ColumnDef } from "@/ui";

function band(min: number, max: number | null): string {
  if (max === null) return `${min}+`;
  return min === max ? `${min}` : `${min}–${max}`;
}

function rowId(r: { tour: { source_id: string }; valid_from: string; pax_min: number }): string {
  return `${r.tour.source_id}|${r.valid_from}|${r.pax_min}`;
}

function Tour({ row }: { row: { title: string | null; tour: { source_id: string }; country?: string | null; duration_days?: number | null } }) {
  const facts = [row.country, row.duration_days ? `${row.duration_days} days` : null].filter(Boolean).join(" · ");
  return (
    <span className="flex flex-col">
      <span className="font-medium text-ink">{row.title ?? row.tour.source_id}</span>
      {facts && <span className="text-xs text-muted">{facts}</span>}
    </span>
  );
}

const columns: ColumnDef<TariffItem, unknown>[] = [
  { id: "tour", header: "Tour", accessorFn: (r) => r.title ?? r.tour.source_id, cell: ({ row }) => <Tour row={row.original} /> },
  {
    id: "dates",
    header: "Travel dates",
    accessorKey: "valid_from",
    cell: ({ row }) => (
      <span className="flex flex-col">
        <span className="whitespace-nowrap">
          <DateText value={row.original.valid_from} /> – <DateText value={row.original.valid_to} />
        </span>
        {row.original.season && <span className="text-xs text-muted">{row.original.season}</span>}
      </span>
    ),
  },
  { id: "pax", header: "Travellers", accessorFn: (r) => r.pax_min, cell: ({ row }) => band(row.original.pax_min, row.original.pax_max) },
  {
    id: "per_person",
    header: "Per person",
    accessorFn: (r) => Number(r.price_per_person.amount),
    cell: ({ row }) => (
      <span className="flex flex-col">
        <Money amount={row.original.price_per_person.amount} currency={row.original.price_per_person.currency} strong />
        <span className="whitespace-nowrap text-xs text-muted">
          {row.original.priced_for_pax} traveller{row.original.priced_for_pax > 1 ? "s" : ""}: <Money amount={row.original.price.amount} currency={row.original.price.currency} />
        </span>
      </span>
    ),
  },
  {
    id: "discount",
    header: "Discount",
    accessorFn: (r) => Number(r.agency_discount_pct),
    cell: ({ row }) => (
      <span className="flex flex-col">
        <span>{pct(row.original.agency_discount_pct)}</span>
        <span className="text-xs text-muted">{row.original.rate_card_id ? "Contract" : "Tier"}</span>
      </span>
    ),
  },
  {
    id: "rrp",
    header: "Retail (RRP)",
    accessorFn: (r) => Number(r.rrp_per_person?.amount ?? -1),
    cell: ({ row }) =>
      row.original.rrp_per_person ? <Money amount={row.original.rrp_per_person.amount} currency={row.original.rrp_per_person.currency} /> : "—",
  },
];

const unpricedColumns: ColumnDef<UnpricedTariffRow, unknown>[] = [
  { id: "tour", header: "Tour", accessorFn: (r) => r.title ?? r.tour.source_id, cell: ({ row }) => <Tour row={row.original} /> },
  {
    id: "dates",
    header: "Travel dates",
    accessorKey: "valid_from",
    cell: ({ row }) => (
      <span className="whitespace-nowrap">
        <DateText value={row.original.valid_from} /> – <DateText value={row.original.valid_to} />
      </span>
    ),
  },
  { id: "pax", header: "Travellers", accessorFn: (r) => r.pax_min, cell: ({ row }) => band(row.original.pax_min, row.original.pax_max) },
  { id: "reason", header: "Why it has no price", accessorKey: "reason", cell: ({ row }) => <span className="text-[13px] text-muted">{row.original.reason}</span> },
];

export function TariffView() {
  const { get, set } = useUrlFilters();
  const toast = useToast();
  const partnerId = get("partner");
  const currency = (TARIFF_CURRENCIES as readonly string[]).includes(get("currency")) ? get("currency") : "USD";
  const [downloading, setDownloading] = useState(false);

  const tariff = useQuery({
    queryKey: ["agency-tariff", partnerId, currency],
    queryFn: () => agencyPricingApi.tariff(partnerId, currency),
    enabled: Boolean(partnerId),
  });
  const data = tariff.data;
  const items = useMemo(() => data?.items ?? [], [data]);

  async function download() {
    setDownloading(true);
    try {
      const { blob, filename } = await agencyPricingApi.tariffCsv(partnerId, currency);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "The tariff could not be downloaded.");
    } finally {
      setDownloading(false);
    }
  }

  if (!partnerId) {
    return (
      <PageFrame title="Agency tariff">
        <EmptyState
          title="Pick an agency"
          description="Open a tariff from the Agencies tab of Agency tiers."
          action={
            <Link href="/pricing/agency-tiers?tab=agencies" className="text-[13px] font-semibold text-accent-deep hover:underline">
              Go to Agencies
            </Link>
          }
        />
      </PageFrame>
    );
  }

  const partner = data?.partner;
  return (
    <PageFrame
      title={partner ? `Tariff: ${partner.agency_name}` : "Agency tariff"}
      description={
        partner
          ? `${partner.tier_label} tier. Prices as a B2B quote on ${data?.priced_on ?? "today"} would charge them, per person for the smallest group of each band sharing twin rooms. Prices follow supplier rates, rules and exchange rates; the quote is binding.`
          : "The agency's own price per tour, season and number of travellers."
      }
      actions={
        <div className="bk-no-print flex flex-wrap items-center gap-2">
          <Select size="sm" aria-label="Currency" value={currency} onChange={(e) => set("currency", e.target.value)}>
            {TARIFF_CURRENCIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
          <Button variant="secondary" size="sm" onClick={download} disabled={downloading || !items.length}>
            <Download size={13} /> Download CSV
          </Button>
          <Button variant="primary" size="sm" onClick={() => window.print()} disabled={!items.length}>
            <Printer size={13} /> Print
          </Button>
        </div>
      }
    >
      {data?.truncated && (
        <div className="rounded-lg border border-accent-border bg-accent-tint px-4 py-3 text-[13px] text-body">
          The tariff is long: only its first rows are shown. Download the CSV per tour or narrow the catalogue.
        </div>
      )}
      <Section title="Prices" description={data ? `In ${data.currency}${data.currency !== "USD" ? ` at ${data.fx.rate_per_usd} per USD (${data.fx.rate_date})` : ""}.` : undefined}>
        <DataTable<TariffItem>
          tableId="agency-tariff"
          data={items}
          columns={columns}
          getRowId={rowId}
          loading={tariff.isLoading}
          error={tariff.isError ? (tariff.error as Error).message : null}
          onRetry={() => tariff.refetch()}
          paginate={false}
          emptyTitle="No prices yet"
          emptyDescription="Sellable tours appear here once their price matrix has rows for agencies (Recompute from rates on the trip plan)."
        />
      </Section>
      {!!data?.unpriced.length && (
        <div className="bk-no-print">
          <Section title="Not in the tariff" description="Matrix rows a B2B quote cannot price today. The agency does not see them.">
            <DataTable<UnpricedTariffRow>
              tableId="agency-tariff-unpriced"
              data={data.unpriced}
              columns={unpricedColumns}
              getRowId={rowId}
              paginate={false}
            />
          </Section>
        </div>
      )}
    </PageFrame>
  );
}
