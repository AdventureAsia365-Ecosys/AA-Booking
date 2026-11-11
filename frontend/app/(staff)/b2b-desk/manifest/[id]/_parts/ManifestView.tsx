"use client";
// app/(staff)/b2b-desk/manifest/[id]/_parts/ManifestView.tsx — the operations manifest of one departure (AA-1075 bk-13
// step 1, GET /booking/b2b-desk/departures/{id}/manifest, ops.read).
//
// Every confirmed agency booking on the departure (agency, its reference, the trip case, travellers by age band, the
// room set-up, the line status), then the supplier bookings of those cases and of the departure (time, pick-up,
// confirmation ref). Counts only: traveller names stay with the agencies (DM-6.8); a rooming list with names waits
// for the data-standard amendment the S275 study names. "Print" opens the server's printable page.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Printer, Users } from "lucide-react";
import { agencyOpsApi, openBlob, paxText, type Manifest, type ManifestBooking } from "@/lib/api/agencyOps";
import { Button, DateText, KpiTile, LightTable, PageFrame, Pill, Section, type BookingTone } from "@/ui";
import { PageError, PageSkeleton } from "@/ui/booking/PageState";

type SupplierRow = Manifest["supplier_bookings"][number];

const LINE_TONE: Record<ManifestBooking["line_status"], BookingTone> = {
  confirmed: "success",
  on_request: "warning",
  declined: "danger",
  cancelled: "neutral",
};
const LINE_LABEL: Record<ManifestBooking["line_status"], string> = {
  confirmed: "Confirmed",
  on_request: "On request",
  declined: "Not available",
  cancelled: "Cancelled",
};

const DEP_LABEL: Record<Manifest["departure"]["status"], string> = {
  scheduled: "Scheduled",
  guaranteed: "Guaranteed",
  cancelled: "Cancelled",
};

function rooms(r: { occupancy: string; count: number }[]): string {
  return r.map((x) => `${x.count} x ${x.occupancy}`).join(", ") || "—";
}

export function ManifestView({ departureId }: { departureId: string }) {
  // ops.read is checked by the API (a 403 shows as the page error).
  const q = useQuery({ queryKey: ["manifest", departureId], queryFn: () => agencyOpsApi.manifest(departureId) });
  const [printing, setPrinting] = useState(false);
  if (q.isLoading) return <PageSkeleton side={false} />;
  if (q.isError || !q.data) return <PageError error={q.error} onRetry={() => q.refetch()} what="the manifest" />;
  const m = q.data;
  const title = m.departure.title ?? `Tour ${m.departure.source_id}`;

  async function print() {
    setPrinting(true);
    try {
      openBlob(await agencyOpsApi.manifestHtml(departureId));
    } finally {
      setPrinting(false);
    }
  }

  return (
    <PageFrame
      title="Departure manifest"
      description={`${title} · counts only; traveller names stay with the agencies.`}
      actions={
        <Button variant="ghost" disabled={printing} onClick={print}>
          <Printer size={14} /> {printing ? "Preparing…" : "Print"}
        </Button>
      }
    >
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiTile label="Departure" value={<DateText value={m.departure.start_date} />} hint={DEP_LABEL[m.departure.status]} hintTone="neutral" />
        <KpiTile
          label="Seats sold"
          value={`${m.totals.seats} / ${m.departure.capacity}`}
          icon={<Users size={16} />}
          hint={m.departure.min_to_operate ? `Minimum ${m.departure.min_to_operate}` : "No minimum"}
          hintTone="neutral"
        />
        <KpiTile label="Travellers" value={m.totals.adult + m.totals.child + m.totals.infant} hint={paxText(m.totals)} hintTone="neutral" />
        <KpiTile label="Rooms" value={m.rooms.reduce((n, r) => n + r.count, 0)} hint={rooms(m.rooms)} hintTone="neutral" />
      </div>
      <Section title="Bookings">
        <LightTable<ManifestBooking>
          label="Bookings on the departure"
          rows={m.bookings}
          rowKey={(b) => `${b.trip_case_id}:${b.line_ref}`}
          empty={<p className="text-[13px] text-muted">No confirmed booking on this departure.</p>}
          columns={[
            {
              id: "agency",
              header: "Agency",
              width: "minmax(0,1.6fr)",
              cell: (b) => (
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-medium text-ink">{b.agency}</span>
                  <span className="text-xs text-muted">
                    Ref {b.agency_ref}
                    {b.entered_via === "staff" ? " · entered by AA" : ""}
                  </span>
                </span>
              ),
            },
            {
              id: "case",
              header: "Booking",
              cell: (b) => (
                <a className="font-medium text-accent-deep hover:underline" href={`/cases/${encodeURIComponent(b.trip_case_code)}`}>
                  {b.trip_case_code}
                </a>
              ),
            },
            { id: "pax", header: "Travellers", width: "minmax(0,1.4fr)", cell: (b) => paxText(b.travellers) },
            { id: "rooms", header: "Rooms", cell: (b) => rooms(b.rooms) },
            { id: "status", header: "Status", cell: (b) => <Pill tone={LINE_TONE[b.line_status]}>{LINE_LABEL[b.line_status]}</Pill> },
          ]}
        />
      </Section>
      <Section title="Supplier bookings">
        <LightTable<SupplierRow>
          label="Supplier bookings of the departure"
          rows={m.supplier_bookings}
          rowKey={(s) => s.code}
          empty={<p className="text-[13px] text-muted">No supplier booking yet.</p>}
          columns={[
            {
              id: "service",
              header: "Service",
              width: "minmax(0,1.8fr)",
              cell: (s) => (
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-medium text-ink">{s.service}</span>
                  <span className="text-xs text-muted">
                    {s.code} · {s.trip_case_code ?? "whole departure"}
                  </span>
                </span>
              ),
            },
            { id: "supplier", header: "Supplier", cell: (s) => s.supplier },
            {
              id: "when",
              header: "When",
              cell: (s) => (
                <span>
                  {s.service_date ? <DateText value={s.service_date} /> : "—"}
                  {s.service_time ? ` ${s.service_time}` : ""}
                </span>
              ),
            },
            { id: "pickup", header: "Pick-up", cell: (s) => s.pickup_point ?? "—" },
            { id: "status", header: "Status", cell: (s) => `${s.status}${s.confirmation_ref ? ` · ${s.confirmation_ref}` : ""}` },
          ]}
        />
      </Section>
    </PageFrame>
  );
}
