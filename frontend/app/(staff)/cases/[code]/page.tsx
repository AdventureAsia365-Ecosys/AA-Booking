"use client";
// app/(staff)/cases/[code]/page.tsx — Case workspace (W1, UI spec §4.7; Figma Assigned Trips-1 layout,
// TRIP CASES DETAILS, USER DETAILS-3/-4/-5/-6/-7). GET /booking/trip-cases/{code} feeds the header band, the
// 4-phase stepper and the Overview / Proposals / Suppliers (AA-682 S1) / Billing (AA-682 S3) / Checks (AA-682 S2) tabs;
// the Activity tab reads /activity (status history merged with audit rows). The header shows the supply badge and the
// money badge (live invoice: to pay / overdue / paid in full; billing.read only). CIS content (tour titles) is
// read-only, tagged "From CIS".
// Writes (fe-cases lane): status actions (convert / decline / change / cancel / reopen with reason) in
// StatusActions, Assign (AssignModal), Edit details (EditDetailsDrawer), inline internal notes (NotesCard).

import { use, useState } from "react";
import Link from "next/link";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft, CalendarDays, Pencil, UserPlus, Users as UsersIcon } from "lucide-react";
import { api, ApiError } from "@/lib/api/client";
import type { ActivityItem, TripCaseDetail } from "@/lib/api/types";
import { CATALOG_STATE_META, catalogReasonLabel, isFlaggedState } from "@/lib/tourState";
import {
  Button,
  Code,
  DateText,
  EmptyState,
  FromCisTag,
  MoneyBadge,
  PersonCell,
  Pill,
  Section,
  Skeleton,
  Stepper,
  SupplyBadge,
  TRIP_CASE_STATUS_META,
  TripCaseStatusBadge,
  formatDate,
  useCan,
  useUrlFilters,
} from "@/ui";
import { QueryError } from "../_parts/QueryError";
import { useCountryNames } from "../_parts/CountryPicker";
import { AssignModal } from "./AssignModal";
import { BillingTab } from "./BillingTab";
import { ChecksTab } from "./ChecksTab";
import { EditDetailsDrawer } from "./EditDetailsDrawer";
import { NotesCard } from "./NotesCard";
import { ProposalsTab } from "./ProposalsTab";
import { StatusActions } from "./StatusActions";
import { SuppliersTab } from "./SuppliersTab";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "proposals", label: "Proposals" },
  { key: "suppliers", label: "Suppliers" },
  { key: "billing", label: "Billing" },
  { key: "checks", label: "Checks" },
  { key: "activity", label: "Activity" },
] as const;

export default function CaseWorkspacePage({ params }: { params: Promise<{ code: string }> }) {
  const { code } = use(params);
  const ref = decodeURIComponent(code);
  const q = useQuery({ queryKey: ["trip-case", ref], queryFn: () => api.tripCases.get(ref) });
  const { get, set } = useUrlFilters();
  const tab = TABS.find((t) => t.key === get("tab"))?.key ?? "overview";
  const canWrite = useCan("trip_case.write");
  const canAssign = useCan("trip_case.assign");
  const [panel, setPanel] = useState<"assign" | "edit" | null>(null);

  if (q.isLoading) return <WorkspaceSkeleton />;
  if (q.isError || !q.data) {
    const notFound = q.error instanceof ApiError && q.error.status === 404;
    return (
      <main className="aa-admin-main flex-1 px-4 py-6 sm:px-8">
        {notFound ? (
          <EmptyState
            title={`Trip case ${ref} not found`}
            description="It may have been removed, or the code is wrong."
            action={
              <Link href="/cases" className="text-[13px] font-semibold text-accent-deep">
                Back to trip cases
              </Link>
            }
          />
        ) : (
          <QueryError error={q.error} onRetry={() => q.refetch()} what="this trip case" />
        )}
      </main>
    );
  }
  const c = q.data;
  const cancelNote = c.status === "cancelled" ? [...c.status_history].reverse().find((h) => h.to_status === "cancelled")?.note : null;

  return (
    <main className="aa-admin-main flex-1 overflow-y-auto">
      {/* Header band (spec §4.7) */}
      <div className="border-b border-line bg-bg px-4 pb-5 pt-6 sm:px-8">
        <div className="mx-auto flex max-w-[1440px] flex-col gap-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex min-w-0 items-start gap-3">
              <Link href="/cases" aria-label="Back to trip cases" className="mt-1 grid h-8 w-8 shrink-0 place-items-center rounded-md border border-line bg-card text-muted hover:text-ink">
                <ArrowLeft size={16} />
              </Link>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="break-words font-display text-xl font-semibold text-ink sm:text-2xl">{c.trip_title ?? "Custom trip"}</h1>
                  <TripCaseStatusBadge status={c.status} />
                  <SupplyBadge supply={c.supply} />
                  <MoneyBadge money={c.money} />
                  {c.source_system === "tripplanner" && <Pill tone="info" dot={false}>TripPlanner</Pill>}
                  {c.channel === "b2b" && <Pill tone="accent" dot={false}>B2B</Pill>}
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-muted">
                  <Code>{c.code}</Code>
                  <span className="inline-flex items-center gap-1">
                    <CalendarDays size={14} />
                    {c.start_date ? `${formatDate(c.start_date)}${c.trip_end_date ? ` – ${formatDate(c.trip_end_date)}` : ""}` : "Dates not set"}
                    {c.duration_days ? ` · ${c.duration_days} days` : ""}
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <UsersIcon size={14} />
                    {paxLabel(c)}
                  </span>
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {canAssign && (
                <Button variant="ghost" onClick={() => setPanel("assign")}>
                  <UserPlus size={14} /> Assign
                </Button>
              )}
              {canWrite && (
                <Button variant="ghost" onClick={() => setPanel("edit")}>
                  <Pencil size={14} /> Edit details
                </Button>
              )}
              {c.channel !== "b2b" && <StatusActions c={c} />}
            </div>
          </div>
          <Stepper status={c.status} cancelReason={cancelNote} />
          <div role="tablist" className="bk-tabstrip -mb-5 flex gap-5">
            {TABS.map((t) => {
              const on = t.key === tab;
              const later = "wave" in t;
              return (
                <button
                  key={t.key}
                  role="tab"
                  aria-selected={on}
                  disabled={later}
                  title={later ? `Coming in wave ${t.wave}` : undefined}
                  onClick={() => set("tab", t.key === "overview" ? null : t.key)}
                  className={`shrink-0 border-b-2 pb-2.5 text-[13px] ${
                    on ? "border-accent font-semibold text-accent-deep" : "border-transparent text-muted hover:text-ink"
                  } ${later ? "cursor-not-allowed opacity-50 hover:text-muted" : "cursor-pointer"}`}
                >
                  {t.label}
                  {t.key === "proposals" && <span className="ml-1 text-xs text-muted2">{c.versions.length}</span>}
                  {t.key === "suppliers" && c.supply && c.supply.state !== "none" && (
                    <span className="ml-1 text-xs text-muted2">
                      {c.supply.items_booked}/{Math.max(c.supply.items_to_book, c.supply.live)}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      <div className="mx-auto max-w-[1440px] px-4 py-6 sm:px-8">
        {tab === "overview" && <Overview c={c} canAssign={canAssign} onAssign={() => setPanel("assign")} />}
        {tab === "proposals" && <ProposalsTab c={c} />}
        {tab === "suppliers" && <SuppliersTab c={c} />}
        {tab === "billing" && <BillingTab c={c} />}
        {tab === "checks" && <ChecksTab c={c} />}
        {tab === "activity" && <Activity code={c.code} />}
      </div>

      <AssignModal c={c} open={panel === "assign"} onClose={() => setPanel(null)} />
      <EditDetailsDrawer c={c} open={panel === "edit"} onClose={() => setPanel(null)} />
    </main>
  );
}

function paxLabel(c: TripCaseDetail): string {
  if (c.pax_adults != null) {
    const kids = c.pax_children ?? 0;
    return `${c.pax_adults} adult${c.pax_adults === 1 ? "" : "s"}${kids ? `, ${kids} child${kids === 1 ? "" : "ren"}` : ""}`;
  }
  return c.pax ? `${c.pax} pax` : "Pax not set";
}

function WorkspaceSkeleton() {
  return (
    <main className="aa-admin-main flex-1 overflow-y-auto" aria-busy="true" aria-label="Loading trip case">
      <div className="border-b border-line bg-bg px-4 pb-5 pt-6 sm:px-8">
        <div className="mx-auto flex max-w-[1440px] flex-col gap-4">
          <Skeleton w={280} h={26} />
          <Skeleton w={360} h={14} />
          <Skeleton h={64} r={12} />
        </div>
      </div>
      <div className="mx-auto grid max-w-[1440px] gap-5 px-4 py-6 sm:px-8 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Skeleton h={220} r={12} />
        <Skeleton h={220} r={12} />
      </div>
    </main>
  );
}

/** AA-986 L5 (root ADR 0015, Q4): a tour this case uses left the CIS catalog. The case is kept as it is; staff decide
 *  with the customer or agency (cancelling is never automatic). Paused tours raise no alert (temporary). */
function TourAlerts({ c }: { c: TripCaseDetail }) {
  const where = { leg: "a trip leg", case: "the trip plan it started from", item: "a proposal item" } as const;
  return (
    <div role="alert" className="rounded-lg border border-danger-border bg-danger-soft px-4 py-3 text-[13px]">
      <p className="font-semibold text-ink">AA no longer serves a tour in this case</p>
      <ul className="mt-1.5 flex flex-col gap-1 text-body">
        {(c.tour_alerts ?? []).map((a) => (
          <li key={a.source_id} className="min-w-0 break-words">
            <Pill tone={CATALOG_STATE_META[a.state].tone}>{CATALOG_STATE_META[a.state].label}</Pill>{" "}
            <span className="break-all font-mono text-[11px] text-muted2">{a.source_id}</span>
            {" — "}
            {catalogReasonLabel(a.reason)}
            {a.changed_at ? (
              <>
                {" since "}
                <DateText value={a.changed_at} />
              </>
            ) : null}
            {"; used by "}
            {a.used_by.map((u) => where[u]).join(", ")}.
          </li>
        ))}
      </ul>
      <p className="mt-1.5 text-xs text-muted">
        Confirmed bookings and holds are kept. New quotes and holds on this tour are refused. Cancelling is your decision.
      </p>
    </div>
  );
}

function Overview({ c, canAssign, onAssign }: { c: TripCaseDetail; canAssign: boolean; onAssign: () => void }) {
  const days = snapshotDays(c.inquiry_snapshot);
  const customDays = snapshotCustomDays(c.inquiry_snapshot);
  const sales = c.assignees.filter((a) => a.role === "sales");
  const countryNames = useCountryNames(c.countries);
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <div className="flex min-w-0 flex-col gap-5">
        {c.source_system === "tripplanner" && (
          <Section title="Inquiry" description="What the customer built in TripPlanner, as received. Read-only." actions={<Pill tone="info" dot={false}>TripPlanner</Pill>}>
            <p className="mb-3 text-[13px] text-muted">
              Received <DateText value={c.created_at} withTime />
            </p>
            {days.length ? (
              <ol className="flex flex-col divide-y divide-line2 rounded-md border border-line">
                {days.map((d, i) => (
                  <li key={i} className="flex gap-3 px-3 py-2 text-[13px]">
                    <span className="w-14 shrink-0 font-semibold text-accent-deep">Day {d.day ?? i + 1}</span>
                    <span className="min-w-0 break-words text-body">{d.name ?? "Unnamed stop"}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="text-[13px] text-muted">The inquiry carried no day-by-day route.</p>
            )}
            {customDays.length > 0 && (
              <p className="mt-3 text-[13px] text-muted">
                Plus {customDays.length} custom day{customDays.length === 1 ? "" : "s"} the customer added
                {customDays.some((d) => d.kind) ? ` (${[...new Set(customDays.map((d) => (d.kind ?? "custom").replace(/_/g, " ")))].join(", ")})` : ""}.
              </p>
            )}
          </Section>
        )}
        {!!c.tour_alerts?.length && <TourAlerts c={c} />}
        <Section title="Trip legs" description="The CIS tours this case is built from, in day order." actions={<FromCisTag />}>
          {c.legs.length ? (
            <ol className="flex flex-col gap-2">
              {c.legs.map((l, i) => (
                <li key={`${l.source_tour_id}-${l.position}`} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-md border border-line px-3 py-2.5">
                  <span className="min-w-0">
                    <span className="block break-words text-[13px] font-medium text-ink">{c.leg_titles[i] ?? l.source_tour_id}</span>
                    <span className="text-xs text-muted">
                      Days {l.day_from}–{l.day_to}
                    </span>
                  </span>
                  <span className="flex min-w-0 flex-wrap items-center gap-2">
                    {l.catalog && isFlaggedState(l.catalog.state) && (
                      <Pill tone={CATALOG_STATE_META[l.catalog.state].tone} title={CATALOG_STATE_META[l.catalog.state].help}>
                        {CATALOG_STATE_META[l.catalog.state].label}
                      </Pill>
                    )}
                    <span className="break-all font-mono text-[11px] text-muted2">{l.source_tour_id}</span>
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-[13px] text-muted">A custom trip: no CIS tour is linked yet.</p>
          )}
        </Section>
      </div>
      <div className="flex min-w-0 flex-col gap-5">
        <Section title="People">
          <div className="flex flex-col gap-4">
            <div>
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted">
                {c.channel === "b2b" ? "Agency" : "Customer"}
              </p>
              {c.channel === "b2b" || !c.customer_id ? (
                <PersonCell
                  name="B2B agency booking"
                  sub={c.party_tenant_id ? `Partner tenant ${c.party_tenant_id}` : "No customer on this case"}
                  chips={<Pill tone="accent" dot={false}>B2B</Pill>}
                />
              ) : (
                <>
                  <Link href={`/customers/${encodeURIComponent(c.customer_code ?? c.customer_id)}`} className="block rounded-md hover:bg-bg">
                    <PersonCell
                      name={c.customer_name}
                      sub={[c.customer_email, c.customer_phone].filter(Boolean).join(" · ") || null}
                      chips={!c.customer_verified ? <Pill tone="accent" dot={false}>Unverified</Pill> : null}
                    />
                  </Link>
                  {c.customer_needs_merge_review && (
                    <p className="mt-2 text-xs text-warning">
                      This lead may duplicate an existing customer:{" "}
                      <Link href={`/customers/${encodeURIComponent(c.customer_code ?? c.customer_id)}`} className="font-semibold underline">
                        review it
                      </Link>
                      .
                    </p>
                  )}
                </>
              )}
            </div>
            <div>
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <p className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted">Assigned sales</p>
                {canAssign && (
                  <button type="button" onClick={onAssign} className="cursor-pointer text-[12px] font-semibold text-accent-deep hover:underline">
                    {sales.length ? "Change" : "Assign"}
                  </button>
                )}
              </div>
              {sales.length ? (
                sales.map((a) => <PersonCell key={a.cis_user_id} name={a.name ?? a.cis_user_id} sub="Sales" />)
              ) : (
                <p className="text-[13px] text-muted">Unassigned</p>
              )}
            </div>
          </div>
        </Section>
        <Section title="Trip details">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-[13px]">
            <dt className="text-muted">Status</dt>
            <dd className="text-ink">{TRIP_CASE_STATUS_META[c.status].label}</dd>
            <dt className="text-muted">Adults / children</dt>
            <dd className="text-ink">
              {c.pax_adults ?? "—"} / {c.pax_children ?? "—"}
            </dd>
            <dt className="text-muted">Currency</dt>
            <dd className="text-ink">{c.currency ?? "—"}</dd>
            <dt className="text-muted">Countries</dt>
            <dd className="text-ink">{c.countries.length ? countryNames : "—"}</dd>
            <dt className="text-muted">Source</dt>
            <dd className="text-ink">
              {c.source_system === "tripplanner" ? (
                "TripPlanner"
              ) : c.source_system === "manual" ? (
                "Manual"
              ) : c.source_system === "crm" ? (
                "B2B agency (AA-Partner CRM)"
              ) : c.source_id ? (
                <Link href={`/catalog/trip-plans/${encodeURIComponent(c.source_id)}`} className="text-accent-deep hover:underline">
                  CIS trip plan
                </Link>
              ) : (
                "CIS"
              )}
            </dd>
            <dt className="text-muted">Created</dt>
            <dd className="text-ink">
              <DateText value={c.created_at} />
            </dd>
          </dl>
        </Section>
        <NotesCard c={c} />
      </div>
    </div>
  );
}

function snapshotDays(snapshot: Record<string, unknown> | null): { day?: number; name?: string | null }[] {
  if (!snapshot) return [];
  const it = (snapshot.itinerary as Record<string, unknown> | undefined) ?? snapshot;
  const days = it.days;
  return Array.isArray(days) ? (days.filter((d) => d && typeof d === "object") as { day?: number; name?: string | null }[]) : [];
}

function snapshotCustomDays(snapshot: Record<string, unknown> | null): { kind?: string | null }[] {
  const raw = snapshot?.custom_days;
  return Array.isArray(raw) ? (raw.filter((d) => d && typeof d === "object") as { kind?: string | null }[]) : [];
}

function Activity({ code }: { code: string }) {
  const q = useInfiniteQuery({
    queryKey: ["trip-case", code, "activity"],
    queryFn: ({ pageParam }) => api.tripCases.activity(code, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const items: ActivityItem[] = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <Section title="Activity" description="Status changes and every staff edit on this case and its versions.">
      {q.isLoading ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          <Skeleton h={14} />
          <Skeleton w="80%" h={14} />
          <Skeleton w="60%" h={14} />
        </div>
      ) : q.isError ? (
        <QueryError error={q.error} onRetry={() => q.refetch()} what="the case activity" />
      ) : items.length === 0 ? (
        <p className="text-[13px] text-muted">Nothing recorded yet.</p>
      ) : (
        <ol className="flex flex-col">
          {items.map((a) => (
            <li key={a.id} className="flex gap-3 border-b border-line2 py-3 last:border-b-0">
              <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${a.kind === "status" ? "bg-accent" : "bg-muted2"}`} />
              <div className="min-w-0 flex-1">
                <p className="text-[13px] text-ink">{a.summary ?? a.action}</p>
                <p className="text-xs text-muted">
                  {a.actor.name ?? "System"} · <DateText value={a.at} withTime />
                </p>
              </div>
            </li>
          ))}
        </ol>
      )}
      {q.hasNextPage && (
        <button type="button" onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage} className="mt-3 cursor-pointer text-[13px] font-semibold text-accent-deep hover:underline disabled:opacity-50">
          {q.isFetchingNextPage ? "Loading…" : "Show older"}
        </button>
      )}
    </Section>
  );
}
