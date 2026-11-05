"use client";
// app/(staff)/pricing/readiness/_parts/ReadinessView.tsx — Pricing → Price readiness (AA-1029).
// GET /booking/catalog/price-readiness (pricing.read): one status per trip plan, from the cost sheet's own reasons, so
// when real supplier data arrives staff see which tours price and what each of the others still needs (components,
// published rates in the next 365 days, an exchange rate, an agency pricing rule). Summary tiles count every plan; the
// status / country / search filters narrow the table (URL query). Each row opens the trip plan (its commercial drawer
// for staff who edit plans). "Download CSV" saves the same list as the staff CSV (fixed columns, never a net).

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download } from "lucide-react";
import {
  priceReadinessApi,
  READINESS_STATUSES,
  type ReadinessItem,
  type UnratedComponent,
} from "@/lib/api/priceReadiness";
import {
  Button,
  DataTable,
  FilterBar,
  KpiTile,
  Money,
  PageFrame,
  PRICE_READINESS_META,
  PriceReadinessBadge,
  Section,
  useCan,
  useToast,
  useUrlFilters,
  type ColumnDef,
} from "@/ui";

const MAX_LISTED = 3;

function componentText(c: UnratedComponent): string {
  const name = c.room_type ? `${c.code ?? "?"} ${c.room_type}` : (c.code ?? "?");
  return `Day ${c.day_no}: ${name}${c.supplier_code ? ` (${c.supplier_code})` : ""}`;
}

/** What the plan still needs, in plain words (the reason, then the items behind it). */
function Needs({ row }: { row: ReadinessItem }) {
  const lines: string[] = [];
  const reason = row.reason;
  if (row.status === "priced" && row.pending_recompute) lines.push("Prices on the next recompute.");
  if (reason === "no_components") lines.push("No hotels or services mapped to this tour.");
  if (reason === "inactive_component") lines.push("A hotel, room type or service of the tour is inactive.");
  if (reason === "no_rule") lines.push("No approved agency pricing rule is in force today.");
  if (reason === "missing_fx") lines.push(`No approved exchange rate today for ${row.missing_fx.join(", ")}.`);
  if (reason === "missing_rates" && row.components_without_rate.length) {
    const n = row.components_without_rate.length;
    lines.push(`${n} component${n > 1 ? "s" : ""} without a published rate:`);
  }
  if (row.note && !row.pending_recompute) lines.push(row.note.charAt(0).toUpperCase() + row.note.slice(1) + ".");
  const unrated = reason === "missing_rates" ? row.components_without_rate : [];
  if (!lines.length && !unrated.length) return <span className="text-muted">—</span>;
  return (
    <span className="flex min-w-0 flex-col gap-0.5 text-[13px]">
      {lines.map((l) => (
        <span key={l} className="text-body">
          {l}
        </span>
      ))}
      {unrated.slice(0, MAX_LISTED).map((c) => (
        <span key={`${c.day_no}-${c.code}-${c.room_type}`} className="break-words text-xs text-muted">
          {componentText(c)}
        </span>
      ))}
      {unrated.length > MAX_LISTED && <span className="text-xs text-muted">and {unrated.length - MAX_LISTED} more</span>}
    </span>
  );
}

function Tour({ row }: { row: ReadinessItem }) {
  const facts = [row.country, row.duration_days ? `${row.duration_days} days` : null, row.sellable ? "Sellable" : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <span className="flex min-w-0 flex-col">
      <span className="font-medium text-ink">{row.title ?? row.tour_key}</span>
      {facts && <span className="text-xs text-muted">{facts}</span>}
      {row.in_catalog === false && <span className="text-xs text-muted">No longer in the catalog</span>}
    </span>
  );
}

export function ReadinessView() {
  const router = useRouter();
  const toast = useToast();
  const { get } = useUrlFilters();
  const canEditPlans = useCan("trip_plan.write");
  const filters = { status: get("status"), country: get("country"), q: get("q") };
  const [downloading, setDownloading] = useState(false);

  const report = useQuery({
    queryKey: ["price-readiness", filters.status, filters.country, filters.q],
    queryFn: () => priceReadinessApi.get(filters),
    placeholderData: (previous) => previous,
  });
  const data = report.data;
  const counts = data?.summary.by_status;
  const items = useMemo(() => data?.items ?? [], [data]);

  function planHref(row: ReadinessItem): string {
    const key = encodeURIComponent(row.tour_key);
    return canEditPlans ? `/catalog/trip-plans/${key}/commercial` : `/catalog/trip-plans/${key}`;
  }

  const columns: ColumnDef<ReadinessItem, unknown>[] = [
    {
      id: "tour",
      header: "Tour",
      accessorFn: (r) => r.title ?? r.tour_key,
      cell: ({ row }) => <Tour row={row.original} />,
    },
    {
      id: "status",
      header: "Status",
      accessorKey: "status",
      cell: ({ row }) => <PriceReadinessBadge status={row.original.status} />,
    },
    { id: "needs", header: "What it needs", enableSorting: false, accessorFn: (r) => r.reason ?? "", cell: ({ row }) => <Needs row={row.original} /> },
    { id: "components", header: "Components", accessorKey: "components" },
    {
      id: "rows",
      header: "Price rows",
      accessorFn: (r) => r.computed_rows + r.hand_rows,
      cell: ({ row }) => (
        <span className="flex flex-col whitespace-nowrap text-[13px]">
          <span>{row.original.computed_rows} from rates</span>
          <span className="text-xs text-muted">{row.original.hand_rows} by hand</span>
        </span>
      ),
    },
    {
      id: "from",
      header: "From (RRP, 2 pax)",
      accessorFn: (r) => Number(r.from_price?.amount ?? -1),
      cell: ({ row }) =>
        row.original.from_price ? <Money amount={row.original.from_price.amount} currency={row.original.from_price.currency} strong /> : "—",
    },
    {
      id: "open",
      header: "Trip plan",
      enableSorting: false,
      cell: ({ row }) => (
        <Link
          href={planHref(row.original)}
          onClick={(e) => e.stopPropagation()}
          className="whitespace-nowrap text-[13px] font-semibold text-accent-deep hover:underline"
        >
          {canEditPlans ? "Open prices" : "Open"}
        </Link>
      ),
    },
  ];

  async function download() {
    setDownloading(true);
    try {
      const { blob, filename } = await priceReadinessApi.csv(filters);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "The report could not be downloaded.");
    } finally {
      setDownloading(false);
    }
  }

  const priced = counts ? counts.priced + counts.hand_priced : undefined;
  const filtered = !!(filters.status || filters.country || filters.q);
  return (
    <PageFrame
      title="Price readiness"
      description="Which trip plans have a price, and what each of the others still needs. A plan prices from supplier rates once every hotel and service of the tour has a published rate, every rate currency has an approved exchange rate and an agency pricing rule is in force."
      actions={
        <Button variant="secondary" size="sm" onClick={download} disabled={downloading || !items.length}>
          <Download size={13} /> Download CSV
        </Button>
      }
    >
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiTile label="Trip plans" value={data?.summary.total} hint={data ? `Rates read to ${data.horizon_to}` : undefined} />
        <KpiTile
          label="Priced"
          value={priced}
          hintTone="success"
          hint={counts ? `${counts.priced} from rates, ${counts.hand_priced} by hand` : undefined}
        />
        <KpiTile
          label="Missing rates or FX"
          value={counts ? counts.missing_rates + counts.missing_fx : undefined}
          hintTone={counts && counts.missing_rates + counts.missing_fx > 0 ? "warning" : "neutral"}
          hint={counts ? `${counts.missing_fx} wait for an exchange rate` : undefined}
        />
        <KpiTile
          label="No components"
          value={counts?.no_components}
          hint={counts ? `${counts.inactive_component} with an inactive component` : undefined}
        />
      </div>
      {data && data.rules.length === 0 && (
        <div className="rounded-lg border border-danger-border bg-danger-soft px-4 py-3 text-[13px] text-body" role="status">
          No approved agency pricing rule is in force today, so no plan prices from rates.{" "}
          <Link href="/pricing/rules" className="font-semibold text-accent-deep hover:underline">
            Open Pricing rules &amp; FX
          </Link>
        </div>
      )}
      {!!data?.degraded.length && (
        <div className="rounded-lg border border-accent-border bg-accent-tint px-4 py-3 text-[13px] text-body" role="status">
          The tour catalog could not be read: plans are listed by their key, without title or country.
        </div>
      )}
      <Section>
        <DataTable<ReadinessItem>
          filters={
            <FilterBar
              search={{ placeholder: "Search a tour title or key" }}
              selects={[
                {
                  key: "status",
                  label: "Status",
                  allLabel: "All statuses",
                  options: READINESS_STATUSES.map((s) => ({
                    value: s,
                    label: counts ? `${PRICE_READINESS_META[s].label} (${counts[s]})` : PRICE_READINESS_META[s].label,
                  })),
                  width: 210,
                },
                {
                  key: "country",
                  label: "Country",
                  allLabel: "All countries",
                  options: (data?.summary.by_country ?? []).map((c) => ({ value: c.country, label: `${c.country} (${c.total})` })),
                  width: 170, // the countries load with the report: a fixed width keeps the bar from shifting
                },
              ]}
            />
          }
          searchable={false}
          tableId="price-readiness"
          data={items}
          columns={columns}
          getRowId={(r) => r.tour_key}
          loading={report.isLoading}
          error={report.isError ? (report.error as Error).message : null}
          onRetry={() => report.refetch()}
          onRowClick={(r) => router.push(planHref(r))}
          emptyTitle={filtered ? "No trip plans match" : "No trip plans yet"}
          emptyDescription={
            filtered
              ? "Change the search or the filters, or reset them to see every plan."
              : "Trip plans appear here once the catalog sync has created them."
          }
          tableMinWidth={980}
        />
      </Section>
    </PageFrame>
  );
}
