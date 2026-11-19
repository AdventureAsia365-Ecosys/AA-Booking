"use client";
// app/(staff)/cases/[code]/page.tsx — Case workspace (W1, UI spec §4.7; Figma Assigned Trips-1 layout,
// TRIP CASES DETAILS, USER DETAILS-3/-4/-5/-6/-7). GET /booking/trip-cases/{code} feeds the header band, the
// 4-phase stepper and the Overview / Proposals / Suppliers (AA-682 S1) / Checks (AA-682 S2) tabs; the Activity tab
// reads /activity (status history merged with audit rows). The header shows the supply badge. The Billing tab, the
// money badge and the customer card of a b2c case (AA-682 S3) are retired with the B2C half of Booking (AA-1046): a b2c
// case is read-only history with no customer record. CIS content (tour titles) is read-only, tagged "From CIS".
// Writes (fe-cases lane): status actions (convert / decline / change / cancel / reopen with reason) in
// StatusActions, Assign (AssignModal), Edit details (EditDetailsDrawer), inline internal notes (NotesCard).

import { use, useState } from "react";
import Link from "next/link";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft, Briefcase, CalendarDays, FileText, Pencil, User, UserPlus, Users as UsersIcon } from "lucide-react";
import { api, ApiError } from "@/lib/api/client";
import { B2C_INTAKE_OPEN } from "@/lib/config";
import { agencyBillingApi, saveBlob } from "@/lib/api/agencyBilling";
import type { ActivityItem, TripCaseDetail } from "@/lib/api/types";
import { CATALOG_STATE_META, catalogReasonLabel, isFlaggedState } from "@/lib/tourState";
import {
  PageFrame,
  Button,
  Code,
  DateText,
  EmptyState,
  FromCisTag,
  IconButton,
  LinkButton,
  PersonCell,
  Pill,
  Section,
  Skeleton,
  Stepper,
  SupplyBadge,
  Tabs,
  TRIP_CASE_STATUS_META,
  TripCaseStatusBadge,
  Money,
  useCan,
  useUrlFilters,
} from "@/ui";
import { B2C_READ_ONLY_NOTE, isFrozenCase } from "@/ui/booking/b2cFrozen";
import { caseCountries, casePartyLabel, casePartySub, inclusiveDays, inquiryPrefs } from "@/ui/booking/caseFacts";
import { QueryError } from "../_parts/QueryError";
import { useCountryNames } from "../_parts/CountryPicker";
import { AgencyBookingPanel, VouchersButton } from "./AgencyBookingPanel";
import { AssignModal } from "./AssignModal";
import { ChecksTab } from "./ChecksTab";
import { EditDetailsDrawer } from "./EditDetailsDrawer";
import { NotesCard } from "./NotesCard";
import { ProposalsTab } from "./ProposalsTab";
import { StatusActions } from "./StatusActions";
import { SuppliersTab } from "./SuppliersTab";
import { useFormat, useLang, useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./page.messages";
import { useBookingFormat } from "@/ui/local/useBookingFormat";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "proposals", label: "Proposals" },
  { key: "suppliers", label: "Suppliers" },
  { key: "checks", label: "Checks" },
  { key: "activity", label: "Activity" },
] as const;

export default function CaseWorkspacePage({ params }: { params: Promise<{ code: string }> }) {
  const tr = useT(MSG);
  const { code } = use(params);
  const ref = decodeURIComponent(code);
  const q = useQuery({ queryKey: ["trip-case", ref], queryFn: () => api.tripCases.get(ref) });

  if (q.isLoading) return <WorkspaceSkeleton />;
  if (q.isError || !q.data) {
    const notFound = q.error instanceof ApiError && q.error.status === 404;
    return (
      <PageFrame width="full">
        {notFound ? (
          <EmptyState
            title={tr("Trip case {0} not found", ref)}
            description={tr("It may have been removed, or the code is wrong.")}
            action={
              <Link href="/cases" className="text-[13px] font-semibold text-accent-deep">
                {tr("Back to trip cases")}
              </Link>
            }
          />
        ) : (
          <QueryError error={q.error} onRetry={() => q.refetch()} what={tr("this trip case")} />
        )}
      </PageFrame>
    );
  }
  // AA-1037 (root ADR 0019 d8): a b2c case is read-only while the B2C part is closed: every tab reads its write
  // permissions from the case layout's StaffProvider (./layout.tsx, casePermissions; the API answers 410 b2b_only).
  return <Workspace c={q.data} />;
}

function Workspace({ c }: { c: TripCaseDetail }) {
  const tr = useT(MSG);
  const tx = useTx();
  const { formatDate } = useFormat();
  const { get, set } = useUrlFilters();
  const tab = TABS.find((t) => t.key === get("tab"))?.key ?? "overview";
  const canWrite = useCan("trip_case.write");
  const canAssign = useCan("trip_case.assign");
  const [panel, setPanel] = useState<"assign" | "edit" | null>(null);
  const frozen = isFrozenCase(c.channel, B2C_INTAKE_OPEN);
  const cancelNote = c.status === "cancelled" ? [...c.status_history].reverse().find((h) => h.to_status === "cancelled")?.note : null;

  return (
    <PageFrame bleed>
      {/* Header band (spec §4.7) */}
      <div className="border-b border-line bg-bg px-4 pb-5 pt-6 sm:px-8">
        <div className="mx-auto flex max-w-[1440px] flex-col gap-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex min-w-0 items-start gap-3">
              <Link href="/cases" aria-label={tr("Back to trip cases")} className="mt-1 grid h-8 w-8 shrink-0 place-items-center rounded-md border border-line bg-card text-muted hover:text-ink">
                <ArrowLeft size={16} />
              </Link>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="break-words font-display text-xl font-semibold text-ink sm:text-2xl">{c.trip_title ?? tr("Custom trip")}</h1>
                  <TripCaseStatusBadge status={c.status} />
                  <SupplyBadge supply={c.supply} />
                  {c.source_system === "tripplanner" && <Pill tone="info" dot={false}>{tr("TripPlanner")}</Pill>}
                  {c.channel === "b2b" && <Pill tone="accent" dot={false}>B2B</Pill>}
                  {frozen && (
                    <span title={tx(B2C_READ_ONLY_NOTE)} data-testid="case-read-only">
                      <Pill tone="neutral" dot={false}>
                        {tr("Read-only")}
                      </Pill>
                    </span>
                  )}
                </div>
                {/* Figma TRIP CASES DETAILS / Assigned Trips-1 meta: customer · agent · date range (N days) · pax. */}
                <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-muted" data-testid="case-meta">
                  <Code>{c.code}</Code>
                  <span className="inline-flex min-w-0 items-center gap-1">
                    <User size={14} className="shrink-0" />
                    <span className="truncate text-body">{c.channel === "b2b" ? tr("B2B agency") : tr("Traveller (B2C history)")}</span>
                  </span>
                  <span className="inline-flex min-w-0 items-center gap-1">
                    <Briefcase size={14} className="shrink-0" />
                    <span className="truncate">{tr("Agent:")} {salesName(c) ?? tr("Unassigned")}</span>
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <CalendarDays size={14} />
                    {tripDates(c, tr, formatDate)}
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <UsersIcon size={14} />
                    {tx(paxLabel(c))}
                  </span>
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {canAssign && (
                <Button variant="ghost" onClick={() => setPanel("assign")}>
                  <UserPlus size={14} /> {tr("Assign")}
                </Button>
              )}
              {canWrite && (
                <Button variant="ghost" onClick={() => setPanel("edit")}>
                  <Pencil size={14} /> {tr("Edit details")}
                </Button>
              )}
              {c.channel === "b2b" && <ConfirmationButton code={c.code} />}
              {c.channel === "b2b" && <VouchersButton code={c.code} />}
              {c.channel !== "b2b" && <StatusActions c={c} />}
            </div>
          </div>
          <Stepper status={c.status} cancelReason={tx(cancelNote)} />
          <Tabs
            variant="underline"
            ariaLabel={tr("Trip case sections")}
            gap={20}
            className="bk-tabstrip -mb-5"
            tabs={TABS.map((t) => {
              const later = "wave" in t;
              return {
                key: t.key,
                label: tx(t.label),
                disabled: later,
                title: later ? `Coming in wave ${t.wave}` : undefined,
                count:
                  t.key === "proposals"
                    ? c.versions.length
                    : t.key === "suppliers" && c.supply && c.supply.state !== "none"
                      ? `${c.supply.items_booked}/${Math.max(c.supply.items_to_book, c.supply.live)}`
                      : undefined,
              };
            })}
            active={tab}
            onChange={(key) => set("tab", key === "overview" ? null : key)}
          />
        </div>
      </div>

      <div className="mx-auto max-w-[1440px] px-4 py-6 sm:px-8">
        {tab === "overview" && (
          <Overview c={c} canAssign={canAssign} canEdit={canWrite} onAssign={() => setPanel("assign")} onEdit={() => setPanel("edit")} />
        )}
        {tab === "proposals" && <ProposalsTab c={c} />}
        {tab === "suppliers" && <SuppliersTab c={c} />}
        {tab === "checks" && <ChecksTab c={c} />}
        {tab === "activity" && <Activity code={c.code} />}
      </div>

      <AssignModal c={c} open={panel === "assign"} onClose={() => setPanel(null)} />
      <EditDetailsDrawer c={c} open={panel === "edit"} onClose={() => setPanel(null)} />
    </PageFrame>
  );
}

function salesName(c: TripCaseDetail): string | null {
  const a = c.assignees.find((x) => x.role === "sales");
  return a ? (a.name ?? a.cis_user_id) : null;
}

/** "6 Dec 2026 – 19 Dec 2026 (14 days)"; the length comes from the legs, else from the two dates. AA-1128: in the UI
 *  language (`tr` of this file's catalogue, `fmt` the kit date format of the language). */
function tripDates(c: TripCaseDetail, tr: ReturnType<typeof useT<typeof MSG.en>>, fmt: (d: string) => string): string {
  const end = c.trip_end_date ?? c.end_date;
  if (!c.start_date) return c.duration_days ? tr("Dates not set · {0} days", c.duration_days) : tr("Dates not set");
  const days = c.duration_days ?? inclusiveDays(c.start_date, end);
  const range = `${fmt(c.start_date)}${end ? ` – ${fmt(end)}` : ""}`;
  return days ? tr("{0} ({1} days)", range, days) : range;
}

function paxLabel(c: TripCaseDetail): string {
  if (c.pax_adults != null) {
    const kids = c.pax_children ?? 0;
    return `${c.pax_adults} adult${c.pax_adults === 1 ? "" : "s"}${kids ? `, ${kids} child${kids === 1 ? "" : "ren"}` : ""}`;
  }
  return c.pax ? `${c.pax} pax` : "Pax not set";
}

function WorkspaceSkeleton() {
  const tr = useT(MSG);
  return (
    <PageFrame bleed>
      <div role="status" aria-busy="true" aria-label={tr("Loading trip case")}>
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
      </div>
    </PageFrame>
  );
}

/** AA-986 L5 (root ADR 0015, Q4): a tour this case uses left the CIS catalog. The case is kept as it is; staff decide
 *  with the customer or agency (cancelling is never automatic). Paused tours raise no alert (temporary). */
function TourAlerts({ c }: { c: TripCaseDetail }) {
  const tr = useT(MSG);
  const tx = useTx();
  const where = { leg: tr("a trip leg"), case: tr("the trip plan it started from"), item: tr("a proposal item") } as const;
  return (
    <div role="alert" className="rounded-lg border border-danger-border bg-danger-soft px-4 py-3 text-[13px]">
      <p className="font-semibold text-ink">{tr("AA no longer serves a tour in this case")}</p>
      <ul className="mt-1.5 flex flex-col gap-1 text-body">
        {(c.tour_alerts ?? []).map((a) => (
          <li key={a.source_id} className="min-w-0 break-words">
            <Pill tone={CATALOG_STATE_META[a.state].tone}>{tx(CATALOG_STATE_META[a.state].label)}</Pill>{" "}
            <span className="break-all font-mono text-[11px] text-muted2">{a.source_id}</span>
            {" — "}
            {tx(catalogReasonLabel(a.reason))}
            {a.changed_at ? (
              <>
                {tr("since")}
                <DateText value={a.changed_at} />
              </>
            ) : null}
            {tr("; used by {0}.", a.used_by.map((u) => where[u]).join(", "))}
          </li>
        ))}
      </ul>
      <p className="mt-1.5 text-xs text-muted">
        {tr("Confirmed bookings and holds are kept. New quotes and holds on this tour are refused. Cancelling is your decision.")}
      </p>
    </div>
  );
}

// AA-996 (Figma TRIP CASES DETAILS, audit item 6): the trip facts are the main column (a two-column grid with an Edit
// pencil), the legs card follows and shrinks to one line on a custom trip, and the rail holds people and notes.
function Overview({
  c,
  canAssign,
  canEdit,
  onAssign,
  onEdit,
}: {
  c: TripCaseDetail;
  canAssign: boolean;
  canEdit: boolean;
  onAssign: () => void;
  onEdit: () => void;
}) {
  const tr = useT(MSG);
  const tx = useTx();
  const days = snapshotDays(c.inquiry_snapshot);
  const customDays = snapshotCustomDays(c.inquiry_snapshot);
  const lang = useLang();
  const prefs = c.source_system === "tripplanner" ? inquiryPrefs(c.inquiry_snapshot, lang) : null;
  const sales = c.assignees.filter((a) => a.role === "sales");
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <div className="flex min-w-0 flex-col gap-5">
        {!!c.tour_alerts?.length && <TourAlerts c={c} />}
        <TripFacts c={c} canEdit={canEdit} onEdit={onEdit} />
        {c.source_system === "tripplanner" && (
          <Section title={tr("Inquiry")} description={tr("What the customer built in TripPlanner, as received. Read-only.")} actions={<Pill tone="info" dot={false}>{tr("TripPlanner")}</Pill>}>
            <p className="mb-3 text-[13px] text-muted">
              {tr("Received")} <DateText value={c.created_at} withTime />
            </p>
            {prefs && <InquiryPrefsBlock prefs={prefs} />}
            {days.length ? (
              <ol className="flex flex-col divide-y divide-line2 rounded-md border border-line">
                {days.map((d, i) => (
                  <li key={i} className="flex gap-3 px-3 py-2 text-[13px]">
                    <span className="w-14 shrink-0 font-semibold text-accent-deep">{tr("Day")} {d.day ?? i + 1}</span>
                    <span className="min-w-0 break-words text-body">{d.name ?? tr("Unnamed stop")}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="text-[13px] text-muted">{tr("The inquiry carried no day-by-day route.")}</p>
            )}
            {customDays.length > 0 && (
              <p className="mt-3 text-[13px] text-muted">
                {tr("Plus {0} custom day{1} the customer added{2}.", customDays.length, customDays.length === 1 ? "" : "s", customDays.some((d) => d.kind) ? ` (${[...new Set(customDays.map((d) => (d.kind ?? "custom").replace(/_/g, " ")))].join(", ")})` : "")}
              </p>
            )}
          </Section>
        )}
        {c.legs.length ? (
          <Section title={tr("Trip legs")} description={tr("The CIS tours this case is built from, in day order.")} actions={<FromCisTag />}>
            <ol className="flex flex-col gap-2">
              {c.legs.map((l, i) => (
                <li key={`${l.source_tour_id}-${l.position}`} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-md border border-line px-3 py-2.5">
                  <span className="min-w-0">
                    <span className="block break-words text-[13px] font-medium text-ink">{c.leg_titles[i] ?? l.source_tour_id}</span>
                    <span className="text-xs text-muted">
                      {tr("Days {0}–{1}", l.day_from, l.day_to)}
                    </span>
                  </span>
                  <span className="flex min-w-0 flex-wrap items-center gap-2">
                    {l.catalog && isFlaggedState(l.catalog.state) && (
                      <Pill tone={CATALOG_STATE_META[l.catalog.state].tone} title={tx(CATALOG_STATE_META[l.catalog.state].help)}>
                        {tx(CATALOG_STATE_META[l.catalog.state].label)}
                      </Pill>
                    )}
                    <span className="break-all font-mono text-[11px] text-muted2">{l.source_tour_id}</span>
                  </span>
                </li>
              ))}
            </ol>
          </Section>
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-dashed border-line bg-card px-4 py-3 text-[13px]" data-testid="legs-empty">
            <span className="min-w-0 text-muted">
              <span className="font-semibold text-ink">{tr("Trip legs")}</span> ·{" "}
              {/* AA-1088 (M13): a B2B booking has no TripPlanner legs; its tours are the lines of its version */}
              {c.channel === "b2b" ? tr("An agency booking: the tours sold are listed in its version.") : tr("A custom trip: no CIS tour is linked yet.")}
            </span>
            <FromCisTag />
          </div>
        )}
      </div>
      <div className="flex min-w-0 flex-col gap-5">
        <Section title={tr("People")}>
          <div className="flex flex-col gap-4">
            <div>
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted">
                {c.channel === "b2b" ? tr("Agency") : tr("Traveller")}
              </p>
              {c.channel === "b2b" ? (
                <PersonCell
                  name={casePartyLabel(c)}
                  sub={tx(casePartySub(c))}
                  chips={<Pill tone="accent" dot={false}>B2B</Pill>}
                />
              ) : (
                <PersonCell
                  name="Traveller (B2C history)"
                  sub={tr("Adventure Asia keeps no traveller record since the B2B-only model: this case is read-only history.")}
                />
              )}
            </div>
            <div>
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <p className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted">{tr("Assigned sales")}</p>
                {canAssign && (
                  <LinkButton size="sm" onClick={onAssign}>
                    {sales.length ? tr("Change") : tr("Assign")}
                  </LinkButton>
                )}
              </div>
              {sales.length ? (
                sales.map((a) => <PersonCell key={a.cis_user_id} name={a.name ?? a.cis_user_id} sub={tr("Sales")} />)
              ) : (
                <p className="text-[13px] text-muted">{tr("Unassigned")}</p>
              )}
            </div>
          </div>
        </Section>
        {c.channel === "b2b" && <AgencyBookingPanel c={c} />}
        <NotesCard c={c} />
      </div>
    </div>
  );
}

/** The trip facts card (Figma "Trip Information"): two columns of label / value, Edit opens the Edit details drawer. */
function TripFacts({ c, canEdit, onEdit }: { c: TripCaseDetail; canEdit: boolean; onEdit: () => void }) {
  const tr = useT(MSG);
  const { formatDay } = useBookingFormat();
  const tx = useTx();
  const { codes, fromLegs } = caseCountries(c);
  const countryNames = useCountryNames(codes);
  const approved = c.versions.find((v) => v.is_approved) ?? null;
  const end = c.trip_end_date ?? c.end_date;
  const days = c.duration_days ?? inclusiveDays(c.start_date, end);
  const fact = (label: string, value: React.ReactNode, hint?: React.ReactNode) => (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{tx(label)}</dt>
      <dd className="mt-0.5 break-words text-[13px] text-ink">
        {value}
        {hint && <span className="mt-0.5 block text-xs text-muted">{hint}</span>}
      </dd>
    </div>
  );
  return (
    <Section
      title={tr("Trip details")}
      actions={
        canEdit ? (
          <IconButton onClick={onEdit} label={tr("Edit trip details")} title={tr("Edit details")}>
            <Pencil size={14} />
          </IconButton>
        ) : undefined
      }
    >
      <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2" data-testid="trip-facts">
        {fact("Status", TRIP_CASE_STATUS_META[c.status].label)}
        {fact(
          tr("Travel dates"),
          c.start_date ? `${formatDay(c.start_date)}${end ? ` – ${formatDay(end)}` : ""}` : tr("Not set"),
          days ? `${days} day${days === 1 ? "" : "s"}` : undefined,
        )}
        {fact(tr("Adults / children"), c.pax_adults != null ? `${c.pax_adults} / ${c.pax_children ?? 0}` : c.pax ? `${c.pax} pax` : tr("Not set"))}
        {fact("Countries", codes.length ? countryNames : "—", fromLegs ? tr("From the trip legs") : undefined)}
        {fact(tr("Trip style"), c.trip_style ?? "—")}
        {fact("Currency", c.currency ?? "—")}
        {fact(
          tr("Approved total"),
          approved ? (
            <Link href={`/cases/${encodeURIComponent(c.code)}/proposals/${approved.version_number}`} className="hover:underline">
              <Money amount={approved.total_price} currency={approved.currency} strong />
            </Link>
          ) : (
            <span className="text-muted">{tr("No approved proposal yet")}</span>
          ),
          approved ? `V${approved.version_number}` : undefined,
        )}
        {fact(
          "Source",
          c.source_system === "tripplanner" ? (
            "TripPlanner"
          ) : c.source_system === "manual" ? (
            "Manual"
          ) : c.source_system === "crm" ? (
            tr("B2B agency (AA-Partner CRM)")
          ) : c.source_id ? (
            <Link href={`/catalog/trip-plans/${encodeURIComponent(c.source_id)}`} className="text-accent-deep hover:underline">
              {tr("CIS trip plan")}
            </Link>
          ) : (
            "CIS"
          ),
        )}
        {fact("Created", <DateText value={c.created_at} />)}
      </dl>
    </Section>
  );
}

/** Figma USER DETAILS-3 "Client message & preferences": the customer's words quoted, then a two-column fact grid. */
function InquiryPrefsBlock({ prefs }: { prefs: NonNullable<ReturnType<typeof inquiryPrefs>> }) {
  const tr = useT(MSG);
  const { formatDay } = useBookingFormat();
  const tx = useTx();
  const w = prefs.travelWindow;
  const when = w ? (w.text ?? [w.start ? formatDay(w.start) : null, w.end ? formatDay(w.end) : null].filter(Boolean).join(" – ")) : null;
  const facts = [
    [tr("Estimated budget"), prefs.budget],
    [tr("Travel window"), when],
    [tr("Travel style"), prefs.travelStyle],
    [tr("Party"), prefs.party],
  ].filter((f): f is [string, string] => !!f[1]);
  return (
    <div className="mb-4 flex flex-col gap-3" data-testid="inquiry-prefs">
      <p className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted">{tr("Client message & preferences")}</p>
      {prefs.message && (
        <blockquote className="whitespace-pre-wrap break-words rounded-md border-l-2 border-accent bg-panel px-3 py-2 text-[13px] italic text-body">
          {tx(prefs.message)}
        </blockquote>
      )}
      {facts.length > 0 && (
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          {facts.map(([label, value]) => (
            <div key={label} className="min-w-0">
              <dt className="text-xs text-muted">{tx(label)}</dt>
              <dd className="mt-0.5 break-words text-[13px] text-ink">{value}</dd>
            </div>
          ))}
        </dl>
      )}
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
  const tr = useT(MSG);
  const q = useInfiniteQuery({
    queryKey: ["trip-case", code, "activity"],
    queryFn: ({ pageParam }) => api.tripCases.activity(code, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const items: ActivityItem[] = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <Section title={tr("Activity")} description={tr("Status changes and every staff edit on this case and its versions.")}>
      {q.isLoading ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          <Skeleton h={14} />
          <Skeleton w="80%" h={14} />
          <Skeleton w="60%" h={14} />
        </div>
      ) : q.isError ? (
        <QueryError error={q.error} onRetry={() => q.refetch()} what={tr("the case activity")} />
      ) : items.length === 0 ? (
        <p className="text-[13px] text-muted">{tr("Nothing recorded yet.")}</p>
      ) : (
        <ol className="flex flex-col">
          {items.map((a) => (
            <li key={a.id} className="flex gap-3 border-b border-line2 py-3 last:border-b-0">
              <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${a.kind === "status" ? "bg-accent" : "bg-muted2"}`} />
              <div className="min-w-0 flex-1">
                <p className="text-[13px] text-ink">{a.summary ?? a.action}</p>
                <p className="text-xs text-muted">
                  {a.actor.name ?? tr("System")} · <DateText value={a.at} withTime />
                </p>
              </div>
            </li>
          ))}
        </ol>
      )}
      {q.hasNextPage && (
        <LinkButton onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage} className="mt-3">
          {q.isFetchingNextPage ? tr("Loading…") : tr("Show older")}
        </LinkButton>
      )}
    </Section>
  );
}

// AA-1037 extras G2: the booking confirmation the agency gets (HTML file, prints to PDF; no traveller names, no prices).
function ConfirmationButton({ code }: { code: string }) {
  const tr = useT(MSG);
  const canRead = useCan("b2b_desk.read");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  if (!canRead) return null;
  async function download() {
    setBusy(true);
    setFailed(null);
    try {
      saveBlob(await agencyBillingApi.confirmation(code), `booking-confirmation-${code}.html`);
    } catch (e) {
      setFailed(e instanceof ApiError && e.status === 404 ? tr("No confirmed agency booking yet") : tr("The confirmation could not be downloaded"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <span className="inline-flex items-center gap-2">
      <Button variant="ghost" disabled={busy} onClick={download}>
        <FileText size={14} /> {busy ? tr("Preparing…") : tr("Confirmation")}
      </Button>
      {failed && <span className="text-[12px] text-danger">{failed}</span>}
    </span>
  );
}
