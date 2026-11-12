"use client";
// app/(staff)/pricing/workbench/_parts/WorkbenchView.tsx — Pricing → Price setup (AA-1091 item 1). Every trip plan
// with its seven checks (hotels and services, supplier rates, exchange rate, pricing rule, price, sellable, departures)
// and its next step (GET /booking/catalog/price-workbench), filters by next step, missing check, price status, country
// and search (URL query). Each row opens the tour's hotels and services (ComponentsDrawer: suggestions from the CIS
// itinerary) or the page of its next step; staff who edit plans select rows for the bulk actions (make sellable or
// not, show the CIS reference as a provisional price, clear it, recompute). "Import workbook" is the bulk Excel import
// with its dry run (ImportDrawer). The page has its own language switch (English default, Vietnamese; messages.ts).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Check, Download, FileUp, Minus, X } from "lucide-react";
import { READINESS_STATUSES } from "@/lib/api/priceReadiness";
import {
  NEXT_STEPS,
  WORKBENCH_CHECKS,
  priceWorkbenchApi,
  type WorkbenchAction,
  type WorkbenchCheck,
  type WorkbenchItem,
} from "@/lib/api/priceWorkbench";
import { skippedReasons, stepHref } from "@/ui/booking/priceWorkbench";
import {
  Button,
  DataTable,
  FilterBar,
  I18nProvider,
  KpiTile,
  LangSwitch,
  LinkButton,
  Money,
  PageFrame,
  Pill,
  Section,
  langStore,
  useCan,
  useT,
  useToast,
  useUrlFilters,
  type ColumnDef,
} from "@/ui";
import { ComponentsDrawer } from "./ComponentsDrawer";
import { ImportDrawer } from "./ImportDrawer";
import { M, type MessageKey } from "./messages";

const RECOMPUTE_MAX = 50;
const STEP_TONE: Record<string, "success" | "warning" | "danger" | "info" | "neutral"> = {
  done: "success",
  make_sellable: "info",
  recompute: "info",
  approve_rule: "danger",
  fix_inactive: "danger",
};

function CheckIcon({ ok, optional }: { ok: boolean; optional?: boolean }) {
  if (ok) return <Check size={13} className="text-success" aria-hidden="true" />;
  return optional ? <Minus size={13} className="text-muted" aria-hidden="true" /> : <X size={13} className="text-danger" aria-hidden="true" />;
}

function Checks({ row }: { row: WorkbenchItem }) {
  const t = useT(M);
  const c = row.checks;
  const detail: Partial<Record<WorkbenchCheck, string>> = {
    components: String(c.components.count),
    rates: c.rates.total ? t("checkRates", c.rates.rated, c.rates.total) : undefined,
    fx: c.fx.missing.length ? c.fx.missing.join(", ") : undefined,
    departures: c.departures.count ? t("checkDepartures", c.departures.count) : t("checkOptional"),
  };
  return (
    <ul className="grid min-w-0 grid-cols-1 gap-x-3 gap-y-0.5 text-xs sm:grid-cols-2" data-testid="workbench-checks">
      {WORKBENCH_CHECKS.map((name) => (
        <li key={name} className="flex min-w-0 items-center gap-1">
          <CheckIcon ok={c[name].ok} optional={name === "departures"} />
          <span className={c[name].ok ? "text-body" : "text-ink"}>{t(`check_${name}` as MessageKey) as string}</span>
          {detail[name] && <span className="truncate text-muted">· {detail[name]}</span>}
        </li>
      ))}
    </ul>
  );
}

function Price({ row }: { row: WorkbenchItem }) {
  const t = useT(M);
  if (row.from_price) {
    return (
      <span className="flex flex-col whitespace-nowrap">
        <Money amount={row.from_price.amount} currency={row.from_price.currency} strong />
        <span className="text-xs text-muted">{t("rrp2")}</span>
      </span>
    );
  }
  if (row.provisional) {
    return (
      <span className="flex flex-col gap-0.5" data-testid="provisional-price">
        <Money amount={row.provisional.amount} currency={row.provisional.currency} />
        <Pill tone="warning">
          {t("provisional")}: {t("noSupplierPrice")}
        </Pill>
      </span>
    );
  }
  return (
    <span className="flex flex-col gap-0.5 text-[13px] text-muted">
      {t("noPrice")}
      {row.reference && (
        <span className="break-words text-xs">
          {t("reference", row.reference.amount ? `${row.reference.amount} ${row.reference.currency}` : (row.reference.raw ?? ""))}
        </span>
      )}
    </span>
  );
}

function Tour({ row }: { row: WorkbenchItem }) {
  const t = useT(M);
  const facts = [row.country, row.duration_days ? t("days", row.duration_days) : null, row.sellable ? t("sellable") : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <span className="flex min-w-0 flex-col">
      <span className="break-words font-medium text-ink">{row.title ?? row.tour_key}</span>
      {facts && <span className="text-xs text-muted">{facts}</span>}
      {row.in_catalog === false && <span className="text-xs text-muted">{t("notInCatalog")}</span>}
    </span>
  );
}

function Workbench() {
  const t = useT(M);
  const toast = useToast();
  const qc = useQueryClient();
  const { get } = useUrlFilters();
  const canEdit = useCan("trip_plan.write");
  const filters = {
    status: get("status"),
    country: get("country"),
    q: get("q"),
    missing: get("missing"),
    next_step: get("next_step"),
  };
  const [importing, setImporting] = useState(false);
  const [components, setComponents] = useState<WorkbenchItem | null>(null);
  const [downloading, setDownloading] = useState(false);

  const report = useQuery({
    queryKey: ["price-workbench", filters.status, filters.country, filters.q, filters.missing, filters.next_step],
    queryFn: () => priceWorkbenchApi.get(filters),
    placeholderData: (previous) => previous,
  });
  const data = report.data;
  const summary = data?.summary;
  const items = useMemo(() => data?.items ?? [], [data]);

  const act = useMutation({
    mutationFn: ({ action, keys }: { action: WorkbenchAction; keys: string[] }) => priceWorkbenchApi.action(action, keys),
    onSuccess: (out) => {
      const reasons = skippedReasons(out.results)
        .map(([r, n]) => `${t(`reason_${r}` as MessageKey) as string} (${n})`)
        .join(", ");
      toast.success(`${t("actDone", out.done, out.unchanged, out.skipped)}${reasons ? `: ${reasons}` : ""}`);
      qc.invalidateQueries({ queryKey: ["price-workbench"] });
      qc.invalidateQueries({ queryKey: ["price-readiness"] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : t("actFailed")),
  });

  async function downloadWorkbook() {
    setDownloading(true);
    try {
      const { blob, filename } = await priceWorkbenchApi.template(filters);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setDownloading(false);
    }
  }

  const columns: ColumnDef<WorkbenchItem, unknown>[] = [
    { id: "tour", header: t("colTour"), accessorFn: (r) => r.title ?? r.tour_key, cell: ({ row }) => <Tour row={row.original} /> },
    {
      id: "checks",
      header: t("colChecks"),
      enableSorting: false,
      accessorFn: (r) => r.missing.length,
      cell: ({ row }) => <Checks row={row.original} />,
    },
    {
      id: "price",
      header: t("colPrice"),
      accessorFn: (r) => Number(r.from_price?.amount ?? r.provisional?.amount ?? -1),
      cell: ({ row }) => <Price row={row.original} />,
    },
    {
      id: "next",
      header: t("colNext"),
      accessorFn: (r) => NEXT_STEPS.indexOf(r.next_step),
      cell: ({ row }) => {
        const r = row.original;
        const opensDrawer = r.next_step === "add_components" || r.next_step === "fix_inactive";
        return (
          <span className="flex flex-col items-start gap-1">
            <Pill tone={STEP_TONE[r.next_step] ?? "warning"}>{t(`step_${r.next_step}` as MessageKey) as string}</Pill>
            <span className="flex flex-wrap gap-x-3 gap-y-1 text-[13px]">
              <LinkButton
                onClick={(e) => {
                  e.stopPropagation();
                  setComponents(r);
                }}
              >
                {t("openComponents")}
              </LinkButton>
              {!opensDrawer && (
                <Link
                  href={stepHref(r.next_step, r.tour_key)}
                  onClick={(e) => e.stopPropagation()}
                  className="font-semibold text-accent-deep hover:underline"
                >
                  {t("openPlan")}
                </Link>
              )}
            </span>
          </span>
        );
      },
    },
  ];

  const stepOptions = NEXT_STEPS.map((s) => ({
    value: s,
    label: summary ? `${t(`step_${s}` as MessageKey) as string} (${summary.by_next_step[s]})` : (t(`step_${s}` as MessageKey) as string),
  }));
  const filtered = Object.values(filters).some(Boolean);
  return (
    <PageFrame
      title={t("title")}
      description={t("description")}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <LangSwitch ariaLabel={t("language")} />
          <Button variant="secondary" size="sm" onClick={downloadWorkbook} disabled={downloading || !items.length}>
            <Download size={13} /> {t("downloadWorkbook")}
          </Button>
          {canEdit && (
            <Button variant="primary" size="sm" onClick={() => setImporting(true)}>
              <FileUp size={13} /> {t("importWorkbook")}
            </Button>
          )}
        </div>
      }
    >
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <KpiTile label={t("kpiPlans")} value={summary?.total} />
        <KpiTile
          label={t("kpiReady")}
          value={summary?.by_next_step.done}
          hintTone="success"
          hint={summary ? t("kpiReadyHint", summary.by_status.priced + summary.by_status.hand_priced) : undefined}
        />
        <KpiTile label={t("kpiComponents")} value={summary?.by_missing.components} />
        <KpiTile label={t("kpiRates")} value={summary?.by_next_step.load_rates} hintTone="warning" />
        <KpiTile label={t("kpiProvisional")} value={summary?.provisional} hint={t("kpiProvisionalHint")} />
      </div>
      {data && data.rules.length === 0 && (
        <div className="rounded-lg border border-danger-border bg-danger-soft px-4 py-3 text-[13px] text-body" role="status">
          {t("noRule")}{" "}
          <Link href="/pricing/rules" className="font-semibold text-accent-deep hover:underline">
            {t("openRules")}
          </Link>
        </div>
      )}
      {!!data?.degraded.length && (
        <div className="rounded-lg border border-accent-border bg-accent-tint px-4 py-3 text-[13px] text-body" role="status">
          {t("degraded")}
        </div>
      )}
      <Section>
        <DataTable<WorkbenchItem>
          filters={
            <FilterBar
              search={{ placeholder: t("search") }}
              selects={[
                { key: "next_step", label: t("filterStep"), allLabel: t("filterStepAll"), options: stepOptions, width: 220 },
                {
                  key: "missing",
                  label: t("filterMissing"),
                  allLabel: t("filterMissingAll"),
                  options: WORKBENCH_CHECKS.map((c) => ({
                    value: c,
                    label: summary
                      ? `${t(`check_${c}` as MessageKey) as string} (${summary.by_missing[c]})`
                      : (t(`check_${c}` as MessageKey) as string),
                  })),
                  width: 200,
                },
                {
                  key: "status",
                  label: t("filterStatus"),
                  allLabel: t("filterStatusAll"),
                  options: READINESS_STATUSES.map((s) => ({ value: s, label: t(`status_${s}` as MessageKey) as string })),
                  width: 190,
                },
                {
                  key: "country",
                  label: t("filterCountry"),
                  allLabel: t("filterCountryAll"),
                  options: (summary?.by_country ?? []).map((c) => ({ value: c.country, label: `${c.country} (${c.total})` })),
                  width: 170,
                },
              ]}
            />
          }
          searchable={false}
          tableId="price-workbench"
          data={items}
          columns={columns}
          getRowId={(r) => r.tour_key}
          loading={report.isLoading}
          error={report.isError ? (report.error as Error).message : null}
          onRetry={() => report.refetch()}
          onRowClick={(r) => setComponents(r)}
          enableSelection={canEdit}
          bulkActions={({ selectedRows, clearSelection }) => {
            const keys = selectedRows.map((r) => r.tour_key);
            const run = (action: WorkbenchAction) =>
              act.mutate({ action, keys }, { onSuccess: () => clearSelection() });
            return (
              <div className="flex flex-wrap items-center gap-2" data-testid="workbench-bulk">
                <span className="text-[13px] text-muted">{t("selected", keys.length)}</span>
                <Button size="sm" variant="primary" disabled={act.isPending} onClick={() => run("make_sellable")}>
                  {t("actMakeSellable")}
                </Button>
                <Button size="sm" variant="secondary" disabled={act.isPending} onClick={() => run("make_not_sellable")}>
                  {t("actMakeNotSellable")}
                </Button>
                <Button size="sm" variant="secondary" disabled={act.isPending} onClick={() => run("apply_provisional")}>
                  {t("actApplyProvisional")}
                </Button>
                <Button size="sm" variant="ghost" disabled={act.isPending} onClick={() => run("clear_provisional")}>
                  {t("actClearProvisional")}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={act.isPending || keys.length > RECOMPUTE_MAX}
                  title={keys.length > RECOMPUTE_MAX ? t("recomputeLimit") : undefined}
                  onClick={() => run("recompute")}
                >
                  {t("actRecompute")}
                </Button>
              </div>
            );
          }}
          emptyTitle={t("emptyTitle")}
          emptyDescription={filtered ? t("emptyHint") : t("emptyAll")}
          tableMinWidth={1040}
        />
      </Section>
      {importing && <ImportDrawer filters={filters} onClose={() => setImporting(false)} />}
      {components && (
        <ComponentsDrawer sourceId={components.tour_key} title={components.title} onClose={() => setComponents(null)} />
      )}
    </PageFrame>
  );
}

/** The page in its own language (English by default; the switch keeps the choice in the `bk_lang` cookie). */
export function WorkbenchView() {
  return (
    <I18nProvider store={langStore}>
      <Workbench />
    </I18nProvider>
  );
}
