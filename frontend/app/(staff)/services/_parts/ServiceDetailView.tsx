"use client";
// app/(staff)/services/_parts/ServiceDetailView.tsx — one service (W2, AA-823; Figma ACTIVITY DETAILS 07): header
// (title, status, code, type, place), Edit / Deactivate | Activate (supply.write), tabs Overview (facts, supplier,
// recent activity) and Rates (net rates under the supplier's contracts; written on the supplier page → Rates).
// AA-835: Overview also shows the customer content CIS published (read-only, "Edit in CIS"; hidden until CIS serves
// it) and the supplier photos (upload / usage rights / remove with supply.write).
// "Used in" (proposal items booking it) arrives with the proposal reports. Reads need supply.read.

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Activity as ActivityIcon, Ban, CalendarDays, Hash, MapPin, Pencil, Power, Tag } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { supplyApi, type ServiceDetail } from "@/lib/api/supply";
import { Button, Code, DateText, EmptyState, ErrorState, FromCisTag, Section } from "@/ui";
import { DetailHeader, DetailSkeleton, Fact, FactGrid, MetaItem, useDetailTab } from "@/app/(staff)/_parts/DetailHeader";
import { RatesTable, SERVICE_TYPE_LABEL, UNIT_LABEL } from "@/app/(staff)/_parts/supply";
import { useRecordStatus } from "@/app/(staff)/_parts/useRecordStatus";
import { SupplierStatusBadge } from "@/app/(staff)/suppliers/_parts/shared";
import { CisContentPanel, SupplierPhotos } from "@/app/(staff)/_parts/SupplyContent";
import { ServiceFormDrawer } from "./ServiceFormDrawer";

export function ServiceDetailView({ refParam }: { refParam: string }) {
  const ref = decodeURIComponent(refParam);
  const q = useQuery({ queryKey: ["service", ref], queryFn: () => supplyApi.service(ref) });
  const status = useRecordStatus();
  const [editing, setEditing] = useState(false);
  const tabs = [
    { key: "overview", label: "Overview" },
    { key: "rates", label: "Rates", count: q.data?.rates_in_force },
  ];
  const [tab, setTab] = useDetailTab(tabs);

  if (q.isLoading) return <DetailSkeleton label="Loading service" />;
  if (q.isError || !q.data) {
    const code = q.error instanceof ApiError ? q.error.status : 0;
    return (
      <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
        {code === 404 ? (
          <EmptyState
            icon={<ActivityIcon size={30} />}
            title="Service not found"
            description="The link may be wrong, or the code was mistyped."
            action={<Link href="/services" className="text-[13px] font-semibold text-accent-deep hover:underline">Back to services</Link>}
          />
        ) : code === 403 ? (
          <EmptyState title="You do not have access to services" description="Your Booking role cannot read supply records." />
        ) : (
          <ErrorState message={(q.error as Error)?.message ?? "Could not load the service."} onRetry={() => q.refetch()} />
        )}
      </main>
    );
  }

  const s = q.data;
  return (
    <main className="aa-admin-main flex-1 overflow-y-auto">
      <DetailHeader
        back={{ href: "/services", label: "Services" }}
        name={s.title}
        badges={<SupplierStatusBadge status={s.status} />}
        meta={
          <>
            <MetaItem icon={<Hash size={14} />}>
              <Code>{s.code}</Code>
            </MetaItem>
            <MetaItem icon={<Tag size={14} />}>
              {SERVICE_TYPE_LABEL[s.service_type]} · {UNIT_LABEL[s.unit]}
            </MetaItem>
            {(s.destination || s.country_name) && (
              <MetaItem icon={<MapPin size={14} />}>{s.destination ? `${s.destination.name}, ${s.country_name ?? s.destination.country}` : s.country_name}</MetaItem>
            )}
            <MetaItem icon={<CalendarDays size={14} />}>
              Added on <DateText value={s.created_at} />
            </MetaItem>
          </>
        }
        actions={
          status.canWrite && (
            <>
              <Button variant="secondary" onClick={() => setEditing(true)}>
                <Pencil size={14} /> Edit service
              </Button>
              <Button variant="secondary" onClick={() => status.ask("service", { code: s.code, name: s.title }, s.status !== "active")}>
                {s.status === "active" ? <Ban size={14} /> : <Power size={14} />} {s.status === "active" ? "Deactivate" : "Activate"}
              </Button>
            </>
          )
        }
        tabs={tabs}
        activeTab={tab}
        onTab={setTab}
      />
      <div className="mx-auto max-w-[1440px] py-6 md:px-8">
        {s.status === "inactive" && (
          <div role="status" className="mb-5 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-4 py-3 text-[13px] text-body">
            <Ban size={15} /> This service is inactive: it stays on record, but proposals cannot newly book it.
          </div>
        )}
        {tab === "overview" && <Overview s={s} canWrite={status.canWrite} />}
        {tab === "rates" && <Rates s={s} />}
      </div>
      {editing && <ServiceFormDrawer service={s} onClose={() => setEditing(false)} />}
      {status.dialogs}
    </main>
  );
}

function Overview({ s, canWrite }: { s: ServiceDetail; canWrite: boolean }) {
  const minutes = s.duration_minutes;
  const duration = minutes == null ? "—" : minutes >= 60 ? `${Math.floor(minutes / 60)} h${minutes % 60 ? ` ${minutes % 60} min` : ""}` : `${minutes} min`;
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <div className="flex min-w-0 flex-col gap-5">
        <Section title="Overview">
          <FactGrid>
            <Fact label="Type">{SERVICE_TYPE_LABEL[s.service_type]}</Fact>
            <Fact label="Rates are">{UNIT_LABEL[s.unit]}</Fact>
            <Fact label="City / place">
              {s.destination ? (
                <span className="inline-flex flex-wrap items-center gap-2">
                  {s.destination.name} <FromCisTag />
                </span>
              ) : s.destination_id ? (
                <span className="text-muted">Set, but the CIS catalog cannot be read right now</span>
              ) : (
                "—"
              )}
            </Fact>
            <Fact label="Country">{s.country_name ?? "—"}</Fact>
            <Fact label="Max group size">{s.max_group_size ?? "—"}</Fact>
            <Fact label="Duration">{duration}</Fact>
            <Fact label="Inclusions" wide>
              {s.inclusions.length ? s.inclusions.join(", ") : "—"}
            </Fact>
            <Fact label="CIS catalog reference">{s.source_id ? <Code>{s.source_id}</Code> : "—"}</Fact>
            <Fact label="Customer-facing text">
              <span className="text-muted">Written in CIS (not in Booking).</span>
            </Fact>
            <Fact label="Internal notes" wide>
              {s.internal_notes ? <span className="whitespace-pre-line">{s.internal_notes}</span> : <span className="text-muted">None</span>}
            </Fact>
          </FactGrid>
        </Section>
        <CisContentPanel content={s.content} />
        <SupplierPhotos kind="service" recordRef={s.code} recordActive={s.status === "active"} canWrite={canWrite} />
      </div>
      <div className="flex min-w-0 flex-col gap-5">
        <Section title="Supplier">
          <FactGrid>
            <Fact label="Supplier" wide>
              <Link href={`/suppliers/${encodeURIComponent(s.supplier_code)}`} className="font-medium text-accent-deep hover:underline">
                {s.supplier_name}
              </Link>{" "}
              <Code>{s.supplier_code}</Code>
            </Fact>
          </FactGrid>
        </Section>
        <Section title="Recent activity">
          {s.recent_activity.length === 0 ? (
            <p className="text-[13px] text-muted">Nothing recorded yet.</p>
          ) : (
            <ol className="flex flex-col">
              {s.recent_activity.map((a) => (
                <li key={a.id} className="border-b border-line2 py-2 text-[13px] first:pt-0 last:border-b-0">
                  <span className="font-medium text-ink">{a.summary ?? a.action}</span>
                  {a.entity_type === "supplier_rate" && a.entity_label && <span className="text-muted"> · {a.entity_label}</span>}
                  <span className="block text-xs text-muted">
                    {a.actor.name ?? (a.actor.id ? "Staff" : "System")} · <DateText value={a.at} withTime />
                  </span>
                </li>
              ))}
            </ol>
          )}
        </Section>
      </div>
    </div>
  );
}

function Rates({ s }: { s: ServiceDetail }) {
  const rates = useQuery({ queryKey: ["rates", "service", s.code], queryFn: () => supplyApi.serviceRates(s.code) });
  return (
    <Section padded={false} className="p-4" title="Net rates" description="Entered under the supplier's contracts; fixed once a contract is published.">
      <RatesTable
        tableId="service-rates"
        rates={rates.data?.items ?? []}
        loading={rates.isLoading}
        error={rates.isError ? (rates.error as Error).message : null}
        onRetry={() => rates.refetch()}
        showTarget={false}
        showContract
        emptyTitle="No rates yet"
        emptyDescription={`Rates are entered under a contract on the supplier page (${s.supplier_name} → Rates).`}
      />
    </Section>
  );
}
