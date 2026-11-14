"use client";
// app/(staff)/pricing/rules/_parts/PricingRulesView.tsx — Pricing rules & FX (W2, UI spec §3 / §6.3, ADR 0005, Q19).
// Two tabs over the append-only policy tables: pricing rules (margin policy per channel) and dated FX rates.
// A new row is PENDING until a second staff member approves it (maker-checker, bk-007); only approved rows
// price quotes. Rows are never edited: a change is a new row with a later date. Reads need pricing.read; New and
// Approve need pricing.write (admin, contracting), and the API refuses an approval by the row's creator (403
// own_row) — the Approve button follows the API's per-row `can_approve`.
// Layout (AA-683 S239): both tables fit a 1440 px screen without sideways scroll. Target and floor margin share one
// "Margin" column (target on top, floor under it, so the header stays short), the Approve action sits under the
// status pill of a pending row (no Actions column that is empty most of the time), and Note is the flexible column:
// two lines at most on desktop with the full text in a tooltip, the full text on phone cards.
// Width budget: with a pending row the rules table needs about 1005 px, so it still fits the 1084 px content frame at
// 1440 when <main> draws a classic scrollbar (6 px styled in Chrome/Edge, about 17 px in Firefox on Windows). Keep
// the Status cell no wider than the widest pill ("Scheduled", 95 px): a plain "Approve" button (78 px), and the
// "Needs a second person" hint wraps at 96 px. AA-910: FX values stay on one line; Source is cut at 180 px (tooltip).
// S280 (AA-1096 settings UI audit): Pricing › Policy links here, so the page has the same English / Vietnamese switch
// (messages.ts, the `bk_lang` cookie); a row takes effect today or later (the API refuses a past day, and an approval
// once the day has passed: 409 effective_date_passed); the FX "as of" day is the day the rate takes effect.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { pct } from "@/lib/proposalDraft";
import { pricingPolicyApi, type FxRateRow, type PolicyPerson, type PolicyStatus, type PricingRuleRow } from "@/lib/api/pricingPolicy";
import {
  Button,
  ConfirmDialog,
  DataTable,
  DateText,
  I18nProvider,
  LangSwitch,
  PageFrame,
  Pill,
  Section,
  langStore,
  useCan,
  useFormat,
  useT,
  useToast,
  useUrlFilters,
} from "@/ui";
import { SavedTabs } from "../../../_parts/SavedTabs";
import { NewFxRateDrawer, NewRuleDrawer } from "./PolicyDrawers";
import { R, type RKey } from "./messages";
import { TONE, apiErrorText, channelLabel, type RT } from "./rulesText";

function rate(value: string): string {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("en-US", { maximumFractionDigits: 8 }) : value;
}

function PolicyStatusBadge({ status }: { status: PolicyStatus }) {
  const t = useT(R);
  const known = status in TONE;
  return (
    <Pill tone={TONE[status] ?? "neutral"} title={known ? (t(`help_${status}` as RKey) as string) : ""}>
      {known ? (t(`status_${status}` as RKey) as string) : status}
    </Pill>
  );
}

/** Free-text note. Desktop table (Tailwind `md`, the kit DataTable's table mode): at most two lines with the full
 *  text in the tooltip, and never narrower than 110 px so it does not wrap word by word. Phone cards: the full text. */
function NoteText({ note }: { note: string | null }) {
  if (!note) return <span className="text-[13px] text-muted">—</span>;
  return (
    <span title={note} className="block break-words text-[13px] text-muted md:line-clamp-2 md:min-w-[90px]">
      {note}
    </span>
  );
}

function Person({ person, at }: { person: PolicyPerson | null; at?: string | null }) {
  const t = useT(R);
  if (!person) return <span className="text-[13px] text-muted">—</span>;
  const seed = person.id.includes(":");
  return (
    <span className="flex flex-col">
      <span className="text-[13px] text-ink">{seed ? (person.id.startsWith("seed") ? t("seed") : t("backfilled")) : person.name ?? person.id}</span>
      {at && <DateText value={at} relative className="text-xs text-muted" />}
    </span>
  );
}

/** Status pill; a pending row also gets its Approve button (or why it cannot be approved by this user) under it. */
function StatusCell({
  status,
  canApprove,
  canWrite,
  dayPassed,
  onApprove,
}: {
  status: PolicyStatus;
  canApprove: boolean;
  canWrite: boolean;
  dayPassed: boolean;
  onApprove: () => void;
}) {
  const t = useT(R);
  if (status !== "pending") return <PolicyStatusBadge status={status} />;
  // S280: a pending row past its day is never approved (409 effective_date_passed); say why instead of "Waiting".
  if (dayPassed) {
    return (
      <span className="flex flex-col items-end gap-1.5 md:items-start">
        <PolicyStatusBadge status={status} />
        <span className="block text-xs leading-snug text-muted md:max-w-[160px]">{t("dayPassed")}</span>
      </span>
    );
  }
  return (
    <span className="flex flex-col items-end gap-1.5 md:items-start">
      <PolicyStatusBadge status={status} />
      {canApprove ? (
        <span onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="secondary" onClick={onApprove} title={t("approveTitleHint") as string}>
            {t("approve")}
          </Button>
        </span>
      ) : (
        <span className="block text-xs leading-snug text-muted md:max-w-[96px]">{canWrite ? t("needsSecond") : t("waiting")}</span>
      )}
    </span>
  );
}

/** The margin cell: target, floor, the child / infant prices and the early-booking discount, one short line each. */
function MarginCell({ r }: { r: PricingRuleRow }) {
  const t = useT(R);
  const early = r.early_days != null && r.early_discount_pts != null;
  return (
    <span className="flex flex-col whitespace-nowrap tabular-nums" title={t("marginTitle") as string}>
      <span className="text-ink">{pct(r.target_margin)}</span>
      <span className="text-xs text-muted">{t("floor", pct(r.floor_margin))}</span>
      {(r.child_pct || r.infant_flat_usd) && (
        // AA-1075 (bk-15): the child share of the adult price and the infant net of the rule. One short line each
        // (AA-1073 rest): on one line they widened the nowrap Margin column past the 1084 px budget.
        <span className="flex flex-col text-xs text-muted" title={t("childTitle") as string}>
          <span>{r.child_pct ? t("child", pct(r.child_pct)) : t("childAsAdult")}</span>
          <span>{r.infant_flat_usd ? t("infant", String(Number(r.infant_flat_usd))) : t("infantFree")}</span>
        </span>
      )}
      {early && (
        // AA-1073 rest (bk-17): the early-booking discount, under the margin it comes off.
        <span className="flex flex-col text-xs text-muted" title={t("earlyTitle") as string}>
          <span>{t("earlyPts", String(Math.round(Number(r.early_discount_pts) * 10000) / 100))}</span>
          <span>{t("earlyFrom", r.early_days ?? 0)}</span>
        </span>
      )}
    </span>
  );
}

type Approving = { kind: "rule"; row: PricingRuleRow } | { kind: "fx"; row: FxRateRow } | null;

function useRuleColumns(canWrite: boolean, onApprove: (a: NonNullable<Approving>) => void): ColumnDef<PricingRuleRow, unknown>[] {
  const t = useT(R);
  return [
    { id: "channel", header: t("colChannel"), accessorFn: (r) => channelLabel(t, r.channel), cell: ({ getValue }) => <span className="font-medium text-ink">{getValue() as string}</span> },
    { id: "margin", header: t("colMargin"), accessorFn: (r) => Number(r.target_margin), cell: ({ row }) => <MarginCell r={row.original} /> },
    { id: "peak", header: t("colPeak"), accessorFn: (r) => Number(r.peak_uplift_pts), cell: ({ row }) => t("pts", pct(row.original.peak_uplift_pts).replace("%", "")) },
    { id: "card", header: t("colCard"), accessorFn: (r) => Number(r.card_fee_pct), cell: ({ row }) => pct(row.original.card_fee_pct) },
    { id: "rrp", header: t("colRrp"), accessorFn: (r) => Number(r.rrp_markup_pct ?? -1), cell: ({ row }) => (row.original.rrp_markup_pct ? `+${pct(row.original.rrp_markup_pct)}` : "—") },
    { id: "valid_from", header: t("colValidFrom"), accessorKey: "valid_from", cell: ({ row }) => <DateText value={row.original.valid_from} /> },
    {
      id: "status",
      header: t("colStatus"),
      accessorKey: "status",
      cell: ({ row }) => (
        <StatusCell status={row.original.status} canApprove={row.original.can_approve} canWrite={canWrite} dayPassed={row.original.day_passed} onApprove={() => onApprove({ kind: "rule", row: row.original })} />
      ),
    },
    { id: "created_by", header: t("colEnteredBy"), accessorFn: (r) => r.created_by?.name ?? "", cell: ({ row }) => <Person person={row.original.created_by} at={row.original.created_by ? row.original.created_at : null} /> },
    { id: "approved_by", header: t("colApprovedBy"), accessorFn: (r) => r.approved_by?.name ?? "", cell: ({ row }) => <Person person={row.original.approved_by} at={row.original.approved_at} /> },
    { id: "note", header: t("colNote"), accessorFn: (r) => r.note ?? "", cell: ({ row }) => <NoteText note={row.original.note} /> },
  ];
}

function useFxColumns(canWrite: boolean, onApprove: (a: NonNullable<Approving>) => void): ColumnDef<FxRateRow, unknown>[] {
  const t = useT(R);
  return [
    { id: "currency", header: t("colCurrency"), accessorKey: "currency", cell: ({ row }) => <span className="font-mono font-semibold text-ink">{row.original.currency}</span> },
    { id: "rate", header: t("colRate"), accessorFn: (r) => Number(r.rate_per_usd), cell: ({ row }) => <span className="whitespace-nowrap tabular-nums">{rate(row.original.rate_per_usd)}</span> },
    { id: "buffer", header: t("colBuffer"), accessorFn: (r) => Number(r.buffer_pct), cell: ({ row }) => pct(row.original.buffer_pct) },
    { id: "as_of", header: t("colAsOf"), accessorKey: "as_of", cell: ({ row }) => <DateText value={row.original.as_of} className="whitespace-nowrap" /> },
    {
      id: "source",
      header: t("colSource"),
      accessorKey: "source",
      // AA-910: one line on desktop (the full text in the tooltip). S250: 140 px so the FX table fits 1400 px.
      cell: ({ row }) => (
        <span title={row.original.source} className="block break-words text-[13px] text-muted md:max-w-[140px] md:truncate">
          {row.original.source}
        </span>
      ),
    },
    {
      id: "status",
      header: t("colStatus"),
      accessorKey: "status",
      cell: ({ row }) => (
        <StatusCell status={row.original.status} canApprove={row.original.can_approve} canWrite={canWrite} dayPassed={row.original.day_passed} onApprove={() => onApprove({ kind: "fx", row: row.original })} />
      ),
    },
    { id: "created_by", header: t("colEnteredBy"), accessorFn: (r) => r.created_by?.name ?? "", cell: ({ row }) => <Person person={row.original.created_by} at={row.original.created_by ? row.original.created_at : null} /> },
    { id: "approved_by", header: t("colApprovedBy"), accessorFn: (r) => r.approved_by?.name ?? "", cell: ({ row }) => <Person person={row.original.approved_by} at={row.original.approved_at} /> },
  ];
}

function ApproveDialog({ approving, busy, error, onClose, onConfirm }: { approving: Approving; busy: boolean; error: string | null; onClose: () => void; onConfirm: () => void }) {
  const t = useT(R);
  const { formatDate } = useFormat();
  const body = (): string | null => {
    if (approving?.kind === "fx") {
      const r = approving.row;
      return t("approveFxBody", r.currency, rate(r.rate_per_usd), pct(r.buffer_pct), formatDate(r.as_of), r.source) as string;
    }
    if (approving?.kind === "rule") {
      const r = approving.row;
      return t("approveRuleBody", channelLabel(t, r.channel), pct(r.target_margin), pct(r.floor_margin),
        pct(r.peak_uplift_pts).replace("%", ""), pct(r.card_fee_pct), formatDate(r.valid_from)) as string;
    }
    return null;
  };
  return (
    <ConfirmDialog
      open={approving !== null}
      onClose={onClose}
      onConfirm={onConfirm}
      busy={busy}
      error={error}
      title={(approving?.kind === "fx" ? t("approveFxTitle") : t("approveRuleTitle")) as string}
      confirmLabel={t("approve") as string}
      message={body()}
    />
  );
}

function useApprove(t: RT, onDone: () => void) {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (a: NonNullable<Approving>): Promise<PricingRuleRow | FxRateRow> =>
      a.kind === "rule" ? pricingPolicyApi.approveRule(a.row.id) : pricingPolicyApi.approveFxRate(a.row.id),
    onSuccess: (row, a) => {
      qc.invalidateQueries({ queryKey: ["pricing"] });
      const status = row.status in TONE ? (t(`status_${row.status}` as RKey) as string) : row.status;
      toast.success((a.kind === "rule" ? t("approvedRule", status) : t("approvedFx", status)) as string);
      onDone();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "already_approved") qc.invalidateQueries({ queryKey: ["pricing"] });
    },
  });
}

function PageActions({ canWrite, onNew }: { canWrite: boolean; onNew: (d: "rule" | "fx") => void }) {
  const t = useT(R);
  return (
    <div className="flex flex-wrap items-center gap-2">
      <LangSwitch ariaLabel={t("language")} />
      {canWrite && (
        <>
          <Button variant="secondary" size="sm" onClick={() => onNew("fx")}>
            <Plus size={13} /> {t("newFx")}
          </Button>
          <Button variant="primary" size="sm" onClick={() => onNew("rule")}>
            <Plus size={13} /> {t("newRule")}
          </Button>
        </>
      )}
    </div>
  );
}

type ListQuery = { isLoading: boolean; isError: boolean; error: unknown; refetch: () => unknown };

function RulesTab({ q, rows, columns }: { q: ListQuery; rows: PricingRuleRow[]; columns: ColumnDef<PricingRuleRow, unknown>[] }) {
  const t = useT(R);
  return (
    <Section title={t("tabRules")} description={t("rulesHint")}>
      <DataTable<PricingRuleRow>
        tableId="pricing-rules"
        data={rows}
        columns={columns}
        getRowId={(r) => r.id}
        loading={q.isLoading}
        error={q.isError ? apiErrorText(t, q.error) : null}
        onRetry={() => q.refetch()}
        searchable={false}
        pageSizeOptions={[25, 50, 100]}
        emptyTitle={t("rulesEmpty")}
        emptyDescription={t("rulesEmptyHint")}
      />
    </Section>
  );
}

function FxTab({ q, rows, columns, canWrite }: { q: ListQuery; rows: FxRateRow[]; columns: ColumnDef<FxRateRow, unknown>[]; canWrite: boolean }) {
  const t = useT(R);
  const inForce = rows.filter((r) => r.status === "in_force").map((r) => r.currency);
  return (
    <Section title={t("tabFx")} description={inForce.length ? t("fxHintInForce", inForce.join(", ")) : t("fxHintNone")}>
      <DataTable<FxRateRow>
        tableId="fx-rates"
        data={rows}
        columns={columns}
        getRowId={(r) => r.id}
        loading={q.isLoading}
        error={q.isError ? apiErrorText(t, q.error) : null}
        onRetry={() => q.refetch()}
        searchable={false}
        pageSizeOptions={[25, 50, 100]}
        emptyTitle={t("fxEmpty")}
        emptyDescription={canWrite ? t("fxEmptyWriter") : t("fxEmptyReader")}
      />
    </Section>
  );
}

function RulesAndFx() {
  const t = useT(R);
  const canWrite = useCan("pricing.write");
  const { get } = useUrlFilters();
  const tab = get("tab") === "fx" ? "fx" : "rules";
  const [drawer, setDrawer] = useState<"rule" | "fx" | null>(null);
  const [approving, setApproving] = useState<Approving>(null);
  const rules = useQuery({ queryKey: ["pricing", "rules"], queryFn: () => pricingPolicyApi.rules() });
  const fx = useQuery({ queryKey: ["pricing", "fx-rates"], queryFn: () => pricingPolicyApi.fxRates() });
  const ruleRows = useMemo(() => rules.data?.items ?? [], [rules.data]);
  const fxRows = useMemo(() => fx.data?.items ?? [], [fx.data]);
  const approve = useApprove(t, () => setApproving(null));
  const startApprove = (a: NonNullable<Approving>) => {
    approve.reset();
    setApproving(a);
  };
  const ruleColumns = useRuleColumns(canWrite, startApprove);
  const fxColumns = useFxColumns(canWrite, startApprove);
  const pendingRules = ruleRows.filter((r) => r.status === "pending").length;
  const pendingFx = fxRows.filter((r) => r.status === "pending").length;

  return (
    <PageFrame
      title={t("title")}
      description={t("description")}
      actions={<PageActions canWrite={canWrite} onNew={setDrawer} />}
    >
      <div className="rounded-lg border border-accent-border bg-accent-tint px-4 py-3 text-[13px] text-body">
        <strong className="text-ink">{t("bannerLead")}</strong> {t("banner")}
      </div>
      <SavedTabs
        tabs={[
          { key: "rules", label: t("tabRules") as string, count: pendingRules || undefined },
          { key: "fx", label: t("tabFx") as string, count: pendingFx || undefined },
        ]}
      />
      {tab === "rules" ? (
        <RulesTab q={rules} rows={ruleRows} columns={ruleColumns} />
      ) : (
        <FxTab q={fx} rows={fxRows} columns={fxColumns} canWrite={canWrite} />
      )}

      <ApproveDialog
        approving={approving}
        busy={approve.isPending}
        error={approve.error ? apiErrorText(t, approve.error) : null}
        onClose={() => setApproving(null)}
        onConfirm={() => approving && approve.mutate(approving)}
      />
      {drawer === "rule" && <NewRuleDrawer onClose={() => setDrawer(null)} />}
      {drawer === "fx" && <NewFxRateDrawer onClose={() => setDrawer(null)} />}
    </PageFrame>
  );
}

/** The page in its own language (English by default; the switch keeps the choice in the `bk_lang` cookie). */
export function PricingRulesView() {
  return (
    <I18nProvider store={langStore}>
      <RulesAndFx />
    </I18nProvider>
  );
}
