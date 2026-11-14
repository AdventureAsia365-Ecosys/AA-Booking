"use client";
// app/(staff)/pricing/policy/_parts/CancellationFees.tsx — Pricing → Policy, tab "Cancellation fees" (AA-1096, S279
// thresholds program). The B2B cancellation fee schedules (AA-974, bk-021; the FIT scale of bk-051): the B2B default
// and any tour's own schedule, each a row valid from a day. A change is a new schedule, pending until a second person
// with pricing.write approves it (403 own_row for its maker); the API decides, the page shows the buttons it allows
// (can_approve). Text from the page's catalogue (messages.ts, English and Vietnamese).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  pricingPolicyApi,
  type CancellationPolicyRow,
  type CancellationTier,
  type PolicyStatus,
} from "@/lib/api/pricingPolicy";
import {
  Button,
  ConfirmDialog,
  DataTable,
  DateField,
  Field,
  FormDrawer,
  Input,
  LinkButton,
  Pill,
  Section,
  Textarea,
  useCan,
  useT,
  useToast,
  type BookingTone,
  type ColumnDef,
} from "@/ui";
import { CloseRowButtons, ClosedNote } from "../../_shared/CloseRow";
import { M, type MessageKey } from "./messages";
import { errorText } from "./policyUi";

const TONE: Record<PolicyStatus, BookingTone> = {
  pending: "warning",
  in_force: "success",
  scheduled: "info",
  superseded: "neutral",
  rejected: "danger",
  withdrawn: "neutral",
};

type TierText = { min_days: string; fee_pct: string };

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** "31+ days: 0 % · 22+ days: 10 % · …" (min_days descending, as the API sends them). */
export function tiersText(tiers: CancellationTier[], daysWord: string): string {
  return tiers.map((tier) => `${tier.min_days}+ ${daysWord}: ${tier.fee_pct} %`).join(" · ");
}

/** The editor rows as the API body, or null when a row is not a whole day count and a percentage 0-100. */
export function parseTiers(rows: TierText[]): { min_days: number; fee_pct: string }[] | null {
  if (!rows.length) return null;
  const out = [];
  for (const row of rows) {
    const days = row.min_days.trim();
    const pct = row.fee_pct.trim();
    if (!/^\d+$/.test(days) || !/^\d+(\.\d{1,2})?$/.test(pct) || Number(pct) > 100) return null;
    out.push({ min_days: Number(days), fee_pct: pct });
  }
  return out;
}

function NewScheduleDrawer({ copyOf, onClose }: { copyOf: CancellationPolicyRow | null; onClose: () => void }) {
  const t = useT(M);
  const qc = useQueryClient();
  const toast = useToast();
  const [title, setTitle] = useState(copyOf?.title ?? "");
  const [sourceId, setSourceId] = useState(copyOf?.source_id ?? "");
  const [validFrom, setValidFrom] = useState(todayUtc());
  const [rows, setRows] = useState<TierText[]>(
    copyOf?.tiers.map((tier) => ({ min_days: String(tier.min_days), fee_pct: tier.fee_pct })) ?? [
      { min_days: "0", fee_pct: "100" },
    ],
  );
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => {
      const tiers = parseTiers(rows);
      if (!tiers) throw new Error(t("cxInvalidTiers") as string);
      return pricingPolicyApi.createCancellationPolicy({
        title: title.trim(),
        source_id: sourceId.trim() || undefined,
        valid_from: validFrom,
        tiers,
        note: note.trim() || undefined,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pricing", "cancellation-policies"] });
      toast.success(t("cxSaved") as string);
      onClose();
    },
    // S280: every API error in the page's language (trip_plan_not_found, effective_in_past, ...).
    onError: (err) => setError(errorText(t, err)),
  });
  const setRow = (i: number, patch: Partial<TierText>) =>
    setRows((all) => all.map((row, j) => (j === i ? { ...row, ...patch } : row)));
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("cxNewTitle") as string}
      onSubmit={() => {
        setError(null);
        save.mutate();
      }}
      submitLabel={t("save") as string}
      busy={save.isPending}
      dirty={!!title.trim()}
      error={error}
      width={560}
    >
      <div className="flex flex-col gap-4" data-testid="cancellation-new-drawer">
        <p className="text-[13px] text-muted">{t("cxNewHint")}</p>
        <Field label={t("cxFieldTitle")} required>
          <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} />
        </Field>
        <Field label={t("cxFieldScope")} hint={t("cxFieldScopeHint") as string}>
          <Input value={sourceId} onChange={(e) => setSourceId(e.target.value)} maxLength={128} />
        </Field>
        <Field label={t("fieldEffective")} required hint={t("cxFieldFromHint") as string}>
          <DateField value={validFrom} onChange={setValidFrom} min={todayUtc()} />
        </Field>
        <Field label={t("cxFieldTiers")} required hint={t("cxFieldTiersHint") as string}>
          <div className="flex flex-col gap-2" data-testid="cancellation-tiers">
            {rows.map((row, i) => (
              <div key={i} className="flex flex-wrap items-center gap-2">
                <Input
                  aria-label={t("cxDaysBefore") as string}
                  value={row.min_days}
                  onChange={(e) => setRow(i, { min_days: e.target.value })}
                  inputMode="numeric"
                  className="w-24"
                />
                <span className="text-[13px] text-muted">{t("cxDaysOrMore")}</span>
                <Input
                  aria-label={t("cxFeePct") as string}
                  value={row.fee_pct}
                  onChange={(e) => setRow(i, { fee_pct: e.target.value })}
                  inputMode="decimal"
                  className="w-24"
                />
                <span className="text-[13px] text-muted">%</span>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={t("cxRemoveTier") as string}
                  disabled={rows.length === 1}
                  onClick={() => setRows((all) => all.filter((_, j) => j !== i))}
                >
                  <Trash2 size={13} />
                </Button>
              </div>
            ))}
            <span>
              <LinkButton onClick={() => setRows((all) => [...all, { min_days: "", fee_pct: "" }])}>
                <Plus size={13} /> {t("cxAddTier")}
              </LinkButton>
            </span>
          </div>
        </Field>
        <Field label={t("cxFieldNote")}>
          <Textarea value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} rows={2} />
        </Field>
      </div>
    </FormDrawer>
  );
}

export function CancellationFees() {
  const t = useT(M);
  const qc = useQueryClient();
  const toast = useToast();
  const canWrite = useCan("pricing.write");
  const [creating, setCreating] = useState<{ copyOf: CancellationPolicyRow | null } | null>(null);
  const [approving, setApproving] = useState<CancellationPolicyRow | null>(null);
  const list = useQuery({
    queryKey: ["pricing", "cancellation-policies"],
    queryFn: () => pricingPolicyApi.cancellationPolicies(),
  });
  const approve = useMutation({
    mutationFn: (id: string) => pricingPolicyApi.approveCancellationPolicy(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pricing", "cancellation-policies"] });
      toast.success(t("approved") as string);
      setApproving(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "already_approved") qc.invalidateQueries({ queryKey: ["pricing", "cancellation-policies"] });
      toast.error(errorText(t, err, { step: "approve" }));
    },
  });
  const days = t("unit_days") as string;
  const person = (p: CancellationPolicyRow["created_by"]) =>
    !p ? "—" : p.id.startsWith("seed:") ? (t("seedMaker") as string) : (p.name ?? p.id);
  const columns: ColumnDef<CancellationPolicyRow, unknown>[] = [
    {
      id: "schedule",
      header: t("cxColSchedule"),
      accessorFn: (r) => r.title,
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className="font-medium text-ink">{row.original.title}</span>
          <span className="text-xs text-muted">
            {row.original.source_id ? t("cxScopeTour", row.original.source_id) : t("cxScopeDefault")}
          </span>
        </span>
      ),
    },
    {
      id: "tiers",
      header: t("cxColTiers"),
      cell: ({ row }) => (
        <span className="block max-w-[420px] break-words text-[13px] text-body" data-testid={`cancellation-tiers-${row.original.id}`}>
          {tiersText(row.original.tiers, days)}
        </span>
      ),
    },
    {
      id: "status",
      header: t("colStatus"),
      accessorFn: (r) => r.status,
      cell: ({ row }) => (
        <span className="flex flex-col items-start gap-1">
          <Pill tone={TONE[row.original.status]}>{t(`status_${row.original.status}` as MessageKey) as string}</Pill>
          <span className="whitespace-nowrap text-xs text-muted">{t("since", row.original.valid_from)}</span>
        </span>
      ),
    },
    {
      id: "people",
      header: t("cxColPeople"),
      cell: ({ row }) => (
        <span className="text-xs text-muted">
          {t("histMadeBy", person(row.original.created_by))}
          {row.original.approved_by ? ` · ${t("histApprovedBy", person(row.original.approved_by))}` : ""}
          <ClosedNote row={row.original} />
        </span>
      ),
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <span className="flex flex-wrap items-center gap-2" onClick={(e) => e.stopPropagation()}>
          {row.original.can_approve && (
            <Button size="sm" variant="primary" onClick={() => setApproving(row.original)}>
              {t("approve")}
            </Button>
          )}
          {row.original.status === "pending" && row.original.day_passed && (
            <span className="text-xs text-muted">{t("cxDayPassed")}</span>
          )}
          <CloseRowButtons kind="cancellation-policies" row={row.original} what={`${row.original.title} (${row.original.valid_from})`} />
          {canWrite && (
            <LinkButton onClick={() => setCreating({ copyOf: row.original })}>{t("cxCopy")}</LinkButton>
          )}
        </span>
      ),
    },
  ];
  return (
    <Section
      title={t("cxTitle")}
      description={t("cxHint")}
      actions={
        canWrite && (
          <Button size="sm" variant="primary" onClick={() => setCreating({ copyOf: null })}>
            <Plus size={14} /> {t("cxNew")}
          </Button>
        )
      }
    >
      <div data-testid="cancellation-fees">
        <DataTable<CancellationPolicyRow>
          tableId="pricing-cancellation-policies"
          data={list.data?.items ?? []}
          columns={columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          error={list.isError ? errorText(t, list.error) : null}
          onRetry={() => list.refetch()}
          emptyTitle={t("cxEmptyTitle")}
          emptyDescription={t("cxEmptyHint")}
          tableMinWidth={900}
        />
      </div>
      {creating && <NewScheduleDrawer copyOf={creating.copyOf} onClose={() => setCreating(null)} />}
      <ConfirmDialog
        open={!!approving}
        onClose={() => setApproving(null)}
        busy={approve.isPending}
        title={t("cxApproveTitle") as string}
        body={approving ? t("cxApproveBody", approving.title, tiersText(approving.tiers, days), approving.valid_from) : null}
        confirmLabel={t("approve") as string}
        onConfirm={() => approving && approve.mutate(approving.id)}
      />
    </Section>
  );
}
