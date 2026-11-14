"use client";
// app/(staff)/pricing/policy/_parts/PolicyView.tsx — Pricing → Policy (AA-1091, bk-050, root ADR 0005b decision 15).
// Every pricing policy parameter with the value in force, its open change (draft or pending) and its scheduled change,
// one row each. The tech lead rule (S278): every business policy value is adjustable here, never fixed in code.
// "Change" opens a drawer (ChangeDrawer.tsx: new value, effective day, required reason, submit now or save a draft); a
// pending change shows Approve / Reject to a second person of the parameter's approver group (pricing lead, finance or
// the administrator), the maker sees Submit / Withdraw. The buttons follow the API's per-version can_* answers; the API
// decides (403 own_row / approver_group, 409 effective_date_passed / open_version). "History" lists every version with
// who made, approved or rejected it. The other policy answers that already are staff data (margins, FX, cancellation
// fees, agency tiers, deposit terms) are linked under the table. Own language switch (English default, Vietnamese;
// messages.ts).
// AA-1096 (S279, bk-056): every Booking threshold and policy value is a parameter here (holds, the on-request desk,
// traveller ages, departures, operations reminders, agency invoice reminders, rounding, retention windows), grouped in
// sections; a parameter no code reads yet carries a "Not enforced yet" badge with what is missing. A second tab edits
// the B2B cancellation fee schedules (same maker-checker, CancellationFees.tsx).
// AA-1096 l23: every parameter explains itself (PolicyGuide.tsx): the short "what" under the label, an Explain toggle
// with the whole guide (EN / VI), the same guide at the top of the change drawer; a decimal takes a comma or a point.
// S280 (settings UI audit): one page-wide search (label in both languages, key, guide, help, approver; sections with
// no match hide) instead of a search box per section; every scheduled change is listed in the order it takes effect;
// another maker's draft says who made it and since when, Change says why it is locked, and the approver group can
// discard that draft (with a reason); the "Not enforced yet" reason and every API error are in the page's language.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { History, Pencil } from "lucide-react";
import { policyParamsApi, valueToText, SECTIONS, type ParamVersion, type PolicyParam, type VersionStatus } from "@/lib/api/policyParams";
import { matchesSearch, searchText } from "@/lib/policyEdit";
import {
  Button,
  ConfirmDialog,
  DataTable,
  Drawer,
  FilterBar,
  I18nProvider,
  KpiTile,
  LangSwitch,
  LinkButton,
  PageFrame,
  Pill,
  Section,
  langStore,
  useCan,
  useFormat,
  useT,
  useToast,
  useUrlFilters,
  type BookingTone,
  type ColumnDef,
} from "@/ui";
import { SavedTabs } from "../../../_parts/SavedTabs";
import { CancellationFees } from "./CancellationFees";
import { ChangeDrawer } from "./ChangeDrawer";
import { M, type MessageKey } from "./messages";
import { ExplainToggle, GuideSummary } from "./PolicyGuide";
import { approverLabel, errorText, labelOf, person, useNotEnforcedReason, useShow, type T } from "./policyUi";

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
  { key: "relTerms", href: "/agency-billing?tab=settings" },
];

const LIST_TYPES: ReadonlySet<PolicyParam["type"]> = new Set(["month_days", "codes", "currency_map", "integers", "amount_map"]);

type StepKind = "approve" | "reject" | "submit" | "withdraw" | "discard";
type Deciding = { kind: "approve" | "reject" | "discard"; param: PolicyParam; version: ParamVersion } | null;
type StepFn = (kind: StepKind, param: PolicyParam, version: ParamVersion) => void;

function HistoryDrawer({ param, onClose }: { param: PolicyParam; onClose: () => void }) {
  const t = useT(M);
  const show = useShow();
  return (
    <Drawer open onClose={onClose} title={t("historyTitle", labelOf(t, param))} width={520}>
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

/** Why the pending or draft change of a row cannot be decided by this person (or who made the draft). */
function OpenNote({ p, open, t }: { p: PolicyParam; open: ParamVersion; t: T }) {
  const { formatDate } = useFormat();
  if (open.status === "draft" && !open.can_withdraw) {
    return <span className="text-xs text-muted">{t("draftBy", person(t, open.created_by), formatDate(open.created_at))}</span>;
  }
  if (open.status === "pending" && !open.can_approve && !open.can_withdraw) {
    return <span className="text-xs text-muted">{p.can_approve_group ? t("needsSecond") : t("needsGroup", approverLabel(t, p))}</span>;
  }
  return null;
}

function OpenButtons({ p, open, busy, onStep }: { p: PolicyParam; open: ParamVersion; busy: boolean; onStep: StepFn }) {
  const t = useT(M);
  return (
    <span className="flex flex-wrap gap-2">
      {open.can_approve && (
        <Button size="sm" variant="primary" onClick={() => onStep("approve", p, open)}>{t("approve")}</Button>
      )}
      {open.can_approve && (
        <Button size="sm" variant="secondary" onClick={() => onStep("reject", p, open)}>{t("reject")}</Button>
      )}
      {open.can_submit && (
        <Button size="sm" variant="secondary" disabled={busy} onClick={() => onStep("submit", p, open)}>{t("submit")}</Button>
      )}
      {open.can_withdraw && (
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => onStep("withdraw", p, open)}>{t("withdraw")}</Button>
      )}
      {open.can_discard && (
        <Button size="sm" variant="ghost" onClick={() => onStep("discard", p, open)}>{t("discard")}</Button>
      )}
      <OpenNote p={p} open={open} t={t} />
    </span>
  );
}

/** The Change column: every scheduled change in the order it takes effect, then the open change and its buttons. */
function ChangeCell({ p, busy, onStep }: { p: PolicyParam; busy: boolean; onStep: StepFn }) {
  const t = useT(M);
  const show = useShow();
  const open = p.open;
  if (!open && !p.scheduled.length) return <span className="text-[13px] text-muted">—</span>;
  return (
    <span className="flex flex-col items-start gap-1.5" onClick={(e) => e.stopPropagation()}>
      {p.scheduled.map((s) => (
        <span key={s.id} className="break-words text-[13px] text-body">
          {t("scheduledChange", show(p, s.value), s.effective_from)}
        </span>
      ))}
      {open && (
        <>
          <Pill tone={STATUS_TONE[open.status]}>{t(`status_${open.status}` as MessageKey) as string}</Pill>
          <span className="break-words text-[13px] text-body">{t("openChange", show(p, open.value), open.effective_from)}</span>
          <span className="max-w-[320px] break-words text-xs text-muted">{open.reason}</span>
          <OpenButtons p={p} open={open} busy={busy} onStep={onStep} />
        </>
      )}
    </span>
  );
}

function ParamCell({ p }: { p: PolicyParam }) {
  const t = useT(M);
  const reason = useNotEnforcedReason()(p);
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <span className="font-medium text-ink">{labelOf(t, p)}</span>
      <GuideSummary param={p} />
      <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
        <code className="break-all">{p.key}</code>
        <span data-testid={`policy-enforced-${p.key}`}>
          <Pill tone={p.in_use ? "success" : "warning"} dot={false} title={reason ?? undefined}>
            {p.in_use ? t("inUse") : t("notEnforced")}
          </Pill>
        </span>
      </span>
      {!p.in_use && reason && <span className="max-w-[360px] break-words text-xs text-muted">{t("notEnforcedWhy", reason)}</span>}
      <ExplainToggle param={p} />
    </span>
  );
}

function ValueCell({ p }: { p: PolicyParam }) {
  const t = useT(M);
  const show = useShow();
  if (!p.current) return <span className="text-[13px] text-danger">{t("noValue")}</span>;
  return (
    <span className="flex flex-col whitespace-nowrap" data-testid={`policy-value-${p.key}`}>
      {/* A list or a map wraps (a phone card is narrower than "AUD 5, EUR 5, GBP 5, SGD 5, USD 5"). */}
      <span className={`font-semibold text-ink ${LIST_TYPES.has(p.type) ? "whitespace-normal break-words" : ""}`}>
        {show(p, p.current.value)}
      </span>
      <span className="text-xs text-muted">
        {t("version", p.current.version_no)} · {t("since", p.current.effective_from)}
      </span>
    </span>
  );
}

function ActionsCell({ p, canWrite, onChange, onHistory }: { p: PolicyParam; canWrite: boolean; onChange: () => void; onHistory: () => void }) {
  const t = useT(M);
  // Locked while a change is open (the API answers 409 open_version); the tooltip says why.
  const locked = p.open ? (t("lockedOpen", t(`status_${p.open.status}` as MessageKey) as string) as string) : undefined;
  return (
    <span className="flex flex-wrap items-center gap-3 text-[13px]" onClick={(e) => e.stopPropagation()}>
      {canWrite && (
        <span title={locked} data-testid={`policy-change-${p.key}`}>
          <Button size="sm" variant="secondary" onClick={onChange} disabled={!!p.open} title={locked}>
            <Pencil size={13} /> {t("change")}
          </Button>
        </span>
      )}
      <LinkButton onClick={onHistory}>
        <History size={13} /> {t("history")}
      </LinkButton>
    </span>
  );
}

/** The words a page-wide search looks in for each parameter, in both languages. */
function useSearchIndex(items: PolicyParam[]): Map<string, string> {
  return useMemo(() => {
    const en = (k: MessageKey) => String(M.en[k]);
    const vi = (k: MessageKey) => String(M.vi[k]);
    const approver = { finance: "approverFinance", admin: "approverAdmin", pricing: "approverPricing" } as const;
    return new Map(
      items.map((p) => {
        const label = `label_${p.key}` as MessageKey;
        const extra = label in M.en ? [en(label), vi(label)] : [];
        const who = approver[p.approver] ?? "approverPricing";
        return [p.key, searchText(p, [...extra, en(who), vi(who), valueToText(p.current?.value, p.type)])];
      }),
    );
  }, [items]);
}

function useSteps(t: T, onDone: () => void) {
  const qc = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: ({ kind, id, reason }: { kind: StepKind; id: string; reason?: string; param: PolicyParam }) => {
      const call = {
        approve: () => policyParamsApi.approve(id),
        reject: () => policyParamsApi.reject(id, reason ?? ""),
        discard: () => policyParamsApi.discard(id, reason ?? ""),
        submit: () => policyParamsApi.submit(id),
        withdraw: () => policyParamsApi.withdraw(id),
      };
      return call[kind]();
    },
    onSuccess: (_, { kind }) => {
      qc.invalidateQueries({ queryKey: ["pricing", "policy"] });
      const done = { approve: "approved", reject: "rejected", discard: "discarded", submit: "submitted", withdraw: "withdrawn" } as const;
      toast.success(t(done[kind]) as string);
      onDone();
    },
    onError: (err, { kind, param }) => toast.error(errorText(t, err, { param, step: kind })),
  });
}

function DecideDialog({ d, busy, onClose, onConfirm }: { d: Deciding; busy: boolean; onClose: () => void; onConfirm: (reason: string) => void }) {
  const t = useT(M);
  const show = useShow();
  const text = (): { title: string; body: string | null; label: string } => {
    if (!d) return { title: "", body: null, label: "" };
    const label = labelOf(t, d.param);
    if (d.kind === "discard") {
      return { title: t("discardTitle") as string, label: t("discard") as string,
        body: t("discardBody", label, show(d.param, d.version.value), person(t, d.version.created_by)) as string };
    }
    if (d.kind === "reject") {
      return { title: t("rejectTitle") as string, label: t("reject") as string, body: t("rejectBody", label, show(d.param, d.version.value)) as string };
    }
    const from = show(d.param, d.version.diff?.from ?? d.param.current?.value);
    return { title: t("approveTitle") as string, label: t("approve") as string,
      body: t("approveBody", label, from, show(d.param, d.version.value), d.version.effective_from) as string };
  };
  const { title, body, label } = text();
  const needsReason = d?.kind === "reject" || d?.kind === "discard";
  return (
    <ConfirmDialog
      open={!!d}
      onClose={onClose}
      busy={busy}
      title={title}
      body={body}
      confirmLabel={label}
      destructive={needsReason}
      reason={needsReason ? { label: t(d?.kind === "discard" ? "discardReason" : "rejectReason") as string, required: true, maxLength: 300 } : undefined}
      onConfirm={onConfirm}
    />
  );
}

function ParamSections({ items, columns, loading, error, onRetry }: {
  items: PolicyParam[];
  columns: ColumnDef<PolicyParam, unknown>[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const t = useT(M);
  const { get } = useUrlFilters();
  const index = useSearchIndex(items);
  const q = get("q");
  const shown = items.filter((p) => matchesSearch(index.get(p.key) ?? "", q));
  const sections = SECTIONS.map((s) => ({ section: s, params: shown.filter((p) => p.section === s) })).filter((g) => g.params.length > 0);
  if (loading || error || sections.length === 0) {
    const none = !loading && !error && items.length > 0;
    return (
      <Section title={t("sectionParams")}>
        <DataTable<PolicyParam>
          tableId="pricing-policy"
          data={[]}
          columns={columns}
          getRowId={(p) => p.key}
          loading={loading}
          error={error}
          onRetry={onRetry}
          searchable={false}
          emptyTitle={none ? t("searchNone") : t("emptyTitle")}
          emptyDescription={none ? undefined : t("emptyHint")}
          tableMinWidth={960}
        />
      </Section>
    );
  }
  return (
    <>
      {sections.map((g) => (
        <Section key={g.section} title={t(`section_${g.section}` as MessageKey) as string} description={t(`sectionHint_${g.section}` as MessageKey) as string}>
          <div data-testid={`policy-section-${g.section}`}>
            <DataTable<PolicyParam>
              tableId={`pricing-policy-${g.section}`}
              data={g.params}
              columns={columns}
              getRowId={(p) => p.key}
              searchable={false}
              emptyTitle={t("emptyTitle")}
              emptyDescription={t("emptyHint")}
              tableMinWidth={960}
            />
          </div>
        </Section>
      ))}
    </>
  );
}

function Kpis({ items, ready }: { items: PolicyParam[]; ready: boolean }) {
  const t = useT(M);
  const pending = items.filter((p) => p.open?.status === "pending").length;
  const scheduled = items.reduce((n, p) => n + p.scheduled.length, 0);
  const inUse = items.filter((p) => p.in_use).length;
  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      <KpiTile label={t("kpiParams")} value={ready ? items.length : undefined} />
      <KpiTile label={t("kpiPending")} value={ready ? pending : undefined} hint={pending ? t("kpiPendingHint") : undefined} hintTone="warning" />
      <KpiTile label={t("kpiScheduled")} value={ready ? scheduled : undefined} />
      <KpiTile label={t("kpiInUse")} value={ready ? inUse : undefined} />
    </div>
  );
}

function Related() {
  const t = useT(M);
  return (
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
  );
}

function Parameters() {
  const t = useT(M);
  const canWrite = useCan("pricing_policy.write");
  const [changing, setChanging] = useState<PolicyParam | null>(null);
  const [history, setHistory] = useState<PolicyParam | null>(null);
  const [deciding, setDeciding] = useState<Deciding>(null);
  const listing = useQuery({ queryKey: ["pricing", "policy"], queryFn: () => policyParamsApi.list() });
  const items = useMemo(() => listing.data?.items ?? [], [listing.data]);
  const step = useSteps(t, () => setDeciding(null));
  const onStep: StepFn = (kind, param, version) => {
    if (kind === "approve" || kind === "reject" || kind === "discard") setDeciding({ kind, param, version });
    else step.mutate({ kind, id: version.id, param });
  };
  const columns: ColumnDef<PolicyParam, unknown>[] = [
    { id: "param", header: t("colParam"), cell: ({ row }) => <ParamCell p={row.original} /> },
    { id: "value", header: t("colValue"), cell: ({ row }) => <ValueCell p={row.original} /> },
    { id: "change", header: t("colStatus"), cell: ({ row }) => <ChangeCell p={row.original} busy={step.isPending} onStep={onStep} /> },
    {
      id: "approver",
      header: t("colApprover"),
      cell: ({ row }) => <span className="whitespace-nowrap text-[13px] text-body">{approverLabel(t, row.original)}</span>,
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <ActionsCell p={row.original} canWrite={canWrite} onChange={() => setChanging(row.original)} onHistory={() => setHistory(row.original)} />
      ),
    },
  ];
  const d = deciding;
  return (
    <>
      <Kpis items={items} ready={!!listing.data} />
      <FilterBar search={{ placeholder: t("searchPlaceholder") as string }} testId="policy-search" />
      <ParamSections
        items={items}
        columns={columns}
        loading={listing.isLoading}
        error={listing.isError ? errorText(t, listing.error) : null}
        onRetry={() => listing.refetch()}
      />
      <Related />
      {changing && <ChangeDrawer param={changing} items={items} onClose={() => setChanging(null)} />}
      {history && <HistoryDrawer param={history} onClose={() => setHistory(null)} />}
      <DecideDialog
        d={d}
        busy={step.isPending}
        onClose={() => setDeciding(null)}
        onConfirm={(reason) => d && step.mutate({ kind: d.kind, id: d.version.id, reason, param: d.param })}
      />
    </>
  );
}

function Policy() {
  const t = useT(M);
  const { get } = useUrlFilters();
  const tab = get("tab") === "cancellation" ? "cancellation" : "parameters";
  return (
    <PageFrame title={t("title")} description={t("description")} actions={<LangSwitch ariaLabel={t("language")} />}>
      <SavedTabs
        ariaLabel={t("tabsLabel") as string}
        tabs={[
          { key: "parameters", label: t("tabParameters") as string },
          { key: "cancellation", label: t("tabCancellation") as string },
        ]}
      />
      {tab === "cancellation" ? <CancellationFees /> : <Parameters />}
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
