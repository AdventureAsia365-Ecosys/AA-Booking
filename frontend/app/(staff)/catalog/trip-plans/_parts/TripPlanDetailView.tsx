"use client";
// app/(staff)/catalog/trip-plans/_parts/TripPlanDetailView.tsx — one trip plan (W2, UI spec §3 / §7; Figma TRIP
// TEMPLATE DETAILS (05); AA-679). Main column: the CIS tour, read-only and tagged "From CIS" (overview, day by day,
// photos from catalog.v_tour / v_tour_day / v_tour_photo). Side column: the Booking commercial panel (sellable,
// trip style, channel, price hint, notes) with "Edit commercial" (trip_plan.write) and "Use for trip case"
// (trip_case.write, sellable plans only). The /commercial route opens the same page with the drawer open.
// AA-996 slice 2: the commercial panel names the supplier and the from price, and a "Price matrix" panel lists the
// per-person prices (pricing.read; bk-032).
// AA-1073: a "Departures" tab (supply.read) lists the plan's fixed departures with their minimum, cutoff and agency
// allotments, and adds or generates departures (trip_plan.write).
// AA-1091: a "Hotels & services" tab (supply.read) lists the plan's components and opens the suggestions from the CIS
// itinerary (the Price setup components drawer).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Briefcase, CalendarDays, EyeOff, Globe, Hash, Pencil, Store } from "lucide-react";
import { CHANNELS } from "@/lib/api/proposals";
import { tripPlansApi } from "@/lib/api/tripPlans";
import { daysNights, marginLabel, paxBandLabel } from "@/ui/booking/tripPlans";
import { PageFrame, Button, CisPhoto, DateText, FromCisTag, ManagedInCisLink, Money, Section, useCan, useStaff } from "@/ui";
import { PageError, PageSkeleton } from "@/ui/booking/PageState";
import { DetailHeader, Fact, FactGrid, MetaItem, useDetailTab } from "../../../_parts/DetailHeader";
import { AgencyTermsPanel } from "./AgencyTermsPanel";
import { CommercialDrawer } from "./CommercialDrawer";
import { ComponentsPanel } from "./ComponentsPanel";
import { DeparturesPanel } from "./DeparturesPanel";
import { CIS_EDITOR_ROLES, SellableBadge, priceBasisLabel } from "./shared";
import { UseForTripCaseModal } from "./UseForTripCaseModal";
import { useSellableToggle } from "./useSellableToggle";
import { daysText } from "@/ui/booking/plural";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./TripPlanDetailView.messages";
import { useBookingFormat } from "@/ui/local/useBookingFormat";

const CHANNEL_LABEL: Record<string, string> = Object.fromEntries(CHANNELS.map((c) => [c.value, c.label]));

function Prose({ text }: { text: string | null | undefined }) {
  const t = useT(MSG);
  const tx = useTx();
  if (!text?.trim()) return <p className="text-[13px] text-muted2">{t("Not provided in CIS.")}</p>;
  return <p className="whitespace-pre-line text-[14px] leading-relaxed text-body">{tx(text)}</p>;
}

function highlightList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && v.trim() !== "") : [];
}

export function TripPlanDetailView({ sourceId, editOnOpen = false }: { sourceId: string; editOnOpen?: boolean }) {
  const t = useT(MSG);
  const { formatDay } = useBookingFormat();
  const tx = useTx();
  const me = useStaff();
  const canEdit = useCan("trip_plan.write");
  const canWriteCases = useCan("trip_case.write");
  const canWriteVersions = useCan("version.write");
  const canUse = canWriteCases && canWriteVersions;
  const canPrice = useCan("pricing.read");
  const canDepartures = useCan("supply.read");
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
    { key: "overview", label: t("Overview") },
    { key: "days", label: t("Day by day"), count: p?.days.length },
    { key: "photos", label: t("Photos"), count: p?.photos.length },
    ...(canDepartures ? [{ key: "departures", label: t("Departures") }] : []),
    // AA-1091: the plan's hotels and services, with suggestions from the CIS itinerary (supply.read).
    ...(canDepartures ? [{ key: "components", label: t("Hotels & services") }] : []),
  ];
  const [tab, setTab] = useDetailTab(tabs);

  if (plan.isLoading) return <PageSkeleton />;
  if (plan.isError || !p) return <PageError error={plan.error} onRetry={() => plan.refetch()} what={t("this trip plan")} />;

  const highlights = highlightList(p.highlights);
  return (
    <PageFrame bleed>
      <DetailHeader
        back={{ href: "/catalog/trip-plans", label: t("Trip plans") }}
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
            {p.duration_days && <MetaItem icon={<CalendarDays size={13} />}>{tx(daysNights(p.duration_days))}</MetaItem>}
          </>
        }
        actions={
          <>
            {CIS_EDITOR_ROLES.has(me.role) && <ManagedInCisLink path={`/admin/master-content?tour=${encodeURIComponent(p.tour_id)}`} />}
            {sellableToggle.canWrite && (
              <Button variant={p.sellable ? "danger" : "secondary"} onClick={() => sellableToggle.ask(p)}>
                {p.sellable ? <EyeOff size={15} /> : <Store size={15} />} {p.sellable ? t("Make not sellable") : t("Make sellable")}
              </Button>
            )}
            {canUse && (
              <Button variant="primary" onClick={() => setUsing(true)} disabled={!p.sellable} title={p.sellable ? undefined : t("Mark the plan sellable first")}>
                <Briefcase size={15} /> {t("Use for trip case")}
              </Button>
            )}
          </>
        }
        tabs={tabs}
        activeTab={tab}
        onTab={setTab}
      />
      {tab === "components" && canDepartures ? (
        <div className="mx-auto max-w-[1100px] px-4 py-6 sm:px-8">
          <ComponentsPanel sourceId={sourceId} title={tx(p.title)} />
        </div>
      ) : tab === "departures" && canDepartures ? (
        // AA-1073: the departures board takes the full width (a wide table and its drawer).
        <div className="mx-auto max-w-[1440px] px-4 py-6 sm:px-8">
          <DeparturesPanel sourceId={sourceId} />
        </div>
      ) : (
        <div className="mx-auto grid max-w-[1440px] gap-5 px-4 py-6 sm:px-8 lg:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]">
          <div className="flex min-w-0 flex-col gap-5">
            {tab === "overview" && (
              <>
                <Section title={t("About this tour")} actions={<FromCisTag />}>
                  <div className="flex flex-col gap-4">
                    <CisPhoto path={p.cover_photo_path} alt={t("{0} cover photo", p.title)} size="large" className="aspect-[16/7] w-full rounded-md" />
                    {p.subtitle && <p className="text-[15px] font-medium text-ink">{tx(p.subtitle)}</p>}
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
                <Section title={t("Facts")} actions={<FromCisTag />}>
                  <FactGrid>
                    <Fact label={t("Duration")}>{p.duration_raw || (p.duration_days ? daysText(p.duration_days) : "—")}</Fact>
                    <Fact label={t("Group size")}>{p.group_size || "—"}</Fact>
                    <Fact label={t("Best time to go")}>{p.best_time_to_go || "—"}</Fact>
                    <Fact label={t("Lifecycle")}>{p.lifecycle_stage || "—"}</Fact>
                    <Fact label={t("Included")} wide>
                      <Prose text={p.inclusions} />
                    </Fact>
                    <Fact label={t("Not included")} wide>
                      <Prose text={p.exclusions} />
                    </Fact>
                    <Fact label={t("CIS source id")} wide>
                      <span className="break-all font-mono text-[12.5px] text-muted">{p.source_id}</span>
                    </Fact>
                  </FactGrid>
                </Section>
              </>
            )}
            {tab === "days" && (
              <Section title={t("Day by day")} actions={<FromCisTag />}>
                {p.days.length === 0 ? (
                  <div className="flex flex-col gap-2">
                    <p className="text-[13px] text-muted">{t("CIS has no day-by-day split for this tour; the itinerary text follows.")}</p>
                    <Prose text={tx(p.itinerary_text)} />
                  </div>
                ) : (
                  <ol className="flex flex-col divide-y divide-line2">
                    {p.days.map((d) => (
                      <li key={d.day_number} className="flex gap-4 py-3 first:pt-0 last:pb-0">
                        <span className="bk-tabular w-14 shrink-0 text-[12px] font-semibold uppercase tracking-[0.06em] text-accent-deep">
                          {t("Day {0}", d.day_number)}
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="text-[14px] font-medium text-ink">{d.title?.trim() || t("Day {0}", d.day_number)}</p>
                          {d.body && <p className="mt-1 whitespace-pre-line text-[13px] leading-relaxed text-body">{tx(d.body)}</p>}
                          {d.places.length > 0 && <p className="mt-1.5 text-xs text-muted">{d.places.join(" · ")}</p>}
                        </div>
                      </li>
                    ))}
                  </ol>
                )}
              </Section>
            )}
            {tab === "photos" && (
              <Section title={t("Photos")} actions={<FromCisTag />}>
                {p.photos.length === 0 ? (
                  <p className="text-[13px] text-muted2">{t("CIS has no photos for this tour yet.")}</p>
                ) : (
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                    {p.photos.map((ph) => (
                      <figure key={ph.photo_id} className="flex min-w-0 flex-col gap-1">
                        <CisPhoto path={ph.path} alt={ph.place_label ? `${ph.place_label}, ${p.title}` : p.title} className="aspect-[4/3] w-full rounded-md" />
                        {ph.place_label && <figcaption className="truncate text-xs text-muted">{tx(ph.place_label)}</figcaption>}
                      </figure>
                    ))}
                  </div>
                )}
              </Section>
            )}
          </div>
  
          <div className="flex min-w-0 flex-col gap-5">
            <Section
              title={t("Commercial")}
              description={t("Booking-owned. The tour content stays in CIS.")}
              actions={
                canEdit && (
                  <Button size="sm" onClick={() => setEditing(true)}>
                    <Pencil size={14} /> {t("Edit")}
                  </Button>
                )
              }
            >
              <FactGrid>
                <Fact label={t("Status")}>
                  <SellableBadge sellable={p.sellable} />
                </Fact>
                <Fact label={t("Trip style")}>{p.trip_style || "—"}</Fact>
                {canPrice && (
                  <>
                    <Fact label={t("Supplier")}>
                      {p.supplier ? (
                        <span>
                          {p.supplier.company_name ?? p.supplier.code}
                          {p.supplier.status === "inactive" && <span className="text-xs text-muted"> {t("(inactive)")}</span>}
                        </span>
                      ) : (
                        "—"
                      )}
                    </Fact>
                    <Fact label={t("Price from")}>
                      {p.from_price ? (
                        <span>
                          <Money amount={p.from_price.amount} currency={p.from_price.currency} strong />{" "}
                          <span className="text-xs text-muted">{t("per person")}{p.from_price.season ? `, ${p.from_price.season}` : ""}</span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </Fact>
                    <Fact label={t("Default channel")}>{p.default_channel ? CHANNEL_LABEL[p.default_channel] ?? p.default_channel : "—"}</Fact>
                    <Fact label={t("Price hint")}>
                      {p.price_hint ? (
                        <span>
                          <span className="text-muted">{t("from")} </span>
                          <Money amount={p.price_hint.amount} currency={p.price_hint.currency} strong />{" "}
                          <span className="text-xs text-muted">{tx(priceBasisLabel(p.price_hint.basis))}</span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </Fact>
                    <Fact label={t("Commercial notes")} wide>
                      {p.commercial_notes ? <span className="whitespace-pre-line">{tx(p.commercial_notes)}</span> : "—"}
                    </Fact>
                  </>
                )}
                <Fact label={t("Trip cases from this plan")}>{p.cases_started}</Fact>
                <Fact label={t("Last change")}>
                  {p.commercial ? (
                    <span>
                      <DateText value={p.commercial.updated_at} relative />
                      {(p.commercial.updated_by ?? p.commercial.created_by)?.name && (
                        <span className="text-muted"> {t("by")} {(p.commercial.updated_by ?? p.commercial.created_by)?.name}</span>
                      )}
                    </span>
                  ) : (
                    <span className="text-muted">{t("Never edited in Booking")}</span>
                  )}
                </Fact>
              </FactGrid>
              {canPrice && (
                <p className="mt-4 text-xs text-muted">
                  {t("The price matrix is the published per-person price list; the hint is a free guide. A proposal is still priced from its own cost lines.")}
                </p>
              )}
            </Section>
            {canPrice && (
              <Section
                title={t("Price matrix")}
                description={t("Per person, by channel, season and pax band.")}
                actions={
                  canEdit && (
                    <Button size="sm" onClick={() => setEditing(true)}>
                      <Pencil size={14} /> {t("Edit")}
                    </Button>
                  )
                }
              >
                {prices.isLoading ? (
                  <p className="text-[13px] text-muted">{t("Loading…")}</p>
                ) : prices.isError ? (
                  <p className="text-[13px] text-danger">{t("The price matrix could not be loaded.")}</p>
                ) : !prices.data?.items.length ? (
                  <p className="text-[13px] text-muted2">{t("No prices yet.")}{canEdit ? t(" Add them in Edit.") : ""}</p>
                ) : (
                  <ul className="flex flex-col divide-y divide-line2" data-testid="price-matrix">
                    {prices.data.items.map((r) => (
                      <li key={r.position} className="flex items-start justify-between gap-3 py-2 first:pt-0 last:pb-0">
                        <span className="min-w-0 text-[13px]">
                          <span className="block font-medium text-ink">
                            {r.season || t("Any season")}
                            {r.peak && <span className="text-xs font-normal text-muted"> {t("· peak")}</span>}
                            <span className="font-normal text-muted"> · {tx(paxBandLabel(r.pax_min, r.pax_max))}</span>
                          </span>
                          <span className="block text-xs text-muted">
                            {CHANNEL_LABEL[r.channel] ?? r.channel} · {formatDay(r.valid_from)} – {formatDay(r.valid_to)}
                            {r.priced && marginLabel(r.effective_margin) && t(" · margin {0}", marginLabel(r.effective_margin))}
                          </span>
                          {(r.child_amount || r.infant_amount || r.single_supplement_amount) && (
                            <span className="block text-xs text-muted">
                              {[
                                r.child_amount && t("child {0}", r.child_amount),
                                r.infant_amount && t("infant {0}", r.infant_amount),
                                r.single_supplement_amount && t("single +{0}", r.single_supplement_amount),
                              ]
                                .filter(Boolean)
                                .join(" · ")}{" "}
                              {r.currency}
                            </span>
                          )}
                        </span>
                        <Money amount={r.amount} currency={r.currency} strong className="shrink-0 text-[13px]" />
                      </li>
                    ))}
                  </ul>
                )}
              </Section>
            )}
            {/* S277 UAT: the panel belongs to the Overview column, not under every tab outside the padded grid. */}
            {tab === "overview" && p.has_commercial_row && <AgencyTermsPanel plan={p} />}
          </div>
        </div>
      )}
      {editing && <CommercialDrawer plan={p} onClose={() => setEditing(false)} />}
      {using && <UseForTripCaseModal plan={p} onClose={() => setUsing(false)} />}
      {sellableToggle.dialogs}
    </PageFrame>
  );
}
