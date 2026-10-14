"use client";
// app/(staff)/pricing/rules/_parts/PricingRulesView.tsx — Pricing rules & FX (W2, UI spec §3 / §6.3, ADR 0005, Q19).
// Two tabs over the append-only policy tables: pricing rules (margin policy per channel) and dated FX rates.
// A new row is PENDING until a second staff member approves it (maker-checker, bk-007); only approved rows
// price quotes. Rows are never edited: a change is a new row with a later date. Reads need pricing.read; New and
// Approve need pricing.write (admin, contracting), and the API refuses an approval by the row's creator (403
// own_row) — the Approve button follows the API's per-row `can_approve`.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCircle2, Plus } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { CHANNELS } from "@/lib/api/proposals";
import { pct } from "@/lib/proposalDraft";
import { pricingPolicyApi, type FxRateRow, type PolicyPerson, type PolicyStatus, type PricingRuleRow } from "@/lib/api/pricingPolicy";
import { Button, ConfirmDialog, DataTable, DateText, PageFrame, Pill, Section, useCan, useToast, useUrlFilters, type BookingTone } from "@/ui";
import { SavedTabs } from "../../../_parts/SavedTabs";
import { NewFxRateDrawer, NewRuleDrawer } from "./PolicyDrawers";

const STATUS_META: Record<PolicyStatus, { label: string; tone: BookingTone; help: string }> = {
  pending: { label: "Pending approval", tone: "warning", help: "Waiting for a second person. Not used for prices." },
  in_force: { label: "In force", tone: "success", help: "Used for prices today." },
  scheduled: { label: "Scheduled", tone: "info", help: "Approved; takes over on its date." },
  superseded: { label: "Superseded", tone: "neutral", help: "Approved, replaced by a newer approved row." },
};

const CHANNEL_LABEL: Record<string, string> = Object.fromEntries(CHANNELS.map((c) => [c.value, c.label]));

function rate(value: string): string {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("en-US", { maximumFractionDigits: 8 }) : value;
}

function PolicyStatusBadge({ status }: { status: PolicyStatus }) {
  const meta = STATUS_META[status] ?? { label: status, tone: "neutral" as const, help: "" };
  return (
    <Pill tone={meta.tone} title={meta.help}>
      {meta.label}
    </Pill>
  );
}

function Person({ person, at }: { person: PolicyPerson | null; at?: string | null }) {
  if (!person) return <span className="text-[13px] text-muted">—</span>;
  const seed = person.id.includes(":");
  return (
    <span className="flex flex-col">
      <span className="text-[13px] text-ink">{seed ? (person.id.startsWith("seed") ? "Migration seed" : "Back-filled") : person.name ?? person.id}</span>
      {at && <DateText value={at} relative className="text-xs text-muted" />}
    </span>
  );
}

function approveErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "own_row") return "You created this row. A second person has to approve it (maker-checker).";
    if (err.code === "already_approved") return "Someone approved this row already. The list is refreshed.";
    if (err.code === "rule_not_found" || err.code === "fx_rate_not_found") return "This row no longer exists.";
  }
  return err instanceof Error ? err.message : "Could not approve the row.";
}

type Approving = { kind: "rule"; row: PricingRuleRow } | { kind: "fx"; row: FxRateRow } | null;

export function PricingRulesView() {
  const canWrite = useCan("pricing.write");
  const qc = useQueryClient();
  const toast = useToast();
  const { get } = useUrlFilters();
  const tab = get("tab") === "fx" ? "fx" : "rules";
  const [drawer, setDrawer] = useState<"rule" | "fx" | null>(null);
  const [approving, setApproving] = useState<Approving>(null);

  const rules = useQuery({ queryKey: ["pricing", "rules"], queryFn: () => pricingPolicyApi.rules() });
  const fx = useQuery({ queryKey: ["pricing", "fx-rates"], queryFn: () => pricingPolicyApi.fxRates() });
  const ruleRows = useMemo(() => rules.data?.items ?? [], [rules.data]);
  const fxRows = useMemo(() => fx.data?.items ?? [], [fx.data]);

  const approve = useMutation({
    mutationFn: (a: NonNullable<Approving>): Promise<PricingRuleRow | FxRateRow> =>
      a.kind === "rule" ? pricingPolicyApi.approveRule(a.row.id) : pricingPolicyApi.approveFxRate(a.row.id),
    onSuccess: (row, a) => {
      qc.invalidateQueries({ queryKey: ["pricing"] });
      toast.success(`${a.kind === "rule" ? "Rule" : "FX rate"} approved: ${STATUS_META[row.status]?.label ?? row.status}.`);
      setApproving(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "already_approved") qc.invalidateQueries({ queryKey: ["pricing"] });
    },
  });

  function approveCell(onClick: () => void, canApprove: boolean, status: PolicyStatus) {
    if (status !== "pending") return null;
    if (!canApprove) {
      return <span className="text-xs text-muted">{canWrite ? "Needs a second person" : "Waiting"}</span>;
    }
    return (
      <span onClick={(e) => e.stopPropagation()}>
        <Button size="sm" variant="secondary" onClick={onClick}>
          <CheckCircle2 size={13} /> Approve
        </Button>
      </span>
    );
  }

  const ruleColumns: ColumnDef<PricingRuleRow, unknown>[] = [
    { id: "channel", header: "Channel", accessorFn: (r) => CHANNEL_LABEL[r.channel] ?? r.channel, cell: ({ getValue }) => <span className="font-medium text-ink">{getValue() as string}</span> },
    { id: "target", header: "Target margin", accessorFn: (r) => Number(r.target_margin), cell: ({ row }) => pct(row.original.target_margin) },
    { id: "floor", header: "Floor margin", accessorFn: (r) => Number(r.floor_margin), cell: ({ row }) => pct(row.original.floor_margin) },
    { id: "peak", header: "Peak uplift", accessorFn: (r) => Number(r.peak_uplift_pts), cell: ({ row }) => `+${pct(row.original.peak_uplift_pts).replace("%", "")} pts` },
    { id: "card", header: "Card fee", accessorFn: (r) => Number(r.card_fee_pct), cell: ({ row }) => pct(row.original.card_fee_pct) },
    { id: "valid_from", header: "Valid from", accessorKey: "valid_from", cell: ({ row }) => <DateText value={row.original.valid_from} /> },
    { id: "status", header: "Status", accessorKey: "status", cell: ({ row }) => <PolicyStatusBadge status={row.original.status} /> },
    { id: "created_by", header: "Entered by", accessorFn: (r) => r.created_by?.name ?? "", cell: ({ row }) => <Person person={row.original.created_by} at={row.original.created_by ? row.original.created_at : null} /> },
    { id: "approved_by", header: "Approved by", accessorFn: (r) => r.approved_by?.name ?? "", cell: ({ row }) => <Person person={row.original.approved_by} at={row.original.approved_at} /> },
    { id: "note", header: "Note", accessorFn: (r) => r.note ?? "", cell: ({ row }) => <span className="text-[13px] text-muted">{row.original.note ?? "—"}</span> },
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => approveCell(() => { approve.reset(); setApproving({ kind: "rule", row: row.original }); }, row.original.can_approve, row.original.status),
    },
  ];

  const fxColumns: ColumnDef<FxRateRow, unknown>[] = [
    { id: "currency", header: "Currency", accessorKey: "currency", cell: ({ row }) => <span className="font-mono font-semibold text-ink">{row.original.currency}</span> },
    { id: "rate", header: "Units per 1 USD", accessorFn: (r) => Number(r.rate_per_usd), cell: ({ row }) => <span className="tabular-nums">{rate(row.original.rate_per_usd)}</span> },
    { id: "buffer", header: "Selling buffer", accessorFn: (r) => Number(r.buffer_pct), cell: ({ row }) => pct(row.original.buffer_pct) },
    { id: "as_of", header: "As of", accessorKey: "as_of", cell: ({ row }) => <DateText value={row.original.as_of} /> },
    { id: "source", header: "Source", accessorKey: "source", cell: ({ row }) => <span className="text-[13px] text-muted">{row.original.source}</span> },
    { id: "status", header: "Status", accessorKey: "status", cell: ({ row }) => <PolicyStatusBadge status={row.original.status} /> },
    { id: "created_by", header: "Entered by", accessorFn: (r) => r.created_by?.name ?? "", cell: ({ row }) => <Person person={row.original.created_by} at={row.original.created_by ? row.original.created_at : null} /> },
    { id: "approved_by", header: "Approved by", accessorFn: (r) => r.approved_by?.name ?? "", cell: ({ row }) => <Person person={row.original.approved_by} at={row.original.approved_at} /> },
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => approveCell(() => { approve.reset(); setApproving({ kind: "fx", row: row.original }); }, row.original.can_approve, row.original.status),
    },
  ];

  const pendingRules = ruleRows.filter((r) => r.status === "pending").length;
  const pendingFx = fxRows.filter((r) => r.status === "pending").length;
  const inForceFx = fxRows.filter((r) => r.status === "in_force").map((r) => r.currency);

  return (
    <PageFrame
      title="Pricing rules & FX"
      description="Margin policy per channel and dated FX rates against USD. Append-only: a change is a new row, and a second person approves it before it prices anything."
      actions={
        canWrite ? (
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" onClick={() => setDrawer("fx")}>
              <Plus size={13} /> New FX rate
            </Button>
            <Button variant="primary" size="sm" onClick={() => setDrawer("rule")}>
              <Plus size={13} /> New rule
            </Button>
          </div>
        ) : undefined
      }
    >
      <div className="rounded-lg border border-accent-border bg-accent-tint px-4 py-3 text-[13px] text-body">
        <strong className="text-ink">Maker-checker.</strong> Every new rule or FX rate starts as{" "}
        <em>Pending approval</em> and is not used until another staff member approves it. The person who entered a row
        cannot approve it. Rows are never edited: enter a new row with a later date instead.
      </div>
      <SavedTabs
        tabs={[
          { key: "rules", label: "Pricing rules", count: pendingRules || undefined },
          { key: "fx", label: "FX rates", count: pendingFx || undefined },
        ]}
      />
      {tab === "rules" ? (
        <Section
          title="Pricing rules"
          description="Selling price = net cost ÷ (1 − margin − card fee). Peak quotes add the uplift points to the target margin. The newest approved rule dated on or before today is in force."
        >
          <DataTable<PricingRuleRow>
            tableId="pricing-rules"
            data={ruleRows}
            columns={ruleColumns}
            getRowId={(r) => r.id}
            loading={rules.isLoading}
            error={rules.isError ? (rules.error as Error).message : null}
            onRetry={() => rules.refetch()}
            searchable={false}
            pageSizeOptions={[25, 50, 100]}
            emptyTitle="No pricing rules"
            emptyDescription="The ADR 0005 seed rules come with migration bk-004. Add a rule to change a margin."
            tableMinWidth={1100}
          />
        </Section>
      ) : (
        <Section
          title="FX rates"
          description={
            inForceFx.length
              ? `Units of each currency per 1 USD. In force today: ${inForceFx.join(", ")}. Costs convert at the plain rate; a non-USD selling price adds the buffer.`
              : "Units of each currency per 1 USD. No approved rate is in force yet, so only USD prices work. Costs convert at the plain rate; a non-USD selling price adds the buffer."
          }
        >
          <DataTable<FxRateRow>
            tableId="fx-rates"
            data={fxRows}
            columns={fxColumns}
            getRowId={(r) => r.id}
            loading={fx.isLoading}
            error={fx.isError ? (fx.error as Error).message : null}
            onRetry={() => fx.refetch()}
            searchable={false}
            pageSizeOptions={[25, 50, 100]}
            emptyTitle="No FX rates yet"
            emptyDescription={
              canWrite
                ? "Add a dated rate (for example EUR from the ECB). A second person approves it before quotes use it."
                : "An admin or contracting user adds dated rates; until then only USD prices work."
            }
            tableMinWidth={1000}
          />
        </Section>
      )}

      <ConfirmDialog
        open={approving !== null}
        onClose={() => setApproving(null)}
        onConfirm={() => approving && approve.mutate(approving)}
        busy={approve.isPending}
        error={approve.error ? approveErrorText(approve.error) : null}
        title={approving?.kind === "fx" ? "Approve FX rate" : "Approve pricing rule"}
        confirmLabel="Approve"
        message={
          approving?.kind === "fx" ? (
            <>
              Approve <strong className="text-ink">{approving.row.currency}</strong> at{" "}
              <strong className="text-ink">{rate(approving.row.rate_per_usd)}</strong> per USD (buffer {pct(approving.row.buffer_pct)}), as of{" "}
              <DateText value={approving.row.as_of} />, source {approving.row.source}? Quotes priced from then on use it.
            </>
          ) : approving?.kind === "rule" ? (
            <>
              Approve <strong className="text-ink">{CHANNEL_LABEL[approving.row.channel] ?? approving.row.channel}</strong>: target{" "}
              {pct(approving.row.target_margin)}, floor {pct(approving.row.floor_margin)}, peak +{pct(approving.row.peak_uplift_pts).replace("%", "")} pts,
              card fee {pct(approving.row.card_fee_pct)}, valid from <DateText value={approving.row.valid_from} />? Quotes priced on or after that day use
              it.
            </>
          ) : null
        }
      />
      {drawer === "rule" && <NewRuleDrawer onClose={() => setDrawer(null)} />}
      {drawer === "fx" && <NewFxRateDrawer onClose={() => setDrawer(null)} />}
    </PageFrame>
  );
}
