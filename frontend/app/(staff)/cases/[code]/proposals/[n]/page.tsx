"use client";
// app/(staff)/cases/[code]/proposals/[n]/page.tsx — Version builder (W1, UI spec §4.8; Figma ITINERARY
// BUILDER-3/-4 layout: header bar with back + actions, day cards with a cream header, item cards with a
// drag handle and an edit pencil, a dashed "Add another day").
//
// Data: GET /booking/trip-cases/{code} + GET …/versions/{n}/items (items with their net cost lines). Save =
// PUT …/items (items keep their ids, D2; each item carries its costs[]). Title / notes = PATCH …/versions/{n}.
// Delete draft = DELETE …/versions/{n}. Send = the shared send modal. Price panel = POST
// /booking/pricing/quote-preview, live and debounced on the draft lines; "Apply price" = POST
// …/versions/{n}/price, which prices the SAVED lines on the server and stores the total that Send locks.
// Saving changed items or costs clears that total on the server (S236 review finding 2).

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
  Save,
  Send,
  Trash2,
} from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { proposalsApi } from "@/lib/api/proposals";
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
  Button,
  Code,
  ConfirmDialog,
  DateText,
  FormDrawer,
  FromCisTag,
  Money,
  Section,
  TripCaseStatusBadge,
  VersionBadgePill,
  formatMoney,
  useCan,
  useStaff,
  useToast,
} from "@/ui";
import { PageError, PageSkeleton } from "@/ui/booking/PageState";
import { CostLinesEditor, FxNotice, PriceSettings, QuoteResult, useQuotePreview, type PriceSettingsValue } from "@/ui/booking/PricePanel";
import { dayDate, useVersion } from "@/app/(staff)/_parts/useVersion";
import { SendProposalModal } from "@/app/(staff)/proposals/_parts/SendProposalModal";
import { ItemDrawer, type ItemDraftFields } from "./ItemDrawer";

const TERMINAL = ["completed", "cancelled"];

export default function VersionBuilderPage({ params }: { params: Promise<{ code: string; n: string }> }) {
  const { code: rawCode, n: rawN } = use(params);
  const code = decodeURIComponent(rawCode);
  const n = Number(rawN);
  const { tripCase, version } = useVersion(code, n);

  if (!Number.isInteger(n) || n < 1) return <PageError error={new ApiError("Not found", 404)} what="this proposal version" />;
  if (tripCase.isLoading || version.isLoading) return <PageSkeleton />;
  if (tripCase.isError || version.isError || !tripCase.data || !version.data) {
    return (
      <PageError
        error={tripCase.error ?? version.error}
        what="this proposal"
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
  const [sendOpen, setSendOpen] = useState(false);
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
    if (!dirty || window.confirm("You have unsaved item or cost changes. Leave without saving?")) router.push(href);
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
        `${c.code} V${v.version_number}: ${res.items.length} item(s) saved` + (cleared ? "; apply the price again before sending" : ""),
      );
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: () => proposalsApi.remove(c.code, v.version_number),
    onSuccess: async () => {
      await refreshCase();
      toast.success(`Draft V${v.version_number} deleted`);
      router.push(`/cases/${encodeURIComponent(c.code)}?tab=proposals`);
    },
  });

  const approve = useMutation({
    mutationFn: (note: string) => proposalsApi.approve(c.code, v.version_number, note || undefined),
    onSuccess: async () => {
      await Promise.all([refreshCase(), qc.invalidateQueries({ queryKey: versionKey })]);
      toast.success(`Approval of V${v.version_number} recorded; ${c.code} is confirmed`);
      setApproveOpen(false);
    },
  });

  // ── Price ──
  const flatLines = ordered.flatMap((it) => it.costs);
  const request = canPrice ? quoteRequest(flatLines, price) : null;
  const quote = useQuotePreview(request);
  const fresh = quote.data && !quote.settling && !quote.isFetching && !quote.isError ? quote.data : null;
  const sellByLine = new Map<string, string>();
  if (fresh && fresh.lines.length === flatLines.length) flatLines.forEach((l, i) => sellByLine.set(l.key, fresh.lines[i].sell_amount));
  const uncosted = ordered.filter((it) => it.costs.length === 0);
  const reasonMissing = !!fresh?.below_floor && !price.overrideReason.trim();
  const applyBlocked = !canWrite
    ? "Your role cannot change proposals."
    : dirty
      ? "Save your item and cost changes first: the price is computed from the saved lines."
      : ordered.length === 0
        ? "Add at least one item."
        : uncosted.length > 0
          ? `Every item needs a net cost line (0 for an included item): ${uncosted.map((it) => it.title).join(", ")}.`
          : !fresh
            ? "Wait for the live quote."
            : reasonMissing
              ? "Give a reason for pricing below the floor."
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
      toast.success(`V${v.version_number} priced at ${formatMoney(res.version.total_price, res.version.currency)}`);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  // ── Item edits ──
  const setItem = (key: string, patch: Partial<DraftItem>) => setItems((list) => list.map((it) => (it.key === key ? { ...it, ...patch } : it)));
  const setCosts = (key: string, costs: CostLine[]) => setItem(key, { costs });
  const newCost = (): CostLine => ({ key: newKey("cost"), net_amount: "", net_currency: c.currency ?? "USD", supplier_note: "" });

  const groups = groupByDay(items, extraDays);
  const lastDay = groups.length;
  const editing = drawer?.mode === "edit" ? items.find((it) => it.key === drawer.key) : undefined;
  const drawerInitial: ItemDraftFields = editing
    ? {
        title: editing.title,
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

  const sendBlocked = itemsDirty
    ? "Save your changes first."
    : v.total_price == null
      ? "The version has no total price yet: apply a price in the Price panel."
      : !["discovery", "building", "proposal_sent"].includes(c.status)
        ? `The case is ${c.status.replace(/_/g, " ")}.`
        : items.length === 0
          ? "Add at least one item."
          : null;

  return (
    <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
      <div className="mx-auto flex max-w-[1440px] flex-col gap-5">
        {/* Header bar (Figma: back arrow, title, Cancel / Save as Draft / primary) */}
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line pb-4">
          <div className="flex min-w-0 items-start gap-3">
            <button
              type="button"
              onClick={() => leave(`/cases/${encodeURIComponent(c.code)}?tab=proposals`)}
              aria-label="Back to the case"
              className="mt-1 grid h-8 w-8 shrink-0 cursor-pointer place-items-center rounded-md border border-line bg-card text-muted hover:text-ink"
            >
              <ArrowLeft size={16} />
            </button>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="font-display text-2xl font-semibold text-ink">Proposal V{v.version_number}</h1>
                <VersionBadgePill badge={v.badge} />
                {itemsDirty && <span className="rounded-full bg-warning-soft px-2 py-0.5 text-[11px] font-semibold text-warning">Unsaved changes</span>}
              </div>
              <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted">
                <Code>{c.code}</Code>
                <span>·</span>
                <span>{c.customer_name ?? "Unnamed customer"}</span>
                {v.title && (
                  <>
                    <span>·</span>
                    <span className="text-ink">{v.title}</span>
                  </>
                )}
                <TripCaseStatusBadge status={c.status} />
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {editable && (
              <Button variant="ghost" size="sm" onClick={() => setDetailsOpen(true)}>
                <Pencil size={13} /> Title & notes
              </Button>
            )}
            <Button variant="secondary" onClick={() => leave(`/cases/${encodeURIComponent(c.code)}/proposals/${v.version_number}/preview`)}>
              <Eye size={14} /> Preview
            </Button>
            {editable && (
              <Button
                variant={itemsDirty ? "primary" : "secondary"}
                onClick={() => save.mutate()}
                disabled={!itemsDirty || save.isPending || !!costError}
                title={costError ? `${costError.item.title}: ${costError.error}` : undefined}
              >
                <Save size={14} /> {save.isPending ? "Saving…" : "Save"}
              </Button>
            )}
            {editable && (
              <Button variant="primary" onClick={() => setSendOpen(true)} disabled={!!sendBlocked} title={sendBlocked ?? undefined}>
                <Send size={14} /> Send to customer
              </Button>
            )}
            {canWrite && v.badge === "sent" && c.status === "proposal_sent" && (
              <Button variant="primary" onClick={() => setApproveOpen(true)}>
                <CheckCircle2 size={14} /> Record approval
              </Button>
            )}
          </div>
        </div>

        {editable && sendBlocked && items.length > 0 && !itemsDirty && (
          <p className="-mt-2 text-xs text-muted">Send is not available: {sendBlocked}</p>
        )}
        {editable && itemsDirty && costError && (
          <p role="alert" className="-mt-2 text-xs text-danger">
            Cannot save yet: a cost line on &ldquo;{costError.item.title}&rdquo; is incomplete ({costError.error})
          </p>
        )}

        {(locked || closed || !canWrite) && (
          <div role="status" className="flex items-center gap-2 rounded-lg border border-line bg-panel px-4 py-3 text-[13px] text-body">
            <Lock size={15} className="shrink-0 text-accent-deep" />
            {v.is_approved
              ? "Approved: this version is locked."
              : v.is_sent_to_customer
                ? "Sent to the customer: this version is locked. Create a new version from the case to change anything."
                : closed
                  ? `The case is ${c.status}: its proposals cannot change.`
                  : "Read-only: your role can view proposals but not edit them."}
          </div>
        )}

        <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
          {/* ── Day timeline ── */}
          <Section
            title="Itinerary"
            description={c.start_date ? `Starts ${fullDate(dayDate(c.start_date, 1))}` : "No start date on the case yet: days are numbered only."}
            actions={
              editable ? (
                <Button size="sm" variant="secondary" onClick={() => setDrawer({ mode: "add", day: Math.max(1, lastDay) })} disabled={items.length >= MAX_ITEMS}>
                  <Plus size={13} /> Add item
                </Button>
              ) : undefined
            }
          >
            {groups.length === 0 ? (
              <div className="flex flex-col items-center gap-3 rounded-md border border-dashed border-accent-border px-4 py-10 text-center">
                <p className="text-sm font-semibold text-ink">No items in this version</p>
                <p className="max-w-md text-[13px] text-muted">
                  An item is one line of the proposal with a day range, e.g. &ldquo;Ha Long Bay overnight cruise, days 3–4&rdquo;.
                </p>
                {editable && (
                  <Button variant="primary" onClick={() => setDrawer({ mode: "add", day: 1 })}>
                    <Plus size={14} /> Add the first item
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
                          <p className="text-[15px] font-semibold text-ink">Day {g.day}</p>
                          {date && <p className="text-xs text-muted">{date}</p>}
                        </div>
                        <button
                          type="button"
                          aria-label={isCollapsed ? `Expand day ${g.day}` : `Collapse day ${g.day}`}
                          aria-expanded={!isCollapsed}
                          onClick={() =>
                            setCollapsed((s) => {
                              const next = new Set(s);
                              if (next.has(g.day)) next.delete(g.day);
                              else next.add(g.day);
                              return next;
                            })
                          }
                          className="grid h-8 w-8 cursor-pointer place-items-center rounded-md border border-line bg-card text-muted hover:text-ink"
                        >
                          <ChevronDown size={15} className={`transition-transform ${isCollapsed ? "" : "rotate-180"}`} />
                        </button>
                      </div>
                      {!isCollapsed && (
                        <div className="flex flex-col gap-2 p-3">
                          {g.continues.map((it) => (
                            <p key={`c-${it.key}`} className="rounded-md bg-panel px-3 py-1.5 text-xs text-muted">
                              Continues: <span className="text-body">{it.title}</span> (day {g.day - it.day_from + 1} of {it.day_to - it.day_from + 1})
                            </p>
                          ))}
                          {g.starts.length === 0 && g.continues.length === 0 && <p className="px-1 py-1 text-[13px] text-muted2">Free day</p>}
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
                            <button
                              type="button"
                              onClick={() => setDrawer({ mode: "add", day: g.day })}
                              disabled={items.length >= MAX_ITEMS}
                              className="inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-md border border-dashed border-line py-2 text-[12px] font-semibold text-accent-deep hover:border-accent-border hover:bg-accent-tint disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              <Plus size={13} /> Add item on day {g.day}
                            </button>
                          )}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ol>
            )}
            {editable && groups.length > 0 && (
              <button
                type="button"
                onClick={() => setExtraDays(lastDay + 1)}
                className="mt-3 flex w-full cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-accent-border py-3 text-[13px] font-semibold text-accent-deep hover:bg-accent-tint"
              >
                <CalendarPlus size={15} /> Add another day
              </button>
            )}
            {editable && extraDays > 0 && extraDays > Math.max(0, ...items.map((i) => i.day_to)) && (
              <p className="mt-2 text-xs text-muted">Empty days at the end are not saved: add an item to keep them.</p>
            )}
          </Section>

          {/* ── Side column: price + version ── */}
          <div className="flex flex-col gap-5">
            {canPrice && (
              <Section title="Price" description="Live quote from the Booking pricing engine; Apply price saves it on the version.">
                <div className="flex flex-col gap-4">
                  <PriceSettings value={price} onChange={setPrice} belowFloor={!!fresh?.below_floor} disabled={locked} />
                  {!locked && <FxNotice />}
                  <div>
                    <p className="mb-2 text-[12px] font-medium uppercase tracking-[0.06em] text-muted">Net costs by item</p>
                    {ordered.length === 0 ? (
                      <p className="text-xs text-muted">Add items first; each item carries its own net costs.</p>
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
                  <div className="border-t border-line2 pt-4">
                    <QuoteResult query={quote} canManageRules={canManageRules} emptyHint="Enter net costs on the items above to see the selling price." />
                  </div>
                  {fresh?.below_floor && price.override.trim() && !price.overrideReason.trim() && (
                    <p className="text-xs text-danger">A reason is required before a below-floor price can be applied.</p>
                  )}
                  {editable && (
                    <div className="flex flex-col gap-1.5">
                      <Button
                        variant={v.total_price == null ? "primary" : "secondary"}
                        onClick={() => applyPrice.mutate()}
                        disabled={!!applyBlocked || applyPrice.isPending}
                        title={applyBlocked ?? undefined}
                      >
                        <BadgeCheck size={14} /> {applyPrice.isPending ? "Applying…" : "Apply price to this version"}
                      </Button>
                      {applyBlocked && <p className="text-xs text-muted">{applyBlocked}</p>}
                    </div>
                  )}
                  <div className="rounded-md bg-panel px-3 py-2 text-xs text-muted">
                    <p>
                      Saved version total: <Money amount={v.total_price} currency={v.currency} strong />
                      {v.fx_locked && v.currency !== "USD" ? (
                        <>
                          {" "}
                          at 1 USD = {v.fx_locked.rate} {v.currency}
                        </>
                      ) : null}{" "}
                      (this is what Send locks).
                    </p>
                    <p className="mt-1">
                      Apply price computes it on the server from the saved cost lines. Saving changed items or costs clears it.
                    </p>
                  </div>
                </div>
              </Section>
            )}
            <Section title="Version">
              <dl className="grid grid-cols-2 gap-y-2 text-[13px]">
                <dt className="text-muted">Title</dt>
                <dd className="truncate text-right text-ink">{v.title ?? "—"}</dd>
                <dt className="text-muted">Created</dt>
                <dd className="text-right text-ink"><DateText value={v.created_at} /></dd>
                <dt className="text-muted">Last edited</dt>
                <dd className="text-right text-ink"><DateText value={v.updated_at} withTime /></dd>
                <dt className="text-muted">Sent</dt>
                <dd className="text-right text-ink"><DateText value={v.sent_at} /></dd>
                <dt className="text-muted">Valid until</dt>
                <dd className="text-right text-ink"><DateText value={v.valid_until} /></dd>
                <dt className="text-muted">Approved</dt>
                <dd className="text-right text-ink"><DateText value={v.approved_at} /></dd>
              </dl>
              {v.notes && <p className="mt-3 whitespace-pre-wrap rounded-md bg-panel px-3 py-2 text-xs text-body">{v.notes}</p>}
              {editable && v.badge === "draft" && (
                <button
                  type="button"
                  onClick={() => setDeleteOpen(true)}
                  className="mt-4 inline-flex cursor-pointer items-center gap-1.5 text-[12px] font-semibold text-danger hover:underline"
                >
                  <Trash2 size={13} /> Delete draft version
                </button>
              )}
            </Section>
            <Link href={`/cases/${encodeURIComponent(c.code)}`} className="text-[13px] font-medium text-accent-deep hover:underline" onClick={(e) => { if (dirty) { e.preventDefault(); leave(`/cases/${encodeURIComponent(c.code)}`); } }}>
              Open case {c.code}
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
        title="Remove item"
        message={
          <>
            Remove <strong>{removing?.title}</strong> from V{v.version_number}? It leaves the version when you save
            {removing?.costs.length ? `, with its ${removing.costs.length} net cost line(s)` : ""}.
          </>
        }
        confirmLabel="Remove item"
        danger
      />
      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={() => remove.mutate()}
        title="Delete draft version"
        message={
          <>
            Delete draft <strong>V{v.version_number}</strong> of {c.code}? It disappears from the case; its number is not reused.
            Sent and approved versions cannot be deleted.
          </>
        }
        confirmLabel="Delete draft"
        danger
        busy={remove.isPending}
        error={remove.error?.message}
      />
      <ConfirmDialog
        open={approveOpen}
        onClose={() => setApproveOpen(false)}
        onConfirm={(note) => approve.mutate(note)}
        title="Record the customer's approval"
        message={
          <>
            The customer approved <strong>V{v.version_number}</strong> ({formatMoney(v.total_price, v.currency)}). The version stays locked and{" "}
            {c.code} moves to Confirmed.
          </>
        }
        reason={{ label: "Note (how the customer approved)", placeholder: "e.g. approved by phone on 14 Oct" }}
        confirmLabel="Record approval"
        busy={approve.isPending}
        error={approve.error?.message}
      />
      {detailsOpen && <DetailsDrawer code={c.code} n={v.version_number} title={v.title} notes={v.notes} onClose={() => setDetailsOpen(false)} />}
      <SendProposalModal open={sendOpen} onClose={() => setSendOpen(false)} code={c.code} n={v.version_number} />
    </main>
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
  // Per-line selling amounts only: the API returns no per-item total, and summing decimal strings in floating
  // point here could disagree with the server by a cent (S236 review finding 10).
  const priced = sellAmounts.filter((a): a is string => a != null);
  const iconBtn = "grid h-8 w-8 cursor-pointer place-items-center rounded-md border border-line bg-card text-muted hover:text-ink disabled:cursor-not-allowed disabled:opacity-30";
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
        <span className="mt-1 hidden cursor-grab text-muted2 sm:block" aria-hidden="true" title="Drag to reorder or to another day">
          <GripVertical size={16} />
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-ink">{item.title}</span>
          <span className="rounded-full bg-accent-tint px-2 py-0.5 text-[11px] font-semibold text-accent-deep">
            {item.day_to > item.day_from ? `Days ${item.day_from}–${item.day_to}` : `Day ${item.day_from}`}
          </span>
          {item.source_tour_id && <FromCisTag />}
          {item.supply && (
            <span className="rounded-full border border-line bg-panel px-2 py-0.5 text-[11px] font-medium text-body" title={item.supply.code}>
              {item.supply.kind === "room_type" ? "Room" : "Service"} · {item.supply.label}
            </span>
          )}
          {!item.id && <span className="text-[11px] font-semibold text-warning">New</span>}
        </div>
        {item.notes && <p className="mt-1 whitespace-pre-wrap text-[13px] text-muted">{item.notes}</p>}
        {item.supply?.description && (
          <p className="mt-1 line-clamp-2 text-[12.5px] text-muted" title="Published in CIS for this hotel / service">
            <span className="font-medium text-body">From CIS: </span>
            {item.supply.description}
          </p>
        )}
        {item.costs.length > 0 && (
          <p className="mt-1.5 text-xs text-muted">
            {item.costs.length} net cost line(s)
            {priced.length > 0 && sellCurrency && (
              <>
                {" "}
                · sells{" "}
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
        <div className="flex shrink-0 flex-col gap-1 sm:flex-row">
          <button type="button" className={iconBtn} aria-label={`Move ${item.title} up`} disabled={first} onClick={() => onMove(-1)}>
            <ArrowUp size={14} />
          </button>
          <button type="button" className={iconBtn} aria-label={`Move ${item.title} down`} disabled={last} onClick={() => onMove(1)}>
            <ArrowDown size={14} />
          </button>
          <button type="button" className={iconBtn} aria-label={`Edit ${item.title}`} onClick={onEdit}>
            <Pencil size={14} />
          </button>
          <button type="button" className={`${iconBtn} hover:text-danger`} aria-label={`Remove ${item.title}`} onClick={onRemove}>
            <Trash2 size={14} />
          </button>
        </div>
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
  const [open, setOpen] = useState(item.costs.length > 0);
  return (
    <li className="rounded-md border border-line">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full cursor-pointer items-center justify-between gap-2 px-3 py-2 text-left"
      >
        <span className="min-w-0">
          <span className="block truncate text-[13px] font-medium text-ink">{item.title}</span>
          <span className="text-xs text-muted">
            {item.day_to > item.day_from ? `Days ${item.day_from}–${item.day_to}` : `Day ${item.day_from}`} · {item.costs.length} line(s)
          </span>
        </span>
        <ChevronDown size={15} className={`shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="border-t border-line2 p-2">
          <CostLinesEditor lines={item.costs} onChange={onChange} newLine={newLine} disabled={disabled} sellAmounts={sellAmounts} sellCurrency={sellCurrency} />
        </div>
      )}
    </li>
  );
}

function DetailsDrawer({ code, n, title, notes, onClose }: { code: string; n: number; title: string | null; notes: string | null; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [t, setT] = useState(title ?? "");
  const [nt, setNt] = useState(notes ?? "");
  const save = useMutation({
    mutationFn: () => {
      const body: { title?: string; notes?: string | null } = {};
      if (t.trim() && t.trim() !== (title ?? "")) body.title = t.trim();
      if (nt !== (notes ?? "")) body.notes = nt.trim() ? nt : null;
      return proposalsApi.update(code, n, body);
    },
    onSuccess: async (res) => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["trip-case", code] }),
        qc.invalidateQueries({ queryKey: ["versions"] }),
      ]);
      toast.success(res.changed.length ? `V${n}: ${res.changed.join(", ")} updated` : "Nothing changed");
      onClose();
    },
  });
  const err = save.error as ApiError | null;
  const cls = "w-full rounded-md border border-line bg-card px-3 text-sm text-ink outline-none focus:border-accent-border";
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={`Title and notes · V${n}`}
      onSubmit={() => save.mutate()}
      busy={save.isPending}
      dirty={t !== (title ?? "") || nt !== (notes ?? "")}
      error={err && !err.fieldErrors.length ? err.message : null}
      fieldErrors={err?.fieldErrors ?? []}
    >
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">Proposal title</span>
        <input value={t} maxLength={200} onChange={(e) => setT(e.target.value)} placeholder="e.g. Vietnam north to south, family pace" className={`${cls} h-10`} />
        <span className="text-xs text-muted">Shown to the customer as the proposal name. A title cannot be cleared once set.</span>
      </label>
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">Internal notes</span>
        <textarea value={nt} rows={6} maxLength={10_000} onChange={(e) => setNt(e.target.value)} className={`${cls} py-2`} />
      </label>
    </FormDrawer>
  );
}
