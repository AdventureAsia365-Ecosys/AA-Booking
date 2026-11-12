"use client";
// app/(staff)/pricing/workbench/_parts/VersionsDrawer.tsx — AA-1091 (bk-052, root ADR 0005b d1 / d2 / d9): the
// Versions tab of a tour in Price setup. Which version prices which dates today, the tour's season calendar, every
// version with its state, and one version at a time: the maker edits their draft (window, label, reason, the price rows
// in the price matrix editor) and submits it; a second person reads the frozen diff (old -> new per row, highlighted
// above the policy threshold, below-floor flagged) and the impact, then approves (the diff's SHA-256 goes with it) or
// rejects with a reason; a published version is withdrawn by a second person after the dialog shows which version then
// prices each date. Buttons follow ui/booking/priceVersions.ts; the API decides (403 maker_checker, 409 diff_changed /
// pending_exists). Text in versions.messages.ts (the page's language).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Plus } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { priceVersionsApi, seasonsApi, type DiffRow, type PriceVersion, type VersionDetail } from "@/lib/api/priceVersions";
import {
  Button,
  ConfirmDialog,
  DateField,
  Drawer,
  Field,
  Input,
  LightTable,
  LinkButton,
  Money,
  Pill,
  Pressable,
  Select,
  Tabs,
  Textarea,
  formatDay,
  useCan,
  useStaff,
  useT,
  useToast,
} from "@/ui";
import { draftsFrom, draftsToBody, paxBandLabel, type DraftError, type PriceDraft } from "@/ui/booking/tripPlans";
import { VERSION_TONE, changeText, versionActions } from "@/ui/booking/priceVersions";
import { PriceMatrixEditor } from "../../../catalog/trip-plans/_parts/PriceMatrixEditor";
import { V, type VersionMessage } from "./versions.messages";

type Tab = "components" | "versions";

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export function PlanTabs({ active, onTab }: { active: Tab; onTab: (tab: Tab) => void }) {
  const t = useT(V);
  return (
    <Tabs
      variant="underline"
      ariaLabel={t("tabVersions")}
      tabs={[
        { key: "components", label: t("tabComponents") },
        { key: "versions", label: t("tabVersions") },
      ]}
      active={active}
      onChange={(k) => onTab(k as Tab)}
      testId="plan-tabs"
    />
  );
}

function VersionCard({ v, onOpen }: { v: PriceVersion; onOpen: () => void }) {
  const t = useT(V);
  return (
    <Pressable onClick={onOpen} testId="version-card">
      <span className="flex w-full min-w-0 flex-col gap-1 rounded-md border border-line bg-card px-3 py-2.5 text-left hover:border-accent-border">
      <span className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="font-semibold text-ink">v{v.version_no}</span>
        <span className="min-w-0 break-words text-body">{v.label}</span>
        <Pill tone={VERSION_TONE[v.status] ?? "neutral"}>{t(`status_${v.status}` as VersionMessage) as string}</Pill>
        {v.source === "import" && <Pill tone="info">{t("source_import")}</Pill>}
      </span>
      <span className="text-xs text-muted">
        {t("window", formatDay(v.travel_from), formatDay(v.travel_to))} · {t("rows", v.row_count)} · {t("by", v.created_by)}
      </span>
      {v.reason && <span className="break-words text-xs text-body">{v.reason}</span>}
      </span>
    </Pressable>
  );
}

function DiffView({ detail }: { detail: VersionDetail }) {
  const t = useT(V);
  const diff = detail.diff;
  if (!diff || !diff.rows) return null;
  const s = diff.summary;
  return (
    <section className="flex flex-col gap-2" data-testid="version-diff">
      <h3 className="text-[13px] font-semibold text-ink">{t("diffTitle")}</h3>
      {s && <p className="text-xs text-muted">{t("diffSummary", s.up, s.down, s.added, s.removed)}</p>}
      <p className="text-xs text-muted">{t("highlighted", diff.highlight_pct)}</p>
      <div className="rounded-md border border-line">
        <LightTable<DiffRow>
          label={t("diffTitle")}
          rows={diff.rows}
          rowKey={(r) => String(r.position)}
          columns={[
            {
              id: "band",
              header: t("colBand"),
              width: "minmax(0,1.4fr)",
              cell: (r) => (
                <span className={r.highlight ? "font-semibold text-ink" : ""}>
                  {r.channel} · {paxBandLabel(r.pax_min, r.pax_max)}
                  {r.below_floor && (
                    <span className="mt-0.5 block">
                      <Pill tone="danger">{t("belowFloorFlag")}</Pill>
                    </span>
                  )}
                </span>
              ),
            },
            { id: "dates", header: t("colDates"), width: "minmax(0,1.4fr)", cell: (r) => `${formatDay(r.valid_from)} – ${formatDay(r.valid_to)}` },
            { id: "old", header: t("colOld"), numeric: true, cell: (r) => (r.old_amount ? <Money amount={r.old_amount} currency={r.currency} /> : "—") },
            { id: "new", header: t("colNew"), numeric: true, cell: (r) => <Money amount={r.new_amount} currency={r.currency} strong /> },
            {
              id: "change",
              header: t("colChange"),
              numeric: true,
              cell: (r) => <span className={r.highlight ? "font-semibold text-warning" : "text-muted"}>{changeText(r)}</span>,
            },
          ]}
        />
      </div>
      {diff.removed.length > 0 && (
        <p className="text-xs text-body">
          {t("removedTitle")}:{" "}
          {diff.removed
            .map((r) => `${r.channel} ${paxBandLabel(r.pax_min, r.pax_max)} ${formatDay(r.valid_from)} – ${formatDay(r.valid_to)}`)
            .join("; ")}
        </p>
      )}
      {detail.impact && (
        <p className="text-xs text-body" data-testid="version-impact">
          {t("impactTitle")}: {t("impact", detail.impact.open_quotes, detail.impact.departures, detail.impact.b2b_rows_changed)}
        </p>
      )}
    </section>
  );
}

function RowsView({ detail }: { detail: VersionDetail }) {
  const t = useT(V);
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-[13px] font-semibold text-ink">{t("rowsTitle")}</h3>
      <div className="rounded-md border border-line">
        <LightTable
          label={t("rowsTitle")}
          rows={detail.items}
          rowKey={(r) => String(r.position)}
          columns={[
            { id: "band", header: t("colBand"), cell: (r) => `${r.channel} · ${paxBandLabel(r.pax_min, r.pax_max)}${r.season ? ` · ${r.season}` : ""}` },
            { id: "dates", header: t("colDates"), cell: (r) => `${formatDay(r.valid_from)} – ${formatDay(r.valid_to)}` },
            { id: "price", header: t("colPrice"), numeric: true, cell: (r) => <Money amount={r.amount} currency={r.currency} /> },
          ]}
        />
      </div>
    </section>
  );
}

function DraftEditor({ detail, onDone }: { detail: VersionDetail; onDone: () => void }) {
  const t = useT(V);
  const toast = useToast();
  const qc = useQueryClient();
  const [facts, setFacts] = useState({
    label: detail.label,
    tariff_year: String(detail.tariff_year),
    travel_from: detail.travel_from,
    travel_to: detail.travel_to,
    sale_from: detail.sale_from ?? "",
    sale_to: detail.sale_to ?? "",
    reason: detail.reason ?? "",
  });
  const [rows, setRows] = useState<PriceDraft[]>(() => draftsFrom(detail.items));
  const [rowErrors, setRowErrors] = useState<DraftError[]>([]);
  const [below, setBelow] = useState("");
  const [needBelow, setNeedBelow] = useState(false);
  const [withdrawing, setWithdrawing] = useState(false);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["price-versions"] });
    qc.invalidateQueries({ queryKey: ["price-workbench"] });
  };
  const body = () => {
    const { items, errors } = draftsToBody(rows);
    setRowErrors(errors);
    if (errors.length) throw new Error(t("fixRows", errors.length));
    return {
      label: facts.label.trim() || null,
      tariff_year: Number(facts.tariff_year) || null,
      travel_from: facts.travel_from,
      travel_to: facts.travel_to,
      sale_from: facts.sale_from || null,
      sale_to: facts.sale_to || null,
      reason: facts.reason.trim() || null,
      items,
      below_floor_reason: below.trim() || null,
    };
  };
  const onError = (err: unknown) => {
    if (err instanceof ApiError && err.code === "override_reason_required") setNeedBelow(true);
    toast.error(errorText(err, t("failed")));
  };
  const save = useMutation({
    mutationFn: () => priceVersionsApi.save(detail.id, body()),
    onSuccess: () => {
      toast.success(t("saved"));
      refresh();
    },
    onError,
  });
  const submit = useMutation({
    mutationFn: async () => {
      await priceVersionsApi.save(detail.id, body());
      return priceVersionsApi.submit(detail.id, facts.reason.trim());
    },
    onSuccess: () => {
      toast.success(t("submitted"));
      refresh();
    },
    onError,
  });
  const withdraw = useMutation({
    mutationFn: (reason: string) => priceVersionsApi.withdraw(detail.id, reason),
    onSuccess: () => {
      toast.success(t("withdrawn"));
      setWithdrawing(false);
      refresh();
      onDone();
    },
    onError,
  });
  const busy = save.isPending || submit.isPending;
  const today = new Date().toISOString().slice(0, 10);
  return (
    <div className="flex flex-col gap-4" data-testid="version-draft">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label={t("label")}>
          <Input maxLength={80} value={facts.label} onChange={(e) => setFacts({ ...facts, label: e.target.value })} />
        </Field>
        <Field label={t("tariffYear")}>
          <Input inputMode="numeric" value={facts.tariff_year} onChange={(e) => setFacts({ ...facts, tariff_year: e.target.value })} />
        </Field>
        <Field label={t("travelFrom")}>
          <DateField value={facts.travel_from} onChange={(v) => setFacts({ ...facts, travel_from: v })} aria-label={t("travelFrom")} />
        </Field>
        <Field label={t("travelTo")}>
          <DateField value={facts.travel_to} min={facts.travel_from} onChange={(v) => setFacts({ ...facts, travel_to: v })} aria-label={t("travelTo")} />
        </Field>
        <Field label={t("saleFrom")}>
          <DateField value={facts.sale_from} onChange={(v) => setFacts({ ...facts, sale_from: v })} aria-label={t("saleFrom")} />
        </Field>
        <Field label={t("saleTo")}>
          <DateField value={facts.sale_to} onChange={(v) => setFacts({ ...facts, sale_to: v })} aria-label={t("saleTo")} />
        </Field>
      </div>
      <PriceMatrixEditor rows={rows} onChange={setRows} errors={rowErrors} saved={[]} today={today} />
      {needBelow && (
        <Field label={t("belowFloor")} required>
          <Input maxLength={300} value={below} onChange={(e) => setBelow(e.target.value)} />
        </Field>
      )}
      <Field label={t("reason")} hint={t("reasonHint")} required>
        <Textarea maxLength={300} rows={2} value={facts.reason} onChange={(e) => setFacts({ ...facts, reason: e.target.value })} />
      </Field>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" disabled={busy} onClick={() => save.mutate()}>
          {t("saveDraft")}
        </Button>
        <Button variant="primary" disabled={busy || !facts.reason.trim()} onClick={() => submit.mutate()}>
          {t("submit")}
        </Button>
        <Button variant="ghost" disabled={busy} onClick={() => setWithdrawing(true)}>
          {t("withdraw")}
        </Button>
      </div>
      <ConfirmDialog
        open={withdrawing}
        onClose={() => setWithdrawing(false)}
        onConfirm={(reason) => withdraw.mutate(reason)}
        title={t("withdrawTitle")}
        body={t("withdrawBody")}
        confirmLabel={t("withdraw")}
        reason={{ label: t("reasonLabel"), required: true, maxLength: 300 }}
        busy={withdraw.isPending}
        danger
      />
    </div>
  );
}

function Decisions({ detail, onDone }: { detail: VersionDetail; onDone: () => void }) {
  const t = useT(V);
  const toast = useToast();
  const qc = useQueryClient();
  const me = useStaff().cis_user_id;
  const actions = versionActions(detail, me, useCan("price_version.write"), useCan("price_version.approve"));
  const [open, setOpen] = useState<"approve" | "reject" | "withdraw" | null>(null);
  const preview = useQuery({
    queryKey: ["price-versions", "withdraw-preview", detail.id],
    queryFn: () => priceVersionsApi.withdrawPreview(detail.id),
    enabled: open === "withdraw" && detail.status === "published",
  });
  const act = useMutation({
    mutationFn: ({ action, reason }: { action: "approve" | "reject" | "withdraw"; reason: string }) => {
      if (action === "approve") return priceVersionsApi.approve(detail.id, detail.diff_sha256 ?? "");
      if (action === "reject") return priceVersionsApi.reject(detail.id, reason);
      return priceVersionsApi.withdraw(detail.id, reason);
    },
    onSuccess: (_, { action }) => {
      toast.success(t(action === "approve" ? "approved" : action === "reject" ? "rejected" : "withdrawn"));
      setOpen(null);
      qc.invalidateQueries({ queryKey: ["price-versions"] });
      qc.invalidateQueries({ queryKey: ["price-workbench"] });
      onDone();
    },
    onError: (err) => toast.error(errorText(err, t("failed"))),
  });
  if (!actions.length) {
    return detail.status === "pending" && detail.created_by === me ? <p className="text-xs text-muted">{t("ownVersion")}</p> : null;
  }
  const withdrawBody = (
    <span className="flex flex-col gap-1.5">
      <span>{t("withdrawBody")}</span>
      {detail.status === "published" && preview.data && (
        <span className="flex flex-col gap-0.5 text-xs" data-testid="withdraw-preview">
          <span className="font-semibold">{t("withdrawPreview")}</span>
          {preview.data.priced_by.map((p) => (
            <span key={`${p.version_id}-${p.from}`}>{t("pricedBy", p.version_no, formatDay(p.from), formatDay(p.to))}</span>
          ))}
          {preview.data.unquotable.map((g) => (
            <span key={g.from} className="text-danger">
              {t("unquotable", formatDay(g.from), formatDay(g.to))}
            </span>
          ))}
          {!preview.data.priced_by.length && !preview.data.unquotable.length && <span>{t("nothingElse")}</span>}
        </span>
      )}
    </span>
  );
  return (
    <div className="flex flex-wrap gap-2" data-testid="version-decisions">
      {actions.includes("approve") && (
        <Button variant="primary" onClick={() => setOpen("approve")}>
          {t("approve")}
        </Button>
      )}
      {actions.includes("reject") && (
        <Button variant="secondary" onClick={() => setOpen("reject")}>
          {t("reject")}
        </Button>
      )}
      {actions.includes("withdraw") && (
        <Button variant="ghost" onClick={() => setOpen("withdraw")}>
          {t("withdraw")}
        </Button>
      )}
      <ConfirmDialog
        open={open === "approve"}
        onClose={() => setOpen(null)}
        onConfirm={() => act.mutate({ action: "approve", reason: "" })}
        title={t("approveTitle")}
        body={t("approveBody")}
        confirmLabel={t("approve")}
        busy={act.isPending}
      />
      <ConfirmDialog
        open={open === "reject"}
        onClose={() => setOpen(null)}
        onConfirm={(reason) => act.mutate({ action: "reject", reason })}
        title={t("rejectTitle")}
        body={t("rejectBody")}
        confirmLabel={t("reject")}
        reason={{ label: t("reasonLabel"), required: true, maxLength: 300 }}
        busy={act.isPending}
        danger
      />
      <ConfirmDialog
        open={open === "withdraw"}
        onClose={() => setOpen(null)}
        onConfirm={(reason) => act.mutate({ action: "withdraw", reason })}
        title={t("withdrawTitle")}
        body={withdrawBody}
        confirmLabel={t("withdraw")}
        reason={{ label: t("reasonLabel"), required: true, maxLength: 300 }}
        busy={act.isPending}
        danger
      />
    </div>
  );
}

function VersionPanel({ id, onBack }: { id: string; onBack: () => void }) {
  const t = useT(V);
  const me = useStaff().cis_user_id;
  const canWrite = useCan("price_version.write");
  const q = useQuery({ queryKey: ["price-versions", "one", id], queryFn: () => priceVersionsApi.get(id) });
  const d = q.data;
  return (
    <div className="flex flex-col gap-4">
      <LinkButton className="self-start" onClick={onBack}>
        <ArrowLeft size={14} /> {t("back")}
      </LinkButton>
      {q.isLoading && <p className="text-[13px] text-muted">{t("loading")}</p>}
      {q.isError && <p className="text-[13px] text-danger">{errorText(q.error, t("loadFailed"))}</p>}
      {d && (
        <>
          <div className="flex min-w-0 flex-col gap-1">
            <span className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="text-[15px] font-semibold text-ink">
                v{d.version_no} · {d.label}
              </span>
              <Pill tone={VERSION_TONE[d.status] ?? "neutral"}>{t(`status_${d.status}` as VersionMessage) as string}</Pill>
            </span>
            <span className="text-xs text-muted">
              {t("window", formatDay(d.travel_from), formatDay(d.travel_to))} · {t("tariffYear")} {d.tariff_year}
              {d.sale_from || d.sale_to ? ` · ${t("sale", formatDay(d.sale_from), formatDay(d.sale_to))}` : ""}
            </span>
            <span className="text-xs text-muted">
              {t("madeBy", d.created_by)}
              {d.approved_by ? ` · ${t("approvedBy", d.approved_by)}` : ""}
            </span>
            {d.reason && <span className="break-words text-[13px] text-body">{d.reason}</span>}
            {d.rejected_by && <span className="text-xs text-danger">{t("rejectedBy", d.rejected_by, d.reject_reason ?? "")}</span>}
            {d.withdrawn_by && <span className="text-xs text-muted">{t("withdrawnBy", d.withdrawn_by, d.withdraw_reason ?? "")}</span>}
          </div>
          {d.status === "draft" && d.created_by === me && canWrite ? (
            <DraftEditor key={d.id + d.row_count} detail={d} onDone={onBack} />
          ) : (
            <>
              <DiffView detail={d} />
              <RowsView detail={d} />
              <Decisions detail={d} onDone={onBack} />
            </>
          )}
        </>
      )}
    </div>
  );
}

export function VersionsDrawer({
  sourceId,
  title,
  onClose,
  onTab,
}: {
  sourceId: string;
  title?: string | null;
  onClose: () => void;
  onTab: (tab: Tab) => void;
}) {
  const t = useT(V);
  const toast = useToast();
  const qc = useQueryClient();
  const canWrite = useCan("price_version.write");
  const canPlan = useCan("trip_plan.write");
  const [selected, setSelected] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["price-versions", "plan", sourceId], queryFn: () => priceVersionsApi.list(sourceId) });
  const calendars = useQuery({
    queryKey: ["season-calendars", "destination"],
    queryFn: () => seasonsApi.calendars("destination"),
    staleTime: 60_000,
  });
  const create = useMutation({
    mutationFn: () => priceVersionsApi.create(sourceId, { copy_from: "in_force" }),
    onSuccess: (v) => {
      qc.invalidateQueries({ queryKey: ["price-versions"] });
      setSelected(v.id);
    },
    onError: (err) => toast.error(errorText(err, t("failed"))),
  });
  const setCalendar = useMutation({
    mutationFn: (id: string | null) => priceVersionsApi.setCalendar(sourceId, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["price-versions", "plan", sourceId] }),
    onError: (err) => toast.error(errorText(err, t("failed"))),
  });
  const data = list.data;
  return (
    <Drawer open onClose={onClose} title={t("title", title ?? sourceId)} width={760}>
      <div className="flex min-w-0 flex-col gap-4" data-testid="versions-drawer">
        <PlanTabs active="versions" onTab={onTab} />
        {selected ? (
          <VersionPanel id={selected} onBack={() => setSelected(null)} />
        ) : (
          <>
            <p className="text-[13px] text-body">{t("intro")}</p>
            {list.isLoading && <p className="text-[13px] text-muted">{t("loading")}</p>}
            {list.isError && <p className="text-[13px] text-danger">{errorText(list.error, t("loadFailed"))}</p>}
            {data && (
              <>
                <section className="flex flex-col gap-1">
                  <h3 className="text-[13px] font-semibold text-ink">{t("inForce")}</h3>
                  {data.in_force.length ? (
                    data.in_force.map((s) => (
                      <span key={`${s.version_id}-${s.from}`} className="text-[13px] text-body">
                        {t("span", s.version_no, formatDay(s.from), formatDay(s.to))}
                      </span>
                    ))
                  ) : (
                    <span className="text-[13px] text-muted">{t("inForceNone")}</span>
                  )}
                </section>
                <Field label={t("calendar")} hint={t("calendarHint")}>
                  <Select
                    value={data.calendar?.id ?? ""}
                    disabled={!canPlan || setCalendar.isPending}
                    onChange={(e) => setCalendar.mutate(e.target.value || null)}
                  >
                    <option value="">{t("calendarNone")}</option>
                    {(calendars.data?.items ?? []).map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </Select>
                </Field>
                <section className="flex flex-col gap-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="text-[13px] font-semibold text-ink">{t("versions")}</h3>
                    {canWrite && (
                      <Button size="sm" variant="primary" disabled={create.isPending} onClick={() => create.mutate()}>
                        <Plus size={13} /> {t("newDraft")}
                      </Button>
                    )}
                  </div>
                  {data.items.length === 0 && <p className="text-[13px] text-muted">{t("empty")}</p>}
                  {data.items.map((v) => (
                    <VersionCard key={v.id} v={v} onOpen={() => setSelected(v.id)} />
                  ))}
                </section>
              </>
            )}
          </>
        )}
      </div>
    </Drawer>
  );
}
