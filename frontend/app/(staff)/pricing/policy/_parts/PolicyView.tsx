"use client";
// app/(staff)/pricing/policy/_parts/PolicyView.tsx — Pricing → Policy (AA-1091, bk-050, root ADR 0005b decision 15).
// Every pricing policy parameter with the value in force, its open change (draft or pending) and its scheduled change,
// one row each. The tech lead rule (S278): every business policy value is adjustable here, never fixed in code.
// "Change" opens a drawer (new value, effective day, required reason, submit now or save a draft); a pending change
// shows Approve / Reject to a second person of the parameter's approver group (pricing lead or finance), the maker sees
// Submit / Withdraw. The buttons follow the API's per-version can_* answers; the API decides (403 own_row /
// approver_group, 409 effective_date_passed / open_version). "History" lists every version with who made, approved or
// rejected it. The other policy answers that already are staff data (margins, FX, cancellation fees, agency tiers,
// deposit terms) are linked under the table. Own language switch (English default, Vietnamese; messages.ts).
// AA-1096 (S279, bk-056): every Booking threshold and policy value is a parameter here (holds, the on-request desk,
// traveller ages, departures, operations reminders, agency invoice reminders, rounding, retention windows), grouped in
// sections; a parameter no code reads yet carries a "Not enforced yet" badge with what is missing. A second tab edits
// the B2B cancellation fee schedules (same maker-checker, CancellationFees.tsx).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { History, Pencil } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  formatValue,
  policyParamsApi,
  textToValue,
  valueToText,
  type ParamVersion,
  SECTIONS,
  type PolicyParam,
  type VersionStatus,
} from "@/lib/api/policyParams";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  DataTable,
  DateField,
  Drawer,
  Field,
  FormDrawer,
  I18nProvider,
  Input,
  KpiTile,
  LangSwitch,
  LinkButton,
  PageFrame,
  Pill,
  Section,
  Select,
  Textarea,
  langStore,
  useCan,
  useT,
  useToast,
  useUrlFilters,
  type BookingTone,
  type ColumnDef,
} from "@/ui";
import { SavedTabs } from "../../../_parts/SavedTabs";
import { CancellationFees } from "./CancellationFees";
import { M, type MessageKey } from "./messages";

const STATUS_TONE: Record<VersionStatus, BookingTone> = {
  draft: "neutral",
  pending: "warning",
  in_force: "success",
  scheduled: "info",
  superseded: "neutral",
  rejected: "danger",
  withdrawn: "neutral",
};

const RELATED: { key: MessageKey; href: string }[] = [
  { key: "relMargins", href: "/pricing/rules" },
  { key: "relFx", href: "/pricing/rules?tab=fx" },
  { key: "relCancellation", href: "/pricing/policy?tab=cancellation" },
  { key: "relTiers", href: "/pricing/agency-tiers" },
  { key: "relTerms", href: "/agency-billing" },
];

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** The translated pieces a value needs: unit label, yes / no, none, others. */
function useShow() {
  const t = useT(M);
  return (p: Pick<PolicyParam, "type" | "unit">, value: ParamVersion["value"] | null | undefined) => {
    const unit = p.unit ? ((t(`unit_${p.unit}` as MessageKey) as string) ?? p.unit) : null;
    return formatValue(value, p.type, unit, {
      yes: t("yes") as string,
      no: t("no") as string,
      none: t("none") as string,
      others: t("others") as string,
    });
  };
}

function useLabel() {
  const t = useT(M);
  return (p: PolicyParam) => {
    const key = `label_${p.key}` as MessageKey;
    return key in M.en ? (t(key) as string) : p.label;
  };
}

function person(t: ReturnType<typeof useT<typeof M.en>>, p: ParamVersion["created_by"]): string {
  if (!p) return "—";
  if (p.id.startsWith("seed:")) return t("seedMaker") as string;
  return p.name ?? p.id;
}

function errorText(t: ReturnType<typeof useT<typeof M.en>>, err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "own_row") return t("errOwnRow") as string;
    if (err.code === "approver_group") return t("errGroup") as string;
    if (err.code === "effective_date_passed") return t("errPassed") as string;
    if (err.code === "open_version") return t("errOpen") as string;
    if (err.code === "no_change") return t("errNoChange") as string;
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

function rangeText(p: PolicyParam): string {
  if (p.choices.length) return p.choices.join(", ");
  if (p.minimum !== null && p.maximum !== null) return `${p.minimum}–${p.maximum}${p.unit === "%" ? " %" : ""}`;
  return "";
}

function ChangeDrawer({ param, onClose }: { param: PolicyParam; onClose: () => void }) {
  const t = useT(M);
  const show = useShow();
  const label = useLabel()(param);
  const qc = useQueryClient();
  const toast = useToast();
  const [text, setText] = useState(valueToText(param.current?.value, param.type));
  const [effective, setEffective] = useState(todayUtc());
  const [reason, setReason] = useState("");
  const [submitNow, setSubmitNow] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => {
      const value = textToValue(text, param.type);
      if (value === null) throw new Error(t("invalidValue") as string);
      return policyParamsApi.propose({ key: param.key, value, effective_from: effective, reason, submit: submitNow });
    },
    onSuccess: (v) => {
      qc.invalidateQueries({ queryKey: ["pricing", "policy"] });
      toast.success(t("saved", t(`status_${v.status}` as MessageKey) as string) as string);
      onClose();
    },
    onError: (err) => setError(errorText(t, err)),
  });
  const hint = param.type === "currency_map" ? t("fieldValueMap") : param.type === "amount_map"
    ? `${t("fieldValueAmounts", param.choices.join(", "))} ${rangeText({ ...param, choices: [] }) ? t("fieldValueHint", rangeText({ ...param, choices: [] })) : ""}`
    : param.type === "month_days" || param.type === "codes" || param.type === "integers"
    ? `${t("fieldValueList")} ${rangeText(param) ? t("fieldValueHint", rangeText(param)) : ""}`
    : rangeText(param) ? t("fieldValueHint", rangeText(param)) : undefined;
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("drawerTitle", label) as string}
      onSubmit={() => {
        setError(null);
        save.mutate();
      }}
      submitLabel={t("save") as string}
      busy={save.isPending}
      dirty={!!reason.trim()}
      error={error}
      width={520}
    >
      <div className="flex flex-col gap-4" data-testid="policy-change-drawer">
        <p className="text-[13px] text-muted">{param.help}</p>
        <p className="text-[13px] text-body">{t("currentIs", show(param, param.current?.value))}</p>
        <Field label={t("fieldValue")} required hint={hint as string | undefined}>
          {param.type === "boolean" ? (
            <Select
              value={text}
              onChange={(e) => setText(e.target.value)}
              options={[
                { value: "true", label: t("yes") },
                { value: "false", label: t("no") },
              ]}
            />
          ) : (
            <Input value={text} onChange={(e) => setText(e.target.value)} data-testid="policy-value" />
          )}
        </Field>
        <Field label={t("fieldEffective")} required hint={t("fieldEffectiveHint") as string}>
          <DateField value={effective} onChange={setEffective} min={todayUtc()} />
        </Field>
        <Field label={t("fieldReason")} required hint={t("fieldReasonHint") as string}>
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} rows={3} />
        </Field>
        <Checkbox checked={submitNow} onChange={(e) => setSubmitNow(e.target.checked)} label={t("fieldSubmit")} />
      </div>
    </FormDrawer>
  );
}

function HistoryDrawer({ param, onClose }: { param: PolicyParam; onClose: () => void }) {
  const t = useT(M);
  const show = useShow();
  const label = useLabel()(param);
  return (
    <Drawer open onClose={onClose} title={t("historyTitle", label)} width={520}>
      <ol className="flex flex-col gap-3" data-testid="policy-history">
        {param.history.map((v) => (
          <li key={v.id} className="rounded-lg border border-line px-3 py-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-ink">{t("version", v.version_no)}</span>
              <Pill tone={STATUS_TONE[v.status]}>{t(`status_${v.status}` as MessageKey) as string}</Pill>
              <span className="text-[13px] text-body">{show(param, v.value)}</span>
              <span className="text-xs text-muted">{t("since", v.effective_from)}</span>
            </div>
            <p className="mt-1 break-words text-[13px] text-body">{v.reason}</p>
            <p className="mt-1 text-xs text-muted">
              {t("histMadeBy", person(t, v.created_by))}
              {v.approved_by ? ` · ${t("histApprovedBy", person(t, v.approved_by))}` : ""}
              {v.rejected_by ? ` · ${t("histRejectedBy", person(t, v.rejected_by), v.reject_reason ?? "")}` : ""}
            </p>
          </li>
        ))}
      </ol>
    </Drawer>
  );
}

type Deciding = { kind: "approve" | "reject"; param: PolicyParam; version: ParamVersion } | null;

function Policy() {
  const t = useT(M);
  const { get } = useUrlFilters();
  const tab = get("tab") === "cancellation" ? "cancellation" : "parameters";
  const show = useShow();
  const labelOf = useLabel();
  const qc = useQueryClient();
  const toast = useToast();
  const canWrite = useCan("pricing_policy.write");
  const [changing, setChanging] = useState<PolicyParam | null>(null);
  const [history, setHistory] = useState<PolicyParam | null>(null);
  const [deciding, setDeciding] = useState<Deciding>(null);
  const listing = useQuery({ queryKey: ["pricing", "policy"], queryFn: () => policyParamsApi.list() });
  const items = useMemo(() => listing.data?.items ?? [], [listing.data]);

  const step = useMutation({
    mutationFn: ({ kind, id, reason }: { kind: "approve" | "reject" | "submit" | "withdraw"; id: string; reason?: string }) =>
      kind === "approve"
        ? policyParamsApi.approve(id)
        : kind === "reject"
          ? policyParamsApi.reject(id, reason ?? "")
          : kind === "submit"
            ? policyParamsApi.submit(id)
            : policyParamsApi.withdraw(id),
    onSuccess: (_, { kind }) => {
      qc.invalidateQueries({ queryKey: ["pricing", "policy"] });
      const done = { approve: "approved", reject: "rejected", submit: "submitted", withdraw: "withdrawn" } as const;
      toast.success(t(done[kind]) as string);
      setDeciding(null);
    },
    onError: (err) => toast.error(errorText(t, err)),
  });

  function changeCell(p: PolicyParam) {
    const open = p.open;
    const scheduled = p.scheduled[0];
    if (!open && !scheduled) return <span className="text-[13px] text-muted">—</span>;
    return (
      <span className="flex flex-col items-start gap-1.5" onClick={(e) => e.stopPropagation()}>
        {scheduled && (
          <span className="break-words text-[13px] text-body">
            {t("scheduledChange", show(p, scheduled.value), scheduled.effective_from)}
          </span>
        )}
        {open && (
          <>
            <Pill tone={STATUS_TONE[open.status]}>{t(`status_${open.status}` as MessageKey) as string}</Pill>
            <span className="break-words text-[13px] text-body">
              {t("openChange", show(p, open.value), open.effective_from)}
            </span>
            <span className="max-w-[320px] break-words text-xs text-muted">{open.reason}</span>
            <span className="flex flex-wrap gap-2">
              {open.can_approve && (
                <Button size="sm" variant="primary" onClick={() => setDeciding({ kind: "approve", param: p, version: open })}>
                  {t("approve")}
                </Button>
              )}
              {open.can_approve && (
                <Button size="sm" variant="secondary" onClick={() => setDeciding({ kind: "reject", param: p, version: open })}>
                  {t("reject")}
                </Button>
              )}
              {open.can_submit && (
                <Button size="sm" variant="secondary" disabled={step.isPending} onClick={() => step.mutate({ kind: "submit", id: open.id })}>
                  {t("submit")}
                </Button>
              )}
              {open.can_withdraw && (
                <Button size="sm" variant="ghost" disabled={step.isPending} onClick={() => step.mutate({ kind: "withdraw", id: open.id })}>
                  {t("withdraw")}
                </Button>
              )}
              {open.status === "pending" && !open.can_approve && !open.can_withdraw && (
                <span className="text-xs text-muted">
                  {p.can_approve_group ? t("needsSecond") : t("needsGroup", approverLabel(p))}
                </span>
              )}
            </span>
          </>
        )}
      </span>
    );
  }

  function approverLabel(p: PolicyParam): string {
    const key = p.approver === "finance" ? "approverFinance" : p.approver === "admin" ? "approverAdmin" : "approverPricing";
    return t(key) as string;
  }

  const columns: ColumnDef<PolicyParam, unknown>[] = [
    {
      id: "param",
      header: t("colParam"),
      accessorFn: (p) => labelOf(p),
      cell: ({ row }) => {
        const p = row.original;
        return (
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className="font-medium text-ink">{labelOf(p)}</span>
            <span className="max-w-[360px] break-words text-xs text-muted">{p.help}</span>
            <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
              <code>{p.key}</code>
              <span data-testid={`policy-enforced-${p.key}`}>
                <Pill tone={p.in_use ? "success" : "warning"} dot={false} title={p.not_enforced_reason ?? undefined}>
                  {p.in_use ? t("inUse") : t("notEnforced")}
                </Pill>
              </span>
            </span>
            {!p.in_use && p.not_enforced_reason && (
              <span className="max-w-[360px] break-words text-xs text-muted">{t("notEnforcedWhy", p.not_enforced_reason)}</span>
            )}
          </span>
        );
      },
    },
    {
      id: "value",
      header: t("colValue"),
      accessorFn: (p) => valueToText(p.current?.value, p.type),
      cell: ({ row }) => {
        const p = row.original;
        if (!p.current) return <span className="text-[13px] text-danger">{t("noValue")}</span>;
        return (
          <span className="flex flex-col whitespace-nowrap" data-testid={`policy-value-${p.key}`}>
            <span className="font-semibold text-ink">{show(p, p.current.value)}</span>
            <span className="text-xs text-muted">
              {t("version", p.current.version_no)} · {t("since", p.current.effective_from)}
            </span>
          </span>
        );
      },
    },
    { id: "change", header: t("colStatus"), cell: ({ row }) => changeCell(row.original) },
    {
      id: "approver",
      header: t("colApprover"),
      accessorFn: (p) => p.approver,
      cell: ({ row }) => <span className="whitespace-nowrap text-[13px] text-body">{approverLabel(row.original)}</span>,
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <span className="flex flex-wrap items-center gap-3 text-[13px]" onClick={(e) => e.stopPropagation()}>
          {canWrite && (
            <Button size="sm" variant="secondary" onClick={() => setChanging(row.original)} disabled={!!row.original.open}>
              <Pencil size={13} /> {t("change")}
            </Button>
          )}
          <LinkButton onClick={() => setHistory(row.original)}>
            <History size={13} /> {t("history")}
          </LinkButton>
        </span>
      ),
    },
  ];

  const pending = items.filter((p) => p.open?.status === "pending").length;
  const scheduled = items.reduce((n, p) => n + p.scheduled.length, 0);
  const inUse = items.filter((p) => p.in_use).length;
  const sections = SECTIONS.map((s) => ({ section: s, params: items.filter((p) => p.section === s) })).filter(
    (g) => g.params.length > 0,
  );
  const d = deciding;
  return (
    <PageFrame title={t("title")} description={t("description")} actions={<LangSwitch ariaLabel={t("language")} />}>
      <SavedTabs
        ariaLabel={t("tabsLabel") as string}
        tabs={[
          { key: "parameters", label: t("tabParameters") as string },
          { key: "cancellation", label: t("tabCancellation") as string },
        ]}
      />
      {tab === "cancellation" ? (
        <CancellationFees />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <KpiTile label={t("kpiParams")} value={listing.data ? items.length : undefined} />
            <KpiTile
              label={t("kpiPending")}
              value={listing.data ? pending : undefined}
              hint={pending ? t("kpiPendingHint") : undefined}
              hintTone="warning"
            />
            <KpiTile label={t("kpiScheduled")} value={listing.data ? scheduled : undefined} />
            <KpiTile label={t("kpiInUse")} value={listing.data ? inUse : undefined} />
          </div>
          {listing.isLoading || listing.isError || sections.length === 0 ? (
            <Section title={t("sectionParams")}>
              <DataTable<PolicyParam>
                tableId="pricing-policy"
                data={[]}
                columns={columns}
                getRowId={(p) => p.key}
                loading={listing.isLoading}
                error={listing.isError ? (listing.error as Error).message : null}
                onRetry={() => listing.refetch()}
                emptyTitle={t("emptyTitle")}
                emptyDescription={t("emptyHint")}
                tableMinWidth={960}
              />
            </Section>
          ) : (
            sections.map((g) => (
              <Section key={g.section} title={t(`section_${g.section}` as MessageKey) as string} description={t(`sectionHint_${g.section}` as MessageKey) as string}>
                <div data-testid={`policy-section-${g.section}`}>
                  <DataTable<PolicyParam>
                    tableId={`pricing-policy-${g.section}`}
                    data={g.params}
                    columns={columns}
                    getRowId={(p) => p.key}
                    emptyTitle={t("emptyTitle")}
                    emptyDescription={t("emptyHint")}
                    tableMinWidth={960}
                  />
                </div>
              </Section>
            ))
          )}
          <Section title={t("sectionRelated")} description={t("relatedHint")}>
            <ul className="flex flex-col gap-2 text-[13px]" data-testid="policy-related">
              {RELATED.map((r) => (
                <li key={r.key} className="flex flex-wrap items-center gap-2">
                  <span className="text-body">{t(r.key) as string}</span>
                  <Link href={r.href} className="font-semibold text-accent-deep hover:underline">
                    {t("open")}
                  </Link>
                </li>
              ))}
            </ul>
          </Section>
        </>
      )}
      {changing && <ChangeDrawer param={changing} onClose={() => setChanging(null)} />}
      {history && <HistoryDrawer param={history} onClose={() => setHistory(null)} />}
      <ConfirmDialog
        open={!!d}
        onClose={() => setDeciding(null)}
        busy={step.isPending}
        title={(d?.kind === "reject" ? t("rejectTitle") : t("approveTitle")) as string}
        body={
          d
            ? d.kind === "reject"
              ? t("rejectBody", labelOf(d.param), show(d.param, d.version.value))
              : t(
                  "approveBody",
                  labelOf(d.param),
                  show(d.param, d.version.diff?.from ?? d.param.current?.value),
                  show(d.param, d.version.value),
                  d.version.effective_from,
                )
            : null
        }
        confirmLabel={(d?.kind === "reject" ? t("reject") : t("approve")) as string}
        destructive={d?.kind === "reject"}
        reason={d?.kind === "reject" ? { label: t("rejectReason") as string, required: true, maxLength: 300 } : undefined}
        onConfirm={(reason) => d && step.mutate({ kind: d.kind, id: d.version.id, reason })}
      />
    </PageFrame>
  );
}

/** The page in its own language (English by default; the switch keeps the choice in the `bk_lang` cookie). */
export function PolicyView() {
  return (
    <I18nProvider store={langStore}>
      <Policy />
    </I18nProvider>
  );
}
