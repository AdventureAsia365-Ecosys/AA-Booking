"use client";
// app/(staff)/hotels/_parts/HotelDrawer.tsx — one hotel in a side drawer (AA-823, bk-010; Figma HOTEL MANAGEMENT 07):
// facts, room types (add / edit / deactivate), the net rates entered under the supplier's contracts (read here; they
// are written on the supplier page → Rates) and recent activity. Edit / Deactivate need supply.write; sales read.
// Customer-facing description and photos are CIS content, not edited here (ADR 0004a, Q17: none in v1).

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, BedDouble, Pencil, Plus, Power } from "lucide-react";
import { supplyApi, type HotelDetail, type RoomType } from "@/lib/api/supply";
import { Button, Code, DateText, Drawer, ErrorState, FromCisTag, Pill, Skeleton, useToast } from "@/ui";
import { Fact, FactGrid } from "@/app/(staff)/_parts/DetailHeader";
import { RatesTable, Stars, supplyError } from "@/app/(staff)/_parts/supply";
import { useRecordStatus } from "@/app/(staff)/_parts/useRecordStatus";
import { SupplierStatusBadge } from "@/app/(staff)/suppliers/_parts/shared";
import { HotelFormDrawer } from "./HotelFormDrawer";
import { RoomTypeDrawer } from "./RoomTypeDrawer";

export function HotelDrawer({ hotelRef, onClose }: { hotelRef: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ["hotel", hotelRef], queryFn: () => supplyApi.hotel(hotelRef) });
  const status = useRecordStatus();
  const [editing, setEditing] = useState(false);
  const h = q.data;
  return (
    <Drawer open onClose={onClose} title={h ? h.name : "Hotel"} width={880}>
      {q.isLoading ? (
        <div className="flex flex-col gap-3" aria-label="Loading hotel">
          <Skeleton w="60%" h={18} />
          <Skeleton h={120} />
          <Skeleton h={160} />
        </div>
      ) : q.isError || !h ? (
        <ErrorState message={supplyError(q.error, "Could not load the hotel.") ?? ""} onRetry={() => q.refetch()} />
      ) : (
        <div className="flex flex-col gap-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2 text-[13px] text-body">
              <SupplierStatusBadge status={h.status} />
              <Code>{h.code}</Code>
              <Stars value={h.star_rating} />
              <span>
                via{" "}
                <Link href={`/suppliers/${encodeURIComponent(h.supplier_code)}`} className="font-medium text-accent-deep hover:underline">
                  {h.supplier_name}
                </Link>
              </span>
            </div>
            {status.canWrite && (
              <div className="flex gap-2">
                <Button size="sm" onClick={() => setEditing(true)}>
                  <Pencil size={13} /> Edit
                </Button>
                <Button size="sm" variant={h.status === "active" ? "danger" : "secondary"} onClick={() => status.ask("hotel", { code: h.code, name: h.name }, h.status !== "active")}>
                  {h.status === "active" ? <Ban size={13} /> : <Power size={13} />} {h.status === "active" ? "Deactivate" : "Activate"}
                </Button>
              </div>
            )}
          </div>
          {h.status === "inactive" && (
            <p role="status" className="rounded-lg border border-line bg-panel px-4 py-3 text-[13px] text-body">
              This hotel is inactive: it stays on record, but proposals cannot newly book its rooms.
            </p>
          )}
          <Facts h={h} />
          <RoomTypes h={h} canWrite={status.canWrite} />
          <HotelRates h={h} />
          <Activity h={h} />
        </div>
      )}
      {editing && h && <HotelFormDrawer hotel={h} onClose={() => setEditing(false)} />}
      {status.dialogs}
    </Drawer>
  );
}

function Block({ title, actions, children }: { title: string; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="min-w-0">
      <header className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-[13px] font-semibold uppercase tracking-[0.04em] text-muted">{title}</h3>
        {actions}
      </header>
      {children}
    </section>
  );
}

function Facts({ h }: { h: HotelDetail }) {
  const time = (t: string | null) => (t ? t.slice(0, 5) : "—");
  return (
    <Block title="Facts">
      <div className="rounded-lg border border-line bg-card p-4">
        <FactGrid>
          <Fact label="Style">{h.hotel_style ?? "—"}</Fact>
          <Fact label="Star rating">
            <Stars value={h.star_rating} />
          </Fact>
          <Fact label="City / place">
            {h.destination ? (
              <span className="inline-flex flex-wrap items-center gap-2">
                {h.destination.name} <FromCisTag />
              </span>
            ) : h.destination_id ? (
              <span className="text-muted">Set, but the CIS catalog cannot be read right now</span>
            ) : (
              "—"
            )}
          </Fact>
          <Fact label="Country">{h.country_name ?? "—"}</Fact>
          <Fact label="Street address" wide>
            {h.address ?? "—"}
          </Fact>
          <Fact label="Geo point">{h.latitude != null ? `${h.latitude}, ${h.longitude}` : "—"}</Fact>
          <Fact label="Check-in / check-out">
            {time(h.check_in_time)} / {time(h.check_out_time)}
          </Fact>
          <Fact label="Amenities" wide>
            {h.amenities.length ? (
              <span className="inline-flex flex-wrap gap-1">
                {h.amenities.map((a) => (
                  <span key={a} className="rounded-full border border-line bg-panel px-2 py-0.5 text-[11.5px] font-medium text-body">
                    {a}
                  </span>
                ))}
              </span>
            ) : (
              "—"
            )}
          </Fact>
          <Fact label="Staff links" wide>
            {h.links.length ? (
              <span className="flex flex-col gap-1">
                {h.links.map((l) => (
                  <a key={l.url} href={l.url} target="_blank" rel="noopener noreferrer" className="break-all text-accent-deep hover:underline">
                    {l.label}
                  </a>
                ))}
              </span>
            ) : (
              "—"
            )}
          </Fact>
          <Fact label="Internal notes" wide>
            {h.internal_notes ? <span className="whitespace-pre-line">{h.internal_notes}</span> : <span className="text-muted">None</span>}
          </Fact>
          <Fact label="Description and photos" wide>
            <span className="text-muted">Written in CIS from these facts (not in Booking).</span>
          </Fact>
        </FactGrid>
      </div>
    </Block>
  );
}

function RoomTypes({ h, canWrite }: { h: HotelDetail; canWrite: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [drawer, setDrawer] = useState<{ room?: RoomType } | null>(null);
  const toggle = useMutation({
    mutationFn: (r: RoomType) => supplyApi.updateRoomType(r.id, { status: r.status === "active" ? "inactive" : "active" }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["hotel"] });
      qc.invalidateQueries({ queryKey: ["hotels"] });
      toast.success(`${r.name} ${r.status === "active" ? "activated" : "deactivated"}.`);
    },
    onError: (err) => toast.error(supplyError(err) ?? "Could not change the room type."),
  });
  return (
    <Block
      title={`Room types (${h.room_types.length})`}
      actions={
        canWrite &&
        h.status === "active" && (
          <Button size="sm" variant="primary" onClick={() => setDrawer({})}>
            <Plus size={13} /> Add room type
          </Button>
        )
      }
    >
      {h.room_types.length === 0 ? (
        <p className="rounded-lg border border-dashed border-line px-4 py-5 text-center text-[13px] text-muted">
          No room types yet.{canWrite ? " Add the rooms this hotel sells; rates are entered per room type." : ""}
        </p>
      ) : (
        <ul className="divide-y divide-line2 rounded-lg border border-line bg-card">
          {h.room_types.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
              <span className="flex min-w-0 items-start gap-3">
                <BedDouble size={16} className="mt-0.5 shrink-0 text-muted2" />
                <span className="min-w-0">
                  <span className="block text-[13.5px] font-medium text-ink">{r.name}</span>
                  <span className="text-xs text-muted">
                    {r.max_occupancy ? `Up to ${r.max_occupancy}` : "Occupancy not set"}
                    {r.bed_types.length ? ` · ${r.bed_types.join(", ")}` : ""} · {r.rates_in_force} rate{r.rates_in_force === 1 ? "" : "s"} in force
                  </span>
                </span>
              </span>
              <span className="flex items-center gap-2">
                {r.status === "inactive" && <Pill tone="neutral">Inactive</Pill>}
                {canWrite && (
                  <>
                    <Button size="sm" variant="ghost" onClick={() => setDrawer({ room: r })} ariaLabel={`Edit ${r.name}`}>
                      <Pencil size={13} />
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => toggle.mutate(r)} disabled={toggle.isPending}>
                      {r.status === "active" ? "Deactivate" : "Activate"}
                    </Button>
                  </>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {drawer && <RoomTypeDrawer hotelRef={h.code} room={drawer.room} onClose={() => setDrawer(null)} />}
    </Block>
  );
}

function HotelRates({ h }: { h: HotelDetail }) {
  const rates = useQuery({ queryKey: ["rates", "hotel", h.code], queryFn: () => supplyApi.hotelRates(h.code) });
  return (
    <Block title="Net rates">
      <RatesTable
        tableId="hotel-rates"
        rates={rates.data?.items ?? []}
        loading={rates.isLoading}
        error={rates.isError ? (rates.error as Error).message : null}
        onRetry={() => rates.refetch()}
        showContract
        compact
        emptyTitle="No rates yet"
        emptyDescription={`Rates are entered under a contract on the supplier page (${h.supplier_name} → Rates).`}
      />
    </Block>
  );
}

function Activity({ h }: { h: HotelDetail }) {
  return (
    <Block title="Recent activity">
      {h.recent_activity.length === 0 ? (
        <p className="text-[13px] text-muted">Nothing recorded yet.</p>
      ) : (
        <ol className="flex flex-col">
          {h.recent_activity.map((a) => (
            <li key={a.id} className="border-b border-line2 py-2 text-[13px] last:border-b-0">
              <span className="font-medium text-ink">{a.summary ?? a.action}</span>
              {a.entity_type === "hotel_room_type" && a.entity_label && <span className="text-muted"> · {a.entity_label}</span>}
              <span className="block text-xs text-muted">
                {a.actor.name ?? (a.actor.id ? "Staff" : "System")} · <DateText value={a.at} withTime />
              </span>
            </li>
          ))}
        </ol>
      )}
    </Block>
  );
}
