"use client";
// app/(staff)/departures/_parts/shared.tsx — small parts of the departure board (AA-1073 rest): the status pills and
// the occupancy bar (sold, held, kept for allotments, free), used by the calendar, the table and the drawer.

import { occupancy, type BoardDeparture } from "@/lib/api/departureBoard";
import { Pill, type BookingTone } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./shared.messages";

export const STATUS_META: Record<BoardDeparture["status"], { label: string; tone: BookingTone }> = {
  scheduled: { label: "Scheduled", tone: "neutral" },
  guaranteed: { label: "Guaranteed", tone: "success" },
  cancelled: { label: "Cancelled", tone: "danger" },
};

export function DepartureStatus({ d }: { d: Pick<BoardDeparture, "status" | "at_risk" | "open_incident"> }) {
  const t = useT(MSG);
  const tx = useTx();
  return (
    <span className="flex flex-wrap gap-1">
      <Pill tone={STATUS_META[d.status].tone}>{tx(STATUS_META[d.status].label)}</Pill>
      {d.at_risk && <Pill tone="warning">{t("At risk")}</Pill>}
      {d.open_incident && <Pill tone="danger">{t("Incident")}</Pill>}
    </span>
  );
}

/** Seats as a stacked bar: sold (accent), held (accent, lighter), kept for allotments (muted), free (track). */
export function SeatsBar({ d }: { d: Pick<BoardDeparture, "capacity" | "sold" | "held" | "allotment_unused" | "occupancy_pct"> }) {
  const t = useT(MSG);
  const cap = Math.max(d.capacity, 1);
  const pct = (n: number) => `${Math.min(100, (n / cap) * 100)}%`;
  const occ = occupancy(d);
  return (
    <span className="flex min-w-[120px] items-center gap-2" title={t("{0} sold, {1} held, {2} kept for allotments of {3}", d.sold, d.held, d.allotment_unused, d.capacity)}>
      <span className="relative flex h-2 flex-1 overflow-hidden rounded-full bg-line2" aria-hidden="true">
        <span className="h-full bg-accent" style={{ width: pct(d.sold) }} />
        <span className="h-full bg-accent opacity-50" style={{ width: pct(d.held) }} />
        <span className="h-full bg-muted2 opacity-60" style={{ width: pct(d.allotment_unused) }} />
      </span>
      <span className="bk-tabular w-10 text-right text-xs text-muted">{occ === null ? "—" : `${Math.round(occ)} %`}</span>
    </span>
  );
}
