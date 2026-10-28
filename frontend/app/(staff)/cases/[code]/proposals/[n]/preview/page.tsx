"use client";
// app/(staff)/cases/[code]/proposals/[n]/preview/page.tsx — Customer-style preview (W1, UI spec §4.9;
// Figma ITINERARY PREVIEW): header bar with back, title row "TC-… · trip" + case status, meta line (dates,
// customer, countries) and the actions Edit / Send to customer; day cards with a cream header and the
// gold day number; side panels Overview and Price.
//
// Data: GET /booking/trip-cases/{code} + GET …/versions/{n}/items. Send = the shared send modal
// (POST …/versions/{n}/send), which marks the version sent and locks it; it never claims an e-mail.
// CIS day text, photos, inclusions and the map join when GET /booking/catalog/tours/{id} exists (§6.2);
// until then the preview shows exactly what Booking stores and says what is missing. Download PDF is W2.
// AA-835: an item that books a hotel room or a service shows the description and highlights CIS published for it
// (the API sends them only while published; draft / stale content never reaches a proposal).

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, CalendarDays, Lock, MapPin, Pencil, Send, User } from "lucide-react";
import { ApiError, api } from "@/lib/api/client";
import { settingsFromApi } from "@/lib/bookingConfig";
import {
  Button,
  Code,
  DateText,
  EmptyState,
  FromCisTag,
  Money,
  Section,
  TripCaseStatusBadge,
  VersionBadgePill,
  formatDay,
  useCan,
} from "@/ui";
import { PageError, PageSkeleton } from "@/ui/booking/PageState";
import { dayDate, itemsByDay, useVersion } from "@/app/(staff)/_parts/useVersion";
import { SendProposalModal } from "@/app/(staff)/proposals/_parts/SendProposalModal";

const SENDABLE_FROM = ["discovery", "building", "proposal_sent"];

function longDate(d: Date | null): string | null {
  if (!d) return null;
  return new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).format(d);
}

export default function VersionPreviewPage({ params }: { params: Promise<{ code: string; n: string }> }) {
  const { code: rawCode, n: rawN } = use(params);
  const code = decodeURIComponent(rawCode);
  const n = Number(rawN);
  const { tripCase, version } = useVersion(code, n);
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.reference.settings, staleTime: 10 * 60_000 });
  const canWrite = useCan("version.write");
  const [sendOpen, setSendOpen] = useState(false);
  const router = useRouter();

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
  const c = tripCase.data;
  const v = version.data;
  const cfg = settingsFromApi(settings.data?.values);
  const days = itemsByDay(v.items);
  const locked = v.is_sent_to_customer || v.is_approved;
  // AA-835: supply descriptions are read live from CIS, not stored with the version (snapshot comes with e-mail / PDF).
  const liveSupplyText = v.items.some((it) => it.supply?.description);
  const canSend = canWrite && !locked && SENDABLE_FROM.includes(c.status);
  const builderHref = `/cases/${encodeURIComponent(c.code)}/proposals/${v.version_number}`;
  const tripName = v.title ?? c.trip_title ?? "Custom trip";
  const dates = c.start_date
    ? `${formatDay(c.start_date)}${c.trip_end_date ? ` – ${formatDay(c.trip_end_date)}` : ""}${c.duration_days ? ` (${c.duration_days} days)` : ""}`
    : "Dates to be confirmed";
  const sales = c.assignees.find((a) => a.role === "sales")?.name ?? null;

  return (
    <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
      <div className="mx-auto flex max-w-[1280px] flex-col gap-5">
        {/* Header bar (Figma: back arrow + "Itinerary Preview") */}
        <div className="flex items-center gap-3 border-b border-line pb-3">
          <Link
            href={builderHref}
            aria-label="Back to the builder"
            className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-line bg-card text-muted hover:text-ink"
          >
            <ArrowLeft size={16} />
          </Link>
          <p className="text-[15px] font-medium text-ink">Proposal preview</p>
          <VersionBadgePill badge={v.badge} />
        </div>

        {/* Title row */}
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-display text-2xl font-semibold text-ink sm:text-3xl">
                <span className="font-mono text-xl text-muted sm:text-2xl">{c.code}</span> · {tripName}
              </h1>
              <TripCaseStatusBadge status={c.status} />
            </div>
            <p className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-muted">
              <span className="inline-flex items-center gap-1.5">
                <CalendarDays size={14} /> {dates}
              </span>
              <span className="inline-flex items-center gap-1.5">
                <User size={14} /> {c.customer_name ?? "Unnamed customer"}
              </span>
              {c.countries.length > 0 && (
                <span className="inline-flex items-center gap-1.5">
                  <MapPin size={14} /> {c.countries.join(", ")}
                </span>
              )}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {canWrite && !locked && (
              <Button variant="secondary" onClick={() => router.push(builderHref)}>
                <Pencil size={14} /> Edit
              </Button>
            )}
            {canSend && (
              <Button variant="primary" onClick={() => setSendOpen(true)}>
                <Send size={14} /> Send to customer
              </Button>
            )}
          </div>
        </div>

        {locked && (
          <div role="status" className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-4 py-3 text-[13px] text-body">
            <Lock size={15} className="shrink-0 text-accent-deep" />
            {v.is_approved ? (
              <span>Approved by the customer: this version is locked.</span>
            ) : (
              <span>
                Marked as sent on <DateText value={v.sent_at} />: this version is locked. Booking does not send e-mail yet.
              </span>
            )}
            {liveSupplyText && <span className="text-muted">Hotel and service descriptions show what CIS publishes today.</span>}
          </div>
        )}

        <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]">
          {/* ── Day by day ── */}
          <div className="flex flex-col gap-4">
            {days.length === 0 ? (
              <div className="rounded-lg border border-line bg-card p-5">
                <EmptyState
                  title="This version has no days yet"
                  description="Add items in the builder; they appear here as the customer will read them."
                  action={
                    canWrite && !locked ? (
                      <Button variant="primary" onClick={() => router.push(builderHref)}>
                        <Pencil size={14} /> Open the builder
                      </Button>
                    ) : undefined
                  }
                />
              </div>
            ) : (
              days.map((d) => {
                const date = dayDate(c.start_date, d.day);
                const headline = d.items[0]?.title ?? "Free day";
                return (
                  <article key={d.day} className="overflow-hidden rounded-lg border border-line bg-card">
                    <header className="flex items-start justify-between gap-3 bg-panel px-5 py-4">
                      <div className="min-w-0">
                        <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Day {d.day}</p>
                        <h2 className="mt-0.5 truncate text-[17px] font-medium text-ink">{headline}</h2>
                      </div>
                      {date && <span className="shrink-0 text-[13px] text-muted">{formatDay(date.toISOString().slice(0, 10))}</span>}
                    </header>
                    <div className="flex gap-4 px-5 py-4">
                      <div className="hidden w-10 shrink-0 text-center sm:block" aria-hidden="true">
                        <p className="font-display text-2xl font-semibold text-accent">{d.day}</p>
                        <p className="text-[11px] uppercase tracking-[0.08em] text-muted">Day</p>
                      </div>
                      <div className="flex min-w-0 flex-1 flex-col gap-2">
                        {date && <p className="text-[13px] text-muted">{longDate(date)}</p>}
                        {d.items.length === 0 ? (
                          <p className="text-sm text-muted">A free day to explore at your own pace.</p>
                        ) : (
                          d.items.map((it) => (
                            <div key={it.id} className="rounded-md bg-panel px-4 py-3">
                              <div className="flex flex-wrap items-center gap-2">
                                <p className="text-sm font-medium text-ink">{it.title}</p>
                                {it.day_to > it.day_from && (
                                  <span className="text-xs text-muted">
                                    day {d.day - it.day_from + 1} of {it.day_to - it.day_from + 1}
                                  </span>
                                )}
                                {it.source_tour_id && <FromCisTag />}
                              </div>
                              {it.notes && <p className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-body">{it.notes}</p>}
                              {it.supply?.description && (
                                <div className="mt-1.5 text-[13px] leading-relaxed text-body">
                                  <p className="whitespace-pre-line">{it.supply.description}</p>
                                  {(it.supply.highlights?.length ?? 0) > 0 && (
                                    <ul className="mt-1 list-disc pl-5">
                                      {(it.supply.highlights ?? []).map((h) => (
                                        <li key={h}>{h}</li>
                                      ))}
                                    </ul>
                                  )}
                                </div>
                              )}
                            </div>
                          ))
                        )}
                      </div>
                    </div>
                  </article>
                );
              })
            )}
            {days.length > 0 && (
              <p className="text-xs text-muted">
                Tour descriptions, photos and the map come from CIS once the catalog endpoint (GET /booking/catalog/tours) is on the
                Booking API; this preview shows the items and notes Booking stores today.
              </p>
            )}
          </div>

          {/* ── Side panels ── */}
          <div className="flex flex-col gap-5">
            <Section title="Overview">
              <dl className="flex flex-col gap-3 text-[13px]">
                <div>
                  <dt className="text-xs text-muted">Trip case</dt>
                  <dd className="mt-0.5 text-ink">
                    <Link href={`/cases/${encodeURIComponent(c.code)}`} className="hover:underline">
                      <Code>{c.code}</Code>
                    </Link>
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">Customer</dt>
                  <dd className="mt-0.5 text-ink">{c.customer_name ?? "—"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">Dates</dt>
                  <dd className="mt-0.5 text-ink">{dates}</dd>
                </div>
                {c.pax ? (
                  <div>
                    <dt className="text-xs text-muted">Travellers</dt>
                    <dd className="mt-0.5 text-ink">{c.pax}</dd>
                  </div>
                ) : null}
                <div>
                  <dt className="text-xs text-muted">Destinations</dt>
                  <dd className="mt-0.5 text-ink">{c.countries.length ? c.countries.join(", ") : "—"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">Status</dt>
                  <dd className="mt-1">
                    <TripCaseStatusBadge status={c.status} />
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">Sales</dt>
                  <dd className="mt-0.5 text-ink">{sales ?? "Unassigned"}</dd>
                </div>
              </dl>
            </Section>
            <Section title="Price">
              {v.total_price == null ? (
                <p className="text-[13px] text-muted">
                  No price on this version yet. Enter the net costs in the builder, save, then use Apply price.
                </p>
              ) : (
                <>
                  <p className="font-display text-2xl font-semibold text-ink">
                    <Money amount={v.total_price} currency={v.currency} strong />
                  </p>
                  <p className="mt-1 text-xs text-muted">
                    {v.valid_until ? (
                      <>
                        Valid until <DateText value={v.valid_until} />
                      </>
                    ) : (
                      `Valid for ${cfg.quoteValidityDays} days once sent`
                    )}
                    {` · deposit ${cfg.depositPct}% on confirmation`}
                  </p>
                </>
              )}
            </Section>
          </div>
        </div>
      </div>
      <SendProposalModal open={sendOpen} onClose={() => setSendOpen(false)} code={c.code} n={v.version_number} />
    </main>
  );
}
