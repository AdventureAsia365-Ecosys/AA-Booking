"use client";
// app/(staff)/catalog/trip-plans/_parts/TripPlanDetailView.tsx — one trip plan (W2, UI spec §3 / §7; Figma TRIP
// TEMPLATE DETAILS (05); AA-679). Main column: the CIS tour, read-only and tagged "From CIS" (overview, day by day,
// photos from catalog.v_tour / v_tour_day / v_tour_photo). Side column: the Booking commercial panel (sellable,
// trip style, channel, price hint, notes) with "Edit commercial" (trip_plan.write) and "Use for trip case"
// (trip_case.write, sellable plans only). The /commercial route opens the same page with the drawer open.
// AA-996 slice 2: the commercial panel names the supplier and the from price, and a "Price matrix" panel lists the
// per-person prices (pricing.read; bk-032).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Briefcase, CalendarDays, EyeOff, Globe, Hash, Pencil, Store } from "lucide-react";
import { CHANNELS } from "@/lib/api/proposals";
import { tripPlansApi } from "@/lib/api/tripPlans";
import { daysNights, marginLabel, paxBandLabel } from "@/ui/booking/tripPlans";
import { Button, CisPhoto, DateText, FromCisTag, ManagedInCisLink, Money, Section, formatDay, useCan, useStaff } from "@/ui";
import { PageError, PageSkeleton } from "@/ui/booking/PageState";
import { DetailHeader, Fact, FactGrid, MetaItem, useDetailTab } from "../../../_parts/DetailHeader";
import { CommercialDrawer } from "./CommercialDrawer";
import { CIS_EDITOR_ROLES, SellableBadge, priceBasisLabel } from "./shared";
import { UseForTripCaseModal } from "./UseForTripCaseModal";
import { useSellableToggle } from "./useSellableToggle";

const CHANNEL_LABEL: Record<string, string> = Object.fromEntries(CHANNELS.map((c) => [c.value, c.label]));

function Prose({ text }: { text: string | null | undefined }) {
  if (!text?.trim()) return <p className="text-[13px] text-muted2">Not provided in CIS.</p>;
  return <p className="whitespace-pre-line text-[14px] leading-relaxed text-body">{text}</p>;
}

function highlightList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && v.trim() !== "") : [];
}

export function TripPlanDetailView({ sourceId, editOnOpen = false }: { sourceId: string; editOnOpen?: boolean }) {
  const me = useStaff();
  const canEdit = useCan("trip_plan.write");
  const canWriteCases = useCan("trip_case.write");
  const canWriteVersions = useCan("version.write");
  const canUse = canWriteCases && canWriteVersions;
  const canPrice = useCan("pricing.read");
  const [editing, setEditing] = useState(editOnOpen && canEdit);
  const [using, setUsing] = useState(false);
  const sellableToggle = useSellableToggle();

  const plan = useQuery({ queryKey: ["trip-plans", "detail", sourceId], queryFn: () => tripPlansApi.get(sourceId) });
  const prices = useQuery({
    queryKey: ["trip-plans", "prices", sourceId],
    queryFn: () => tripPlansApi.prices(sourceId),
    enabled: canPrice,
  });
  const p = plan.data;
  const tabs = [
    { key: "overview", label: "Overview" },
    { key: "days", label: "Day by day", count: p?.days.length },
    { key: "photos", label: "Photos", count: p?.photos.length },
  ];
  const [tab, setTab] = useDetailTab(tabs);

  if (plan.isLoading) return <PageSkeleton />;
  if (plan.isError || !p) return <PageError error={plan.error} onRetry={() => plan.refetch()} what="this trip plan" />;

  const highlights = highlightList(p.highlights);
  return (
    <main className="aa-admin-main flex-1 overflow-y-auto">
      <DetailHeader
        back={{ href: "/catalog/trip-plans", label: "Trip plans" }}
        name={p.title}
        badges={
          <>
            <SellableBadge sellable={p.sellable} />
            <FromCisTag />
          </>
        }
        meta={
          <>
            {p.sku && <MetaItem icon={<Hash size={13} />}>{p.sku}</MetaItem>}
            {p.country && <MetaItem icon={<Globe size={13} />}>{p.country}</MetaItem>}
            {p.duration_days && <MetaItem icon={<CalendarDays size={13} />}>{daysNights(p.duration_days)}</MetaItem>}
          </>
        }
        actions={
          <>
            {CIS_EDITOR_ROLES.has(me.role) && <ManagedInCisLink path={`/admin/master-content?tour=${encodeURIComponent(p.tour_id)}`} />}
            {sellableToggle.canWrite && (
              <Button variant={p.sellable ? "danger" : "secondary"} onClick={() => sellableToggle.ask(p)}>
                {p.sellable ? <EyeOff size={15} /> : <Store size={15} />} {p.sellable ? "Make not sellable" : "Make sellable"}
              </Button>
            )}
            {canUse && (
              <Button variant="primary" onClick={() => setUsing(true)} disabled={!p.sellable} title={p.sellable ? undefined : "Mark the plan sellable first"}>
                <Briefcase size={15} /> Use for trip case
              </Button>
            )}
          </>
        }
        tabs={tabs}
        activeTab={tab}
        onTab={setTab}
      />
      <div className="mx-auto grid max-w-[1440px] gap-5 px-4 py-6 sm:px-8 lg:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]">
        <div className="flex min-w-0 flex-col gap-5">
          {tab === "overview" && (
            <>
              <Section title="About this tour" actions={<FromCisTag />}>
                <div className="flex flex-col gap-4">
                  <CisPhoto path={p.cover_photo_path} alt={`${p.title} cover photo`} size="large" className="aspect-[16/7] w-full rounded-md" />
                  {p.subtitle && <p className="text-[15px] font-medium text-ink">{p.subtitle}</p>}
                  <Prose text={p.summary || p.description} />
                  {highlights.length > 0 && (
                    <ul className="list-disc pl-5 text-[14px] text-body">
                      {highlights.map((h) => (
                        <li key={h}>{h}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </Section>
              <Section title="Facts" actions={<FromCisTag />}>
                <FactGrid>
                  <Fact label="Duration">{p.duration_raw || (p.duration_days ? `${p.duration_days} days` : "—")}</Fact>
                  <Fact label="Group size">{p.group_size || "—"}</Fact>
                  <Fact label="Best time to go">{p.best_time_to_go || "—"}</Fact>
                  <Fact label="Lifecycle">{p.lifecycle_stage || "—"}</Fact>
                  <Fact label="Included" wide>
                    <Prose text={p.inclusions} />
                  </Fact>
                  <Fact label="Not included" wide>
                    <Prose text={p.exclusions} />
                  </Fact>
                  <Fact label="CIS source id" wide>
                    <span className="break-all font-mono text-[12.5px] text-muted">{p.source_id}</span>
                  </Fact>
                </FactGrid>
              </Section>
            </>
          )}
          {tab === "days" && (
            <Section title="Day by day" actions={<FromCisTag />}>
              {p.days.length === 0 ? (
                <div className="flex flex-col gap-2">
                  <p className="text-[13px] text-muted">CIS has no day-by-day split for this tour; the itinerary text follows.</p>
                  <Prose text={p.itinerary_text} />
                </div>
              ) : (
                <ol className="flex flex-col divide-y divide-line2">
                  {p.days.map((d) => (
                    <li key={d.day_number} className="flex gap-4 py-3 first:pt-0 last:pb-0">
                      <span className="bk-tabular w-14 shrink-0 text-[12px] font-semibold uppercase tracking-[0.06em] text-accent-deep">
                        Day {d.day_number}
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-[14px] font-medium text-ink">{d.title?.trim() || `Day ${d.day_number}`}</p>
                        {d.body && <p className="mt-1 whitespace-pre-line text-[13px] leading-relaxed text-body">{d.body}</p>}
                        {d.places.length > 0 && <p className="mt-1.5 text-xs text-muted">{d.places.join(" · ")}</p>}
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </Section>
          )}
          {tab === "photos" && (
            <Section title="Photos" actions={<FromCisTag />}>
              {p.photos.length === 0 ? (
                <p className="text-[13px] text-muted2">CIS has no photos for this tour yet.</p>
              ) : (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  {p.photos.map((ph) => (
                    <figure key={ph.photo_id} className="flex min-w-0 flex-col gap-1">
                      <CisPhoto path={ph.path} alt={ph.place_label ? `${ph.place_label}, ${p.title}` : p.title} className="aspect-[4/3] w-full rounded-md" />
                      {ph.place_label && <figcaption className="truncate text-xs text-muted">{ph.place_label}</figcaption>}
                    </figure>
                  ))}
                </div>
              )}
            </Section>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-5">
          <Section
            title="Commercial"
            description="Booking-owned. The tour content stays in CIS."
            actions={
              canEdit && (
                <Button size="sm" onClick={() => setEditing(true)}>
                  <Pencil size={14} /> Edit
                </Button>
              )
            }
          >
            <FactGrid>
              <Fact label="Status">
                <SellableBadge sellable={p.sellable} />
              </Fact>
              <Fact label="Trip style">{p.trip_style || "—"}</Fact>
              {canPrice && (
                <>
                  <Fact label="Supplier">
                    {p.supplier ? (
                      <span>
                        {p.supplier.company_name ?? p.supplier.code}
                        {p.supplier.status === "inactive" && <span className="text-xs text-muted"> (inactive)</span>}
                      </span>
                    ) : (
                      "—"
                    )}
                  </Fact>
                  <Fact label="Price from">
                    {p.from_price ? (
                      <span>
                        <Money amount={p.from_price.amount} currency={p.from_price.currency} strong />{" "}
                        <span className="text-xs text-muted">per person{p.from_price.season ? `, ${p.from_price.season}` : ""}</span>
                      </span>
                    ) : (
                      "—"
                    )}
                  </Fact>
                  <Fact label="Default channel">{p.default_channel ? CHANNEL_LABEL[p.default_channel] ?? p.default_channel : "—"}</Fact>
                  <Fact label="Price hint">
                    {p.price_hint ? (
                      <span>
                        <span className="text-muted">from </span>
                        <Money amount={p.price_hint.amount} currency={p.price_hint.currency} strong />{" "}
                        <span className="text-xs text-muted">{priceBasisLabel(p.price_hint.basis)}</span>
                      </span>
                    ) : (
                      "—"
                    )}
                  </Fact>
                  <Fact label="Commercial notes" wide>
                    {p.commercial_notes ? <span className="whitespace-pre-line">{p.commercial_notes}</span> : "—"}
                  </Fact>
                </>
              )}
              <Fact label="Trip cases from this plan">{p.cases_started}</Fact>
              <Fact label="Last change">
                {p.commercial ? (
                  <span>
                    <DateText value={p.commercial.updated_at} relative />
                    {(p.commercial.updated_by ?? p.commercial.created_by)?.name && (
                      <span className="text-muted"> by {(p.commercial.updated_by ?? p.commercial.created_by)?.name}</span>
                    )}
                  </span>
                ) : (
                  <span className="text-muted">Never edited in Booking</span>
                )}
              </Fact>
            </FactGrid>
            {canPrice && (
              <p className="mt-4 text-xs text-muted">
                The price matrix is the published per-person price list; the hint is a free guide. A proposal is still priced from its own cost lines.
              </p>
            )}
          </Section>
          {canPrice && (
            <Section
              title="Price matrix"
              description="Per person, by channel, season and pax band."
              actions={
                canEdit && (
                  <Button size="sm" onClick={() => setEditing(true)}>
                    <Pencil size={14} /> Edit
                  </Button>
                )
              }
            >
              {prices.isLoading ? (
                <p className="text-[13px] text-muted">Loading…</p>
              ) : prices.isError ? (
                <p className="text-[13px] text-danger">The price matrix could not be loaded.</p>
              ) : !prices.data?.items.length ? (
                <p className="text-[13px] text-muted2">No prices yet.{canEdit ? " Add them in Edit." : ""}</p>
              ) : (
                <ul className="flex flex-col divide-y divide-line2" data-testid="price-matrix">
                  {prices.data.items.map((r) => (
                    <li key={r.position} className="flex items-start justify-between gap-3 py-2 first:pt-0 last:pb-0">
                      <span className="min-w-0 text-[13px]">
                        <span className="block font-medium text-ink">
                          {r.season || "Any season"}
                          {r.peak && <span className="text-xs font-normal text-muted"> · peak</span>}
                          <span className="font-normal text-muted"> · {paxBandLabel(r.pax_min, r.pax_max)}</span>
                        </span>
                        <span className="block text-xs text-muted">
                          {CHANNEL_LABEL[r.channel] ?? r.channel} · {formatDay(r.valid_from)} – {formatDay(r.valid_to)}
                          {r.priced && marginLabel(r.effective_margin) && ` · margin ${marginLabel(r.effective_margin)}`}
                        </span>
                      </span>
                      <Money amount={r.amount} currency={r.currency} strong className="shrink-0 text-[13px]" />
                    </li>
                  ))}
                </ul>
              )}
            </Section>
          )}
        </div>
      </div>
      {editing && <CommercialDrawer plan={p} onClose={() => setEditing(false)} />}
      {using && <UseForTripCaseModal plan={p} onClose={() => setUsing(false)} />}
      {sellableToggle.dialogs}
    </main>
  );
}
