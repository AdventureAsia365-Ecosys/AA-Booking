"use client";
// app/(staff)/cases/[code]/proposals/[n]/page.tsx — Version builder (W1, UI spec §4.8; Figma ITINERARY
// BUILDER-3/-4 layout: header bar with back + actions, day cards with a cream header, item cards with a
// drag handle and an edit pencil, a dashed "Add another day").
//
// Data: GET /booking/trip-cases/{code} + GET …/versions/{n}/items (items with their net cost lines). Save =
// PUT …/items (items keep their ids, D2; each item carries its costs[]). Title / notes = PATCH …/versions/{n}.
// Delete draft = DELETE …/versions/{n}. Price panel = POST /booking/pricing/quote-preview, live and debounced on
// the draft lines; "Apply price" = POST …/versions/{n}/price, which prices the SAVED lines on the server and stores the
// total. Send to the customer is retired with the B2C half of Booking (AA-1046); a version sent before stays locked.
// Saving changed items or costs clears that total on the server (S236 review finding 2).
// AA-996 (Figma audit items 11, 12): a sent or approved version shows its saved price (total, channel, FX lock, sent /
// approved dates) instead of a live quote; a read-only session's 403 on the quote is explained (PricePanel); "Record
// approval" is refused for a version with no items (the API answers 422 version_empty too).
// AA-1013: items made from a CIS tour carry what CIS says today (`cis`); when a day title moved on, a notice names the
// tour and, on an editable draft, "Refresh from CIS" copies today's titles into the draft (Save keeps them).
// AA-1037: on a b2c case while the B2C part is closed, the case layout (../../layout.tsx) takes version.write out, so
// the builder is read-only and says why.

import { use, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  BadgeCheck,
  CalendarPlus,
  CheckCircle2,
  ChevronDown,
  Eye,
  GripVertical,
  Lock,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Trash2,
} from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { B2C_INTAKE_OPEN } from "@/lib/config";
import { CHANNELS, proposalsApi } from "@/lib/api/proposals";
import type { TripCaseDetail, VersionWithItems } from "@/lib/api/types";
import {
  MAX_ITEMS,
  dropItem,
  firstCostError,
  fromApi,
  groupByDay,
  itemsSignature,
  moveWithinDay,
  newKey,
  normalise,
  quoteRequest,
  toInput,
  type CostLine,
  type DraftItem,
} from "@/lib/proposalDraft";
import {
  PageFrame,
  AiNotice,
  Button,
  Code,
  ConfirmDialog,
  DateText,
  ButtonGroup,
  FromCisTag,
  IconButton,
  LinkButton,
  Money,
  Pressable,
  Section,
  TripCaseStatusBadge,
  VersionBadgePill,
  formatMoney,
  useCan,
  useStaff,
  useToast,
} from "@/ui";
import { B2C_READ_ONLY_NOTE, isFrozenCase } from "@/ui/booking/b2cFrozen";
import { PageError, PageSkeleton } from "@/ui/booking/PageState";
import { CostLinesEditor, FxNotice, PriceSettings, QuoteResult, useQuotePreview, type PriceSettingsValue } from "@/ui/booking/PricePanel";
import { dayDate, useVersion } from "@/app/(staff)/_parts/useVersion";
import { CisUpdatedNotice } from "@/app/(staff)/_parts/CisUpdatedNotice";
import { cisNewTitle, cisTourName, cisUpdates, refreshFromCis } from "@/ui/booking/cisReadback";
import { aiTextAttrs, showsAiBadge } from "@/ui/booking/aiMark";
import { ItemDrawer, type ItemDraftFields } from "./ItemDrawer";
import { DetailsDrawer } from "./DetailsDrawer";
import { casePartyLabel, lockedPriceText } from "@/ui/booking/caseFacts";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./page.messages";

const TERMINAL = ["completed", "cancelled"];

export default function VersionBuilderPage({ params }: { params: Promise<{ code: string; n: string }> }) {
  const tr = useT(MSG);
  const { code: rawCode, n: rawN } = use(params);
  const code = decodeURIComponent(rawCode);
  const n = Number(rawN);
  const { tripCase, version } = useVersion(code, n);

  if (!Number.isInteger(n) || n < 1) return <PageError error={new ApiError(tr("Not found"), 404)} what={tr("this proposal version")} />;
  if (tripCase.isLoading || version.isLoading) return <PageSkeleton />;
  if (tripCase.isError || version.isError || !tripCase.data || !version.data) {
    return (
      <PageError
        error={tripCase.error ?? version.error}
        what={tr("this proposal")}
        onRetry={() => {
          tripCase.refetch();
          version.refetch();
        }}
      />
    );
  }
  // Re-seed the editor only when the version changes identity or lock state (send / approve), never on a
  // plain save or price: the editor re-reads the saved items itself after a Save.
  const v = version.data;
  return <Builder key={`${v.id}:${v.is_sent_to_customer}:${v.is_approved}`} c={tripCase.data} v={v} />;
}

function fullDate(d: Date | null): string | null {
  if (!d) return null;
  return new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).format(d);
}

type DrawerState = { mode: "add"; day: number } | { mode: "edit"; key: string } | null;

function Builder({ c, v }: { c: TripCaseDetail; v: VersionWithItems }) {
  const tr = useT(MSG);
  const tx = useTx();
  const router = useRouter();
  const qc = useQueryClient();
  const toast = useToast();
  const me = useStaff();
  const canWrite = useCan("version.write");
  const canPrice = ["admin", "sales", "contracting"].includes(me.role);
  const canManageRules = me.role === "admin" || me.role === "contracting";
  const locked = v.is_sent_to_customer || v.is_approved;
  const closed = TERMINAL.includes(c.status);
  const editable = canWrite && !locked && !closed;

  const [items, setItems] = useState<DraftItem[]>(() => fromApi(v.items));
  const [savedSig, setSavedSig] = useState(() => itemsSignature(fromApi(v.items)));
  const [extraDays, setExtraDays] = useState(0);
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [removing, setRemoving] = useState<DraftItem | null>(null);
  const [dragKey, setDragKey] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [approveOpen, setApproveOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [price, setPrice] = useState<PriceSettingsValue>({
    channel: (v.channel as PriceSettingsValue["channel"]) ?? "b2c_tailor_made",
    currency: v.currency ?? c.currency ?? "USD",
    peak: false,
    override: "",
    overrideReason: "",
  });

  const ordered = useMemo(() => normalise(items), [items]);
  const itemsDirty = itemsSignature(items) !== savedSig;
  const dirty = itemsDirty;
  const costError = firstCostError(items);

  // Unsaved-changes guard on reload / tab close (UI spec §4.8).
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  const leave = (href: string) => {
    if (!dirty || window.confirm(tr("You have unsaved item or cost changes. Leave without saving?"))) router.push(href);
  };

  const versionKey = ["trip-case", c.code, "version", v.version_number];
  const refreshCase = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["trip-case", c.code] }),
      qc.invalidateQueries({ queryKey: ["versions"] }),
    ]);

  const save = useMutation({
    mutationFn: () => proposalsApi.saveItems(c.code, v.version_number, toInput(items)),
    onSuccess: (res) => {
      // The response is the saved state (ids for new items and cost lines, the possibly cleared price).
      const next = fromApi(res.items);
      setItems(next);
      setSavedSig(itemsSignature(next));
      qc.setQueryData(versionKey, res);
      qc.invalidateQueries({ queryKey: ["versions"] });
      qc.invalidateQueries({ queryKey: ["trip-case", c.code] });
      const cleared = v.total_price != null && res.total_price == null;
      toast.success(
        tr("{0} V{1}: {2} item(s) saved", c.code, v.version_number, res.items.length) + (cleared ? tr("; apply the price again before sending") : ""),
      );
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: () => proposalsApi.remove(c.code, v.version_number),
    onSuccess: async () => {
      await refreshCase();
      toast.success(tr("Draft V{0} deleted", v.version_number));
      router.push(`/cases/${encodeURIComponent(c.code)}?tab=proposals`);
    },
  });

  const approve = useMutation({
    mutationFn: (note: string) => proposalsApi.approve(c.code, v.version_number, note || undefined),
    onSuccess: async () => {
      await Promise.all([refreshCase(), qc.invalidateQueries({ queryKey: versionKey })]);
      toast.success(tr("Approval of V{0} recorded; {1} is confirmed", v.version_number, c.code));
      setApproveOpen(false);
    },
  });

  // ── Price ──
  const flatLines = ordered.flatMap((it) => it.costs);
  // A locked version keeps its saved price: no live quote (AA-996, audit item 12; the panel shows the saved figures).
  const request = canPrice && !locked ? quoteRequest(flatLines, price) : null;
  const quote = useQuotePreview(request);
  const fresh = quote.data && !quote.settling && !quote.isFetching && !quote.isError ? quote.data : null;
  const sellByLine = new Map<string, string>();
  if (fresh && fresh.lines.length === flatLines.length) flatLines.forEach((l, i) => sellByLine.set(l.key, fresh.lines[i].sell_amount));
  const uncosted = ordered.filter((it) => it.costs.length === 0);
  const reasonMissing = !!fresh?.below_floor && !price.overrideReason.trim();
  const applyBlocked = !canWrite
    ? tr("Your role cannot change proposals.")
    : dirty
      ? tr("Save your item and cost changes first: the price is computed from the saved lines.")
      : ordered.length === 0
        ? tr("Add at least one item.")
        : uncosted.length > 0
          ? tr("Every item needs a net cost line (0 for an included item): {0}.", uncosted.map((it) => it.title).join(", "))
          : !fresh
            ? tr("Wait for the live quote.")
            : reasonMissing
              ? tr("Give a reason for pricing below the floor.")
              : null;
  const applyPrice = useMutation({
    mutationFn: () =>
      proposalsApi.price(c.code, v.version_number, {
        channel: price.channel,
        currency: price.currency,
        peak: price.peak,
        ...(price.override.trim() ? { override_total: price.override.trim() } : {}),
        ...(price.overrideReason.trim() ? { override_reason: price.overrideReason.trim() } : {}),
      }),
    onSuccess: (res) => {
      qc.setQueryData<VersionWithItems>(versionKey, (old) => (old ? { ...old, ...res.version, items: old.items } : old));
      refreshCase();
      toast.success(tr("V{0} priced at {1}", v.version_number, formatMoney(res.version.total_price, res.version.currency)));
    },
    onError: (e: Error) => toast.error(e.message),
  });

  // ── Item edits ──
  const setItem = (key: string, patch: Partial<DraftItem>) => setItems((list) => list.map((it) => (it.key === key ? { ...it, ...patch } : it)));
  const setCosts = (key: string, costs: CostLine[]) => setItem(key, { costs });
  const newCost = (): CostLine => ({ key: newKey("cost"), net_amount: "", net_currency: c.currency ?? "USD", supplier_note: "" });

  const cisNow = cisUpdates(items);
  const refreshCis = () => {
    setItems((list) => refreshFromCis(list));
    toast.success(tr("{0} title(s) copied from CIS. Save to keep them.", cisNow.count));
  };

  const groups = groupByDay(items, extraDays);
  const lastDay = groups.length;
  const editing = drawer?.mode === "edit" ? items.find((it) => it.key === drawer.key) : undefined;
  const drawerInitial: ItemDraftFields = editing
    ? {
        title: tx(editing.title),
        day_from: editing.day_from,
        day_to: editing.day_to,
        notes: editing.notes,
        source_tour_id: editing.source_tour_id,
        supply: editing.supply ?? null,
      }
    : {
        title: "",
        day_from: drawer?.mode === "add" ? drawer.day : 1,
        day_to: drawer?.mode === "add" ? drawer.day : 1,
        notes: "",
        source_tour_id: null,
        supply: null,
      };

  const applyDrawer = (f: ItemDraftFields) => {
    if (drawer?.mode === "edit") {
      // Another room type or service: lines defaulted from the previous one's rate no longer price this item, so drop
      // them; with no line left the API takes the new rate in force on save. Lines typed by hand stay.
      const prev = items.find((it) => it.key === drawer.key);
      const switched = prev && f.supply && f.supply.id !== (prev.supply?.id ?? null);
      setItem(drawer.key, prev && switched ? { ...f, costs: prev.costs.filter((c) => !c.supplier_rate_id) } : f);
    } else setItems((list) => normalise([...list, { key: newKey("item"), ...f, costs: [] }]));
    setDrawer(null);
  };

  return (
    <PageFrame width="full">
      <div className="mx-auto flex max-w-[1440px] flex-col gap-5">
        {/* Header bar (Figma: back arrow, title, Cancel / Save as Draft / primary) */}
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line pb-4">
          <div className="flex min-w-0 items-start gap-3">
            <IconButton
              onClick={() => leave(`/cases/${encodeURIComponent(c.code)}?tab=proposals`)}
              label={tr("Back to the case")}
              className="mt-1"
            >
              <ArrowLeft size={16} />
            </IconButton>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="font-display text-2xl font-semibold text-ink">{tr("Proposal V")}{v.version_number}</h1>
                <VersionBadgePill badge={v.badge} />
                {itemsDirty && <span className="rounded-full bg-warning-soft px-2 py-0.5 text-[11px] font-semibold text-warning">{tr("Unsaved changes")}</span>}
              </div>
              <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted">
                <Code>{c.code}</Code>
                <span>·</span>
                <span>{tx(casePartyLabel(c))}</span>
                {v.title && (
                  <>
                    <span>·</span>
                    <span className="text-ink">{tx(v.title)}</span>
                  </>
                )}
                <TripCaseStatusBadge status={c.status} />
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {editable && (
              <Button variant="ghost" size="sm" onClick={() => setDetailsOpen(true)}>
                <Pencil size={13} /> {tr("Title & notes")}
              </Button>
            )}
            <Button variant="secondary" onClick={() => leave(`/cases/${encodeURIComponent(c.code)}/proposals/${v.version_number}/preview`)}>
              <Eye size={14} /> {tr("Preview")}
            </Button>
            {editable && (
              <Button
                variant={itemsDirty ? "primary" : "secondary"}
                onClick={() => save.mutate()}
                disabled={!itemsDirty || save.isPending || !!costError}
                title={costError ? `${costError.item.title}: ${costError.error}` : undefined}
              >
                <Save size={14} /> {save.isPending ? tr("Saving…") : tr("Save")}
              </Button>
            )}
            {canWrite && v.badge === "sent" && c.status === "proposal_sent" && (
              <Button
                variant="primary"
                onClick={() => setApproveOpen(true)}
                disabled={v.items.length === 0}
                title={v.items.length === 0 ? tr("A version with no items cannot be approved") : undefined}
              >
                <CheckCircle2 size={14} /> {tr("Record approval")}
              </Button>
            )}
          </div>
        </div>

        {canWrite && v.badge === "sent" && c.status === "proposal_sent" && v.items.length === 0 && (
          <p role="alert" className="-mt-2 text-xs text-danger">
            {tr("This sent version has no items, so it cannot be approved. Create a new version with the itinerary and send that one.")}
          </p>
        )}
        {editable && itemsDirty && costError && (
          <p role="alert" className="-mt-2 text-xs text-danger">
            {tr("Cannot save yet: a cost line on “{0}” is incomplete ({1})", costError.item.title, costError.error)}
          </p>
        )}

        {(locked || closed || !canWrite) && (
          <div role="status" data-testid="builder-read-only" className="flex items-center gap-2 rounded-lg border border-line bg-panel px-4 py-3 text-[13px] text-body">
            <Lock size={15} className="shrink-0 text-accent-deep" />
            {v.is_approved
              ? tr("Approved: this version is locked.")
              : v.is_sent_to_customer
                ? tr("Sent to the customer: this version is locked. Create a new version from the case to change anything.")
                : closed
                  ? tr("The case is {0}: its proposals cannot change.", c.status)
                  : isFrozenCase(c.channel, B2C_INTAKE_OPEN)
                    ? B2C_READ_ONLY_NOTE
                    : tr("Read-only: your role can view proposals but not edit them.")}
          </div>
        )}

        <CisUpdatedNotice
          summary={cisNow}
          hint={
            editable
              ? tr("Refresh copies today's CIS titles into this draft; nothing is saved until you press Save.")
              : locked
                ? tr("This version stays as it was sent. Create a new version from the case and refresh it there.")
                : tr("Only an editable draft can be refreshed.")
          }
          action={
            editable ? (
              <Button size="sm" variant="secondary" onClick={refreshCis}>
                <RefreshCw size={13} /> {tr("Refresh from CIS")}
              </Button>
            ) : undefined
          }
        />

        <div className="grid grid-cols-[minmax(0,1fr)] gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
          {/* ── Day timeline ── */}
          <Section
            title={tr("Itinerary")}
            description={c.start_date ? tr("Starts {0}", fullDate(dayDate(c.start_date, 1))) : tr("No start date on the case yet: days are numbered only.")}
            actions={
              editable ? (
                <Button size="sm" variant="secondary" onClick={() => setDrawer({ mode: "add", day: Math.max(1, lastDay) })} disabled={items.length >= MAX_ITEMS}>
                  <Plus size={13} /> {tr("Add item")}
                </Button>
              ) : undefined
            }
          >
            {groups.length === 0 ? (
              <div className="flex flex-col items-center gap-3 rounded-md border border-dashed border-accent-border px-4 py-10 text-center">
                <p className="text-sm font-semibold text-ink">{tr("No items in this version")}</p>
                <p className="max-w-md text-[13px] text-muted">
                  {tr("An item is one line of the proposal with a day range, e.g. “Ha Long Bay overnight cruise, days 3–4”.")}
                </p>
                {editable && (
                  <Button variant="primary" onClick={() => setDrawer({ mode: "add", day: 1 })}>
                    <Plus size={14} /> {tr("Add the first item")}
                  </Button>
                )}
              </div>
            ) : (
              <ol className="flex flex-col gap-3">
                {groups.map((g) => {
                  const isCollapsed = collapsed.has(g.day);
                  const date = fullDate(dayDate(c.start_date, g.day));
                  return (
                    <li
                      key={g.day}
                      className={`overflow-hidden rounded-lg border ${dragKey ? "border-dashed border-accent-border" : "border-line"}`}
                      onDragOver={(e) => editable && dragKey && e.preventDefault()}
                      onDrop={(e) => {
                        if (!editable || !dragKey) return;
                        e.preventDefault();
                        setItems((list) => dropItem(list, dragKey, { toDay: g.day }));
                        setDragKey(null);
                      }}
                    >
                      <div className="flex items-center justify-between gap-2 bg-panel px-4 py-3">
                        <div>
                          <p className="text-[15px] font-semibold text-ink">{tr("Day")} {g.day}</p>
                          {date && <p className="text-xs text-muted">{date}</p>}
                        </div>
                        <IconButton
                          label={isCollapsed ? tr("Expand day {0}", g.day) : tr("Collapse day {0}", g.day)}
                          aria-expanded={!isCollapsed}
                          onClick={() =>
                            setCollapsed((s) => {
                              const next = new Set(s);
                              if (next.has(g.day)) next.delete(g.day);
                              else next.add(g.day);
                              return next;
                            })
                          }
                        >
                          <ChevronDown size={15} className={`transition-transform ${isCollapsed ? "" : "rotate-180"}`} />
                        </IconButton>
                      </div>
                      {!isCollapsed && (
                        <div className="flex flex-col gap-2 p-3">
                          {g.continues.map((it) => (
                            <p key={`c-${it.key}`} className="rounded-md bg-panel px-3 py-1.5 text-xs text-muted">
                              {tr("Continues:")} <span className="text-body">{tx(it.title)}</span> {tr("(day {0} of {1})", g.day - it.day_from + 1, it.day_to - it.day_from + 1)}
                            </p>
                          ))}
                          {g.starts.length === 0 && g.continues.length === 0 && <p className="px-1 py-1 text-[13px] text-muted2">{tr("Free day")}</p>}
                          {g.starts.map((it, idx) => (
                            <ItemCard
                              key={it.key}
                              item={it}
                              editable={editable}
                              first={idx === 0}
                              last={idx === g.starts.length - 1}
                              sellCurrency={fresh?.currency}
                              sellAmounts={it.costs.map((l) => sellByLine.get(l.key) ?? null)}
                              onEdit={() => setDrawer({ mode: "edit", key: it.key })}
                              onRemove={() => setRemoving(it)}
                              onMove={(dir) => setItems((list) => moveWithinDay(list, it.key, dir))}
                              onDragStart={() => setDragKey(it.key)}
                              onDragEnd={() => setDragKey(null)}
                              onDropBefore={() => {
                                if (dragKey && dragKey !== it.key) setItems((list) => dropItem(list, dragKey, { beforeKey: it.key }));
                                setDragKey(null);
                              }}
                            />
                          ))}
                          {editable && (
                            <Button
                              variant="dashed"
                              onClick={() => setDrawer({ mode: "add", day: g.day })}
                              disabled={items.length >= MAX_ITEMS}
                            >
                              <Plus size={13} /> {tr("Add item on day {0}", g.day)}
                            </Button>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ol>
            )}
            {editable && groups.length > 0 && (
              <Button variant="dashed" onClick={() => setExtraDays(lastDay + 1)} className="mt-3" style={{ width: "100%" }}>
                <CalendarPlus size={15} /> {tr("Add another day")}
              </Button>
            )}
            {editable && extraDays > 0 && extraDays > Math.max(0, ...items.map((i) => i.day_to)) && (
              <p className="mt-2 text-xs text-muted">{tr("Empty days at the end are not saved: add an item to keep them.")}</p>
            )}
          </Section>

          {/* ── Side column: price + version ── */}
          <div className="flex flex-col gap-5">
            {canPrice && (
              <Section
                title={tr("Price")}
                description={locked ? lockedPriceText(c.channel, v.sent_at, v.is_approved).note : tr("Live quote from the Booking pricing engine; Apply price saves it on the version.")}
              >
                <div className="flex flex-col gap-4">
                  {locked ? (
                    <LockedPrice v={v} channel={c.channel} />
                  ) : (
                    <>
                      <PriceSettings value={price} onChange={setPrice} belowFloor={!!fresh?.below_floor} disabled={locked} />
                      <FxNotice />
                    </>
                  )}
                  <div>
                    <p className="mb-2 text-[12px] font-medium uppercase tracking-[0.06em] text-muted">{tr("Net costs by item")}</p>
                    {ordered.length === 0 ? (
                      <p className="text-xs text-muted">{tr("Add items first; each item carries its own net costs.")}</p>
                    ) : (
                      <ul className="flex flex-col gap-2">
                        {ordered.map((it) => (
                          <CostGroup
                            key={it.key}
                            item={it}
                            disabled={locked}
                            onChange={(costs) => setCosts(it.key, costs)}
                            newLine={newCost}
                            sellAmounts={it.costs.map((l) => sellByLine.get(l.key) ?? null)}
                            sellCurrency={fresh?.currency}
                          />
                        ))}
                      </ul>
                    )}
                  </div>
                  {!locked && (
                    <div className="border-t border-line2 pt-4">
                      <QuoteResult
                        query={quote}
                        canManageRules={canManageRules}
                        emptyHint={tr("Enter net costs on the items above to see the selling price.")}
                        readOnlyNote={tr("The saved version total below is still correct.")}
                      />
                    </div>
                  )}
                  {fresh?.below_floor && price.override.trim() && !price.overrideReason.trim() && (
                    <p className="text-xs text-danger">{tr("A reason is required before a below-floor price can be applied.")}</p>
                  )}
                  {editable && (
                    <div className="flex flex-col gap-1.5">
                      <Button
                        variant={v.total_price == null ? "primary" : "secondary"}
                        onClick={() => applyPrice.mutate()}
                        disabled={!!applyBlocked || applyPrice.isPending}
                        title={applyBlocked ?? undefined}
                      >
                        <BadgeCheck size={14} /> {applyPrice.isPending ? tr("Applying…") : tr("Apply price to this version")}
                      </Button>
                      {applyBlocked && <p className="text-xs text-muted">{applyBlocked}</p>}
                    </div>
                  )}
                  {!locked && (
                    <div className="rounded-md bg-panel px-3 py-2 text-xs text-muted">
                      <p>
                        {tr("Saved version total:")} <Money amount={v.total_price} currency={v.currency} strong />
                        {v.fx_locked && v.currency !== "USD" ? (
                          <>
                            {tr("at 1 USD = {0}", v.fx_locked.rate)} {v.currency}
                          </>
                        ) : null}{" "}
                        {tr("(this is what Send locks).")}
                      </p>
                      <p className="mt-1">
                        {tr("Apply price computes it on the server from the saved cost lines. Saving changed items or costs clears it.")}
                      </p>
                    </div>
                  )}
                </div>
              </Section>
            )}
            <Section title={tr("Version")}>
              <dl className="grid grid-cols-2 gap-y-2 text-[13px]">
                <dt className="text-muted">{tr("Title")}</dt>
                <dd className="truncate text-right text-ink">{v.title ?? "—"}</dd>
                <dt className="text-muted">{tr("Created")}</dt>
                <dd className="text-right text-ink"><DateText value={v.created_at} /></dd>
                <dt className="text-muted">{tr("Last edited")}</dt>
                <dd className="text-right text-ink"><DateText value={v.updated_at} withTime /></dd>
                <dt className="text-muted">{tr("Sent")}</dt>
                <dd className="text-right text-ink"><DateText value={v.sent_at} /></dd>
                <dt className="text-muted">{tr("Valid until")}</dt>
                <dd className="text-right text-ink"><DateText value={v.valid_until} /></dd>
                <dt className="text-muted">{tr("Approved")}</dt>
                <dd className="text-right text-ink"><DateText value={v.approved_at} /></dd>
              </dl>
              {v.notes && <p className="mt-3 whitespace-pre-wrap rounded-md bg-panel px-3 py-2 text-xs text-body">{tx(v.notes)}</p>}
              {editable && v.badge === "draft" && (
                <LinkButton tone="danger" size="sm" onClick={() => setDeleteOpen(true)} className="mt-4">
                  <Trash2 size={13} /> {tr("Delete draft version")}
                </LinkButton>
              )}
            </Section>
            <Link href={`/cases/${encodeURIComponent(c.code)}`} className="text-[13px] font-medium text-accent-deep hover:underline" onClick={(e) => { if (dirty) { e.preventDefault(); leave(`/cases/${encodeURIComponent(c.code)}`); } }}>
              {tr("Open case {0}", c.code)}
            </Link>
          </div>
        </div>
      </div>

      <ItemDrawer open={drawer !== null} isNew={drawer?.mode === "add"} initial={drawerInitial} onClose={() => setDrawer(null)} onApply={applyDrawer} />

      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={() => {
          if (removing) setItems((list) => list.filter((it) => it.key !== removing.key));
          setRemoving(null);
        }}
        title={tr("Remove item")}
        message={
          <>
            {tr("Remove")} <strong>{tx(removing?.title)}</strong> {tr("from V{0}? It leaves the version when you save{1}.", v.version_number, removing?.costs.length ? `, with its ${removing.costs.length} net cost line(s)` : "")}
          </>
        }
        confirmLabel={tr("Remove item")}
        danger
      />
      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={() => remove.mutate()}
        title={tr("Delete draft version")}
        message={
          <>
            {tr("Delete draft")} <strong>V{v.version_number}</strong> {tr("of {0}? It disappears from the case; its number is not reused. Sent and approved versions cannot be deleted.", c.code)}
          </>
        }
        confirmLabel={tr("Delete draft")}
        danger
        busy={remove.isPending}
        error={tx(remove.error?.message)}
      />
      <ConfirmDialog
        open={approveOpen}
        onClose={() => setApproveOpen(false)}
        onConfirm={(note) => approve.mutate(note)}
        title={tr("Record the customer's approval")}
        message={
          <>
            {tr("The customer approved")} <strong>V{v.version_number}</strong> {tr("({0}). The version stays locked and {1} moves to Confirmed.", formatMoney(v.total_price, v.currency), c.code)}
          </>
        }
        reason={{ label: tr("Note (how the customer approved)"), placeholder: tr("e.g. approved by phone on 14 Oct") }}
        confirmLabel={tr("Record approval")}
        busy={approve.isPending}
        error={tx(approve.error?.message)}
      />
      {detailsOpen && <DetailsDrawer code={c.code} n={v.version_number} title={tx(v.title)} notes={tx(v.notes)} onClose={() => setDetailsOpen(false)} />}
    </PageFrame>
  );
}

/** The price panel of a sent or approved version (AA-996, audit item 12): the saved total and how it was locked,
 *  instead of the editing empty state "No quote yet". */
function LockedPrice({ v, channel }: { v: VersionWithItems; channel: string }) {
  const tr = useT(MSG);
  const tx = useTx();
  const row = (label: string, value: React.ReactNode) => (
    <>
      <dt className="text-muted">{tx(label)}</dt>
      <dd className="text-right text-ink">{value}</dd>
    </>
  );
  return (
    <div className="flex flex-col gap-3" data-testid="locked-price">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[12px] font-medium uppercase tracking-[0.06em] text-muted">{tr("Total")}</span>
        <span className="font-display text-2xl font-semibold text-ink bk-tabular">
          {v.total_price != null ? formatMoney(v.total_price, v.currency) : "—"}
        </span>
      </div>
      {v.total_price == null && <p className="text-xs text-danger">{tr("This version was locked without a saved total.")}</p>}
      <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1.5 text-[13px]">
        {row("Channel", v.channel ? (CHANNELS.find((ch) => ch.value === v.channel)?.label ?? v.channel) : "—")}
        {row("Currency", v.currency ?? "—")}
        {row(
          tr("FX locked"),
          v.fx_locked && v.currency !== "USD" ? (
            <>
              {tr("1 USD = {0}", v.fx_locked.rate)} {v.currency}
              {v.fx_locked.as_of && (
                <span className="block text-xs text-muted">
                  {tr("as of")} <DateText value={v.fx_locked.as_of} />
                </span>
              )}
            </>
          ) : (
            tr("Not needed (USD)")
          ),
        )}
        {row("Sent", <DateText value={v.sent_at} />)}
        {row(tr("Valid until"), <DateText value={v.valid_until} />)}
        {v.is_approved && row("Approved", <DateText value={v.approved_at} />)}
      </dl>
      <p className="rounded-md bg-panel px-3 py-2 text-xs text-muted">
        {tx(lockedPriceText(channel, v.sent_at, v.is_approved).footnote)}
      </p>
    </div>
  );
}

function ItemCard({
  item,
  editable,
  first,
  last,
  sellAmounts,
  sellCurrency,
  onEdit,
  onRemove,
  onMove,
  onDragStart,
  onDragEnd,
  onDropBefore,
}: {
  item: DraftItem;
  editable: boolean;
  first: boolean;
  last: boolean;
  sellAmounts: (string | null)[];
  sellCurrency?: string;
  onEdit: () => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
  onDragStart: () => void;
  onDragEnd: () => void;
  onDropBefore: () => void;
}) {
  const tr = useT(MSG);
  const tx = useTx();
  // Per-line selling amounts only: the API returns no per-item total, and summing decimal strings in floating
  // point here could disagree with the server by a cent (S236 review finding 10).
  const priced = sellAmounts.filter((a): a is string => a != null);
  const cisTitle = cisNewTitle(item);
  const cisTour = cisTourName(item);
  return (
    <div
      draggable={editable}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = "move";
        onDragStart();
      }}
      onDragEnd={onDragEnd}
      onDragOver={(e) => editable && e.preventDefault()}
      onDrop={(e) => {
        if (!editable) return;
        e.preventDefault();
        e.stopPropagation();
        onDropBefore();
      }}
      className="flex gap-2 rounded-lg border border-line bg-card p-3"
    >
      {editable && (
        <span className="mt-1 hidden cursor-grab text-muted2 sm:block" aria-hidden="true" title={tr("Drag to reorder or to another day")}>
          <GripVertical size={16} />
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="min-w-0 break-words text-sm font-medium text-ink">{tx(item.title)}</span>
          <span className="rounded-full bg-accent-tint px-2 py-0.5 text-[11px] font-semibold text-accent-deep">
            {item.day_to > item.day_from ? tr("Days {0}–{1}", item.day_from, item.day_to) : tr("Day {0}", item.day_from)}
          </span>
          {item.source_tour_id && <FromCisTag />}
          {item.supply && (
            <span className="rounded-full border border-line bg-panel px-2 py-0.5 text-[11px] font-medium text-body" title={item.supply.code}>
              {item.supply.kind === "room_type" ? tr("Room") : tr("Service")} · {tx(item.supply.label)}
            </span>
          )}
          {!item.id && <span className="text-[11px] font-semibold text-warning">{tr("New")}</span>}
        </div>
        {cisTour && <p className="mt-1 break-words text-[12.5px] text-muted">{tr("Tour:")} {cisTour}</p>}
        {cisTitle && (
          <p className="mt-1 break-words text-[12.5px] text-warning" data-testid="cis-new-title">
            {tr("CIS now:")} <span className="font-medium">{tx(cisTitle)}</span>
          </p>
        )}
        {item.notes && <p className="mt-1 whitespace-pre-wrap text-[13px] text-muted">{tx(item.notes)}</p>}
        {item.supply?.description && (
          <p className="mt-1 line-clamp-2 text-[12.5px] text-muted" title={tr("Published in CIS for this hotel / service")}>
            <span className="font-medium text-body">{tr("From CIS:")} </span>
            {showsAiBadge(item.supply.description, item.supply.ai) && (
              <AiNotice form="badge" testId="proposal-item-ai-badge" style={{ marginRight: 6, verticalAlign: "middle" }} />
            )}
            <span {...aiTextAttrs(item.supply.description, item.supply.ai)}>{tx(item.supply.description)}</span>
          </p>
        )}
        {item.costs.length > 0 && (
          <p className="mt-1.5 text-xs text-muted">
            {tr("{0} net cost line(s)", item.costs.length)}
            {priced.length > 0 && sellCurrency && (
              <>
                {tr("· sells")}
                {priced.map((a, i) => (
                  <span key={i}>
                    {i > 0 && " + "}
                    <Money amount={a} currency={sellCurrency} className="font-semibold text-ink" />
                  </span>
                ))}
              </>
            )}
          </p>
        )}
      </div>
      {editable && (
        <ButtonGroup label={tr("Actions for {0}", item.title)} gap="sm" className="shrink-0 flex-col self-start sm:flex-row">
          <IconButton label={tr("Move {0} up", item.title)} disabled={first} onClick={() => onMove(-1)}>
            <ArrowUp size={14} />
          </IconButton>
          <IconButton label={tr("Move {0} down", item.title)} disabled={last} onClick={() => onMove(1)}>
            <ArrowDown size={14} />
          </IconButton>
          <IconButton label={tr("Edit {0}", item.title)} onClick={onEdit}>
            <Pencil size={14} />
          </IconButton>
          <IconButton tone="danger" label={tr("Remove {0}", item.title)} onClick={onRemove}>
            <Trash2 size={14} />
          </IconButton>
        </ButtonGroup>
      )}
    </div>
  );
}

function CostGroup({
  item,
  disabled,
  onChange,
  newLine,
  sellAmounts,
  sellCurrency,
}: {
  item: DraftItem;
  disabled: boolean;
  onChange: (costs: CostLine[]) => void;
  newLine: () => CostLine;
  sellAmounts: (string | null)[];
  sellCurrency?: string;
}) {
  const tr = useT(MSG);
  const tx = useTx();
  const [open, setOpen] = useState(item.costs.length > 0);
  return (
    <li className="rounded-md border border-line">
      <Pressable
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, padding: "8px 12px", textAlign: "left" }}
      >
        <span className="min-w-0">
          <span className="block truncate text-[13px] font-medium text-ink">{tx(item.title)}</span>
          <span className="text-xs text-muted">
            {tr("{0} · {1} line(s)", item.day_to > item.day_from ? `Days ${item.day_from}–${item.day_to}` : `Day ${item.day_from}`, item.costs.length)}
          </span>
        </span>
        <ChevronDown size={15} className={`shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`} />
      </Pressable>
      {open && (
        <div className="border-t border-line2 p-2">
          <CostLinesEditor lines={item.costs} onChange={onChange} newLine={newLine} disabled={disabled} sellAmounts={sellAmounts} sellCurrency={sellCurrency} />
        </div>
      )}
    </li>
  );
}
