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
import { daysText } from "@/ui/booking/plural";
import { outOfCatalog, tourName, type OutOfCatalog } from "@/ui/booking/priceWorkbench";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./ReadinessView.messages";

const MAX_LISTED = 3;

// AA-1128: a paused tour is temporary in CIS, never "no longer in the catalog".
const OUT_OF_CATALOG: Record<OutOfCatalog, string> = {
  paused: "Paused in CIS: back on sale once CIS serves it again",
  retired: "Retired in CIS",
  gone: "No longer in the catalog",
};

/** The component's code and room type, with its supplier (the "Day N:" before it is the UI-language message). */
function componentName(c: UnratedComponent): string {
  const name = c.room_type ? `${c.code ?? "?"} ${c.room_type}` : (c.code ?? "?");
  return `${name}${c.supplier_code ? ` (${c.supplier_code})` : ""}`;
}

/** What the plan still needs, in plain words (the reason, then the items behind it). */
function Needs({ row }: { row: ReadinessItem }) {
  const t = useT(MSG);
  const lines: string[] = [];
  const reason = row.reason;
  if (row.status === "priced" && row.pending_recompute) lines.push(t("Prices on the next recompute."));
  if (reason === "no_components") lines.push(t("No hotels or services mapped to this tour."));
  if (reason === "inactive_component") lines.push(t("A hotel, room type or service of the tour is inactive."));
  if (reason === "no_rule") lines.push(t("No approved agency pricing rule is in force today."));
  if (reason === "missing_fx") lines.push(t("No approved exchange rate today for {0}.", row.missing_fx.join(", ")));
  if (reason === "missing_rates" && row.components_without_rate.length) {
    const n = row.components_without_rate.length;
    lines.push(t("{0} component{1} without a published rate:", n, n > 1 ? "s" : ""));
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
          {t("Day {0}: {1}", c.day_no, componentName(c))}
        </span>
      ))}
      {unrated.length > MAX_LISTED && <span className="text-xs text-muted">{t("and {0} more", unrated.length - MAX_LISTED)}</span>}
    </span>
  );
}

function Tour({ row }: { row: ReadinessItem }) {
  const t = useT(MSG);
  const tx = useTx();
  const out = outOfCatalog(row);
  const facts = [row.country, row.duration_days ? daysText(row.duration_days) : null, row.sellable ? t("Sellable") : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <span className="flex min-w-0 flex-col">
      <span className="font-medium text-ink">{tx(tourName(row, "Tour"))}</span>
      {facts && <span className="text-xs text-muted">{facts}</span>}
      {out && <span className="text-xs text-muted">{tx(OUT_OF_CATALOG[out])}</span>}
    </span>
  );
}

export function ReadinessView() {
  const t = useT(MSG);
  const tx = useTx();
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
      header: t("Tour"),
      accessorFn: (r) => r.title ?? r.tour_key,
      cell: ({ row }) => <Tour row={row.original} />,
    },
    {
      id: "status",
      header: t("Status"),
      accessorKey: "status",
      cell: ({ row }) => <PriceReadinessBadge status={row.original.status} />,
    },
    { id: "needs", header: t("What it needs"), enableSorting: false, accessorFn: (r) => r.reason ?? "", cell: ({ row }) => <Needs row={row.original} /> },
    { id: "components", header: t("Components"), accessorKey: "components" },
    {
      id: "rows",
      header: t("Price rows"),
      accessorFn: (r) => r.computed_rows + r.hand_rows,
      cell: ({ row }) => (
        <span className="flex flex-col whitespace-nowrap text-[13px]">
          <span>{row.original.computed_rows} {t("from rates")}</span>
          <span className="text-xs text-muted">{row.original.hand_rows} {t("by hand")}</span>
        </span>
      ),
    },
    {
      id: "from",
      header: t("From (RRP, 2 pax)"),
      accessorFn: (r) => Number(r.from_price?.amount ?? -1),
      cell: ({ row }) =>
        row.original.from_price ? <Money amount={row.original.from_price.amount} currency={row.original.from_price.currency} strong /> : "—",
    },
    {
      id: "open",
      header: t("Trip plan"),
      enableSorting: false,
      // AA-1128: the trip plan pages read the tour from the catalog: no link for a tour out of it (it would 404).
      cell: ({ row }) =>
        row.original.in_catalog === false ? (
          <span className="text-[13px] text-muted">—</span>
        ) : (
          <Link
            href={planHref(row.original)}
            onClick={(e) => e.stopPropagation()}
            className="whitespace-nowrap text-[13px] font-semibold text-accent-deep hover:underline"
          >
            {canEditPlans ? t("Open prices") : t("Open")}
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
      toast.error(err instanceof Error ? err.message : t("The report could not be downloaded."));
    } finally {
      setDownloading(false);
    }
  }

  const priced = counts ? counts.priced + counts.hand_priced : undefined;
  const filtered = !!(filters.status || filters.country || filters.q);
  return (
    <PageFrame
      title={t("Price readiness")}
      description={t("Which trip plans have a price, and what each of the others still needs. A plan prices from supplier rates once every hotel and service of the tour has a published rate, every rate currency has an approved exchange rate and an agency pricing rule is in force.")}
      actions={
        <Button variant="secondary" size="sm" onClick={download} disabled={downloading || !items.length}>
          <Download size={13} /> {t("Download CSV")}
        </Button>
      }
    >
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiTile label={t("Trip plans")} value={data?.summary.total} hint={data ? t("Rates read to {0}", data.horizon_to) : undefined} />
        <KpiTile
          label={t("Priced")}
          value={priced}
          hintTone="success"
          hint={counts ? t("{0} from rates, {1} by hand", counts.priced, counts.hand_priced) : undefined}
        />
        <KpiTile
          label={t("Missing rates or FX")}
          value={counts ? counts.missing_rates + counts.missing_fx : undefined}
          hintTone={counts && counts.missing_rates + counts.missing_fx > 0 ? "warning" : "neutral"}
          hint={counts ? t("{0} wait for an exchange rate", counts.missing_fx) : undefined}
        />
        <KpiTile
          label={t("No components")}
          value={counts?.no_components}
          hint={counts ? t("{0} with an inactive component", counts.inactive_component) : undefined}
        />
      </div>
      {data && data.rules.length === 0 && (
        <div className="rounded-lg border border-danger-border bg-danger-soft px-4 py-3 text-[13px] text-body" role="status">
          {t("No approved agency pricing rule is in force today, so no plan prices from rates.")}{" "}
          <Link href="/pricing/rules" className="font-semibold text-accent-deep hover:underline">
            {t("Open Pricing rules & FX")}
          </Link>
        </div>
      )}
      {!!data?.degraded.length && (
        <div className="rounded-lg border border-accent-border bg-accent-tint px-4 py-3 text-[13px] text-body" role="status">
          {t("The tour catalog could not be read: plans are listed by their key, without title or country.")}
        </div>
      )}
      <Section>
        <DataTable<ReadinessItem>
          filters={
            <FilterBar
              search={{ placeholder: t("Search a tour title or key") }}
              selects={[
                {
                  key: "status",
                  label: t("Status"),
                  allLabel: t("All statuses"),
                  options: READINESS_STATUSES.map((s) => ({
                    value: s,
                    label: counts ? `${tx(PRICE_READINESS_META[s].label)} (${counts[s]})` : tx(PRICE_READINESS_META[s].label),
                  })),
                  width: 210,
                },
                {
                  key: "country",
                  label: t("Country"),
                  allLabel: t("All countries"),
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
          onRowClick={(r) => {
            if (r.in_catalog !== false) router.push(planHref(r));
          }}
          emptyTitle={filtered ? t("No trip plans match") : t("No trip plans yet")}
          emptyDescription={
            filtered
              ? t("Change the search or the filters, or reset them to see every plan.")
              : t("Trip plans appear here once the catalog sync has created them.")
          }
          tableMinWidth={980}
        />
      </Section>
    </PageFrame>
  );
}
