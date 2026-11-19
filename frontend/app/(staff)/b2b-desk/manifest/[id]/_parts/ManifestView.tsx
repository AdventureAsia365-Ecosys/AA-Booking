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
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./ManifestView.messages";

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
  const t = useT(MSG);
  const tx = useTx();
  // ops.read is checked by the API (a 403 shows as the page error).
  const q = useQuery({ queryKey: ["manifest", departureId], queryFn: () => agencyOpsApi.manifest(departureId) });
  const [printing, setPrinting] = useState(false);
  if (q.isLoading) return <PageSkeleton side={false} />;
  if (q.isError || !q.data) return <PageError error={q.error} onRetry={() => q.refetch()} what={t("the manifest")} />;
  const m = q.data;
  const title = m.departure.title ?? t("Tour {0}", m.departure.source_id);

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
      title={t("Departure manifest")}
      description={t("{0} · counts only; traveller names stay with the agencies.", title)}
      actions={
        <Button variant="ghost" disabled={printing} onClick={print}>
          <Printer size={14} /> {printing ? t("Preparing…") : t("Print")}
        </Button>
      }
    >
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiTile label={t("Departure")} value={<DateText value={m.departure.start_date} />} hint={tx(DEP_LABEL[m.departure.status])} hintTone="neutral" />
        <KpiTile
          label={t("Seats sold")}
          value={`${m.totals.seats} / ${m.departure.capacity}`}
          icon={<Users size={16} />}
          hint={m.departure.min_to_operate ? t("Minimum {0}", m.departure.min_to_operate) : t("No minimum")}
          hintTone="neutral"
        />
        <KpiTile label={t("Travellers")} value={m.totals.adult + m.totals.child + m.totals.infant} hint={tx(paxText(m.totals))} hintTone="neutral" />
        <KpiTile label={t("Rooms")} value={m.rooms.reduce((n, r) => n + r.count, 0)} hint={rooms(m.rooms)} hintTone="neutral" />
      </div>
      <Section title={t("Bookings")}>
        <LightTable<ManifestBooking>
          label={t("Bookings on the departure")}
          rows={m.bookings}
          rowKey={(b) => `${b.trip_case_id}:${b.line_ref}`}
          empty={<p className="text-[13px] text-muted">{t("No confirmed booking on this departure.")}</p>}
          columns={[
            {
              id: "agency",
              header: t("Agency"),
              width: "minmax(0,1.6fr)",
              cell: (b) => (
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-medium text-ink">{b.agency}</span>
                  <span className="text-xs text-muted">
                    {t("Ref {0}{1}", b.agency_ref, b.entered_via === "staff" ? t(" · entered by AA") : "")}
                  </span>
                </span>
              ),
            },
            {
              id: "case",
              header: t("Booking"),
              cell: (b) => (
                <a className="font-medium text-accent-deep hover:underline" href={`/cases/${encodeURIComponent(b.trip_case_code)}`}>
                  {b.trip_case_code}
                </a>
              ),
            },
            { id: "pax", header: t("Travellers"), width: "minmax(0,1.4fr)", cell: (b) => paxText(b.travellers) },
            { id: "rooms", header: t("Rooms"), cell: (b) => rooms(b.rooms) },
            { id: "status", header: t("Status"), cell: (b) => <Pill tone={LINE_TONE[b.line_status]}>{tx(LINE_LABEL[b.line_status])}</Pill> },
          ]}
        />
      </Section>
      <Section title={t("Supplier bookings")}>
        <LightTable<SupplierRow>
          label={t("Supplier bookings of the departure")}
          rows={m.supplier_bookings}
          rowKey={(s) => s.code}
          empty={<p className="text-[13px] text-muted">{t("No supplier booking yet.")}</p>}
          columns={[
            {
              id: "service",
              header: t("Service"),
              width: "minmax(0,1.8fr)",
              cell: (s) => (
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-medium text-ink">{s.service}</span>
                  <span className="text-xs text-muted">
                    {s.code} · {s.trip_case_code ?? t("whole departure")}
                  </span>
                </span>
              ),
            },
            { id: "supplier", header: t("Supplier"), cell: (s) => s.supplier },
            {
              id: "when",
              header: t("When"),
              cell: (s) => (
                <span>
                  {s.service_date ? <DateText value={s.service_date} /> : "—"}
                  {s.service_time ? ` ${s.service_time}` : ""}
                </span>
              ),
            },
            { id: "pickup", header: t("Pick-up"), cell: (s) => s.pickup_point ?? "—" },
            { id: "status", header: t("Status"), cell: (s) => `${s.status}${s.confirmation_ref ? ` · ${s.confirmation_ref}` : ""}` },
          ]}
        />
      </Section>
    </PageFrame>
  );
}
