"use client";
// app/(staff)/cases/[code]/SupplierGroups.tsx — the sold tour of a B2B trip on the Suppliers tab (AA-1149).
// A tour sold to an agency is split into its components as priced (one hotel night, one transfer, one guide day…): the
// "Generate" button makes one supplier booking per component. This part shows the bookings grouped by supplier (how
// many each, the tour days) and the components still without a live booking with why: booked, no booking needed
// (waived with a reason), waiting for the desk to accept the agency line, line declined, supplier not active, or a trip
// sold before tours were split (add its bookings by hand). Staff book a component with its supplier or another one, or
// close it as "no booking needed" (20-500 characters). Kept out of SuppliersTab.tsx (file size and complexity limits).

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { suppliersApi } from "@/lib/api/suppliers";
import {
  supplierBookingsApi,
  tourParts,
  type CaseSupplierBookings,
  type SupplierBooking,
  type UnbookedPart,
} from "@/lib/api/supplierBookings";
import type { TripCaseDetail } from "@/lib/api/types";
import { Button, FormDrawer, Pill, Section, Select, Textarea, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./SupplierGroups.messages";

const WAIVE_MIN = 20;
const SKIP_LABEL: Record<NonNullable<UnbookedPart["skip_reason"]>, string> = {
  booked: "Booked",
  waived: "No booking needed",
  b2b_line_closed: "Line declined or cancelled",
  line_not_confirmed: "Waiting for the desk to accept the line",
  supplier_not_active: "Supplier not active",
  sold_before_split: "Sold before tours were split: add by hand",
};
const OPEN = new Set(["supplier_not_active", null]);

interface Group {
  id: string;
  name: string;
  items: SupplierBooking[];
}

function groups(items: SupplierBooking[]): Group[] {
  const out = new Map<string, Group>();
  for (const b of items.filter((x) => x.live)) {
    const g = out.get(b.supplier.id) ?? { id: b.supplier.id, name: b.supplier.name, items: [] };
    g.items.push(b);
    out.set(b.supplier.id, g);
  }
  return [...out.values()].sort((a, b) => a.name.localeCompare(b.name));
}

function days(items: SupplierBooking[]): string {
  const list = [...new Set(items.map((b) => b.tour_day).filter((d): d is number => d != null))].sort((a, b) => a - b);
  return list.join(", ");
}

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

export function SupplierGroups({
  c,
  data,
  canWrite,
  sold,
  onDone,
}: {
  c: TripCaseDetail;
  data: CaseSupplierBookings | undefined;
  canWrite: boolean;
  sold: boolean;
  onDone: () => void;
}) {
  const t = useT(MSG);
  const [booking, setBooking] = useState<UnbookedPart | null>(null);
  const [waiving, setWaiving] = useState<UnbookedPart | null>(null);
  if (!data || data.trip_case.channel !== "b2b") return null;
  const parts = tourParts(data);
  const byGroup = groups(data.items.filter((b) => b.quote_part_id));
  if (!parts.length && !byGroup.length) return null;
  const writable = canWrite && sold;
  return (
    <Section
      title={t("Tour components")}
      description={t("The tour sold to the agency, split into its components as priced. Later edits of the tour do not change them.")}
    >
      {byGroup.length > 0 && (
        <ul className="mb-3 flex flex-col divide-y divide-line2 text-[13px]" aria-label={t("Bookings by supplier")}>
          {byGroup.map((g) => (
            <li key={g.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <span className="font-medium text-ink">{g.name}</span>
              <span className="text-muted">
                {t("{0} items", g.items.length)} · {t("{0} confirmed", g.items.filter((b) => b.status === "confirmed").length)}
                {days(g.items) && ` · ${t("Tour day {0}", days(g.items))}`}
              </span>
            </li>
          ))}
        </ul>
      )}
      {parts.length > 0 && (
        <PartList parts={parts} writable={writable} onBook={setBooking} onWaive={setWaiving} />
      )}
      {booking && <BookPart c={c} part={booking} onClose={() => setBooking(null)} onDone={onDone} />}
      {waiving && <WaivePart c={c} part={waiving} onClose={() => setWaiving(null)} onDone={onDone} />}
    </Section>
  );
}

function PartList({
  parts,
  writable,
  onBook,
  onWaive,
}: {
  parts: UnbookedPart[];
  writable: boolean;
  onBook: (p: UnbookedPart) => void;
  onWaive: (p: UnbookedPart) => void;
}) {
  const t = useT(MSG);
  return (
    <ul className="flex flex-col divide-y divide-line2 text-[13px]" aria-label={t("Components without a live booking")}>
      {parts.map((p) => (
        <PartRow key={p.quote_part_id ?? `line-${p.position}`} p={p} writable={writable} onBook={onBook} onWaive={onWaive} />
      ))}
    </ul>
  );
}

function PartRow({
  p,
  writable,
  onBook,
  onWaive,
}: {
  p: UnbookedPart;
  writable: boolean;
  onBook: (p: UnbookedPart) => void;
  onWaive: (p: UnbookedPart) => void;
}) {
  const t = useT(MSG);
  const tx = useTx();
  const actionable = writable && !!p.quote_part_id && OPEN.has(p.skip_reason);
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 py-2">
      <span className="min-w-0">
        <span className="font-medium text-ink">{p.tour_day ? t("Tour day {0}", p.tour_day) : p.title}</span>
        {p.supply_label && <> · {p.supply_label}</>}
        {p.supplier && <span className="text-muted"> · {p.supplier.name}</span>}
      </span>
      <span className="flex flex-wrap items-center gap-2">
        {p.skip_reason && (
          <Pill tone="neutral" dot={false} title={p.waive_reason ?? undefined}>
            {tx(SKIP_LABEL[p.skip_reason])}
          </Pill>
        )}
        {actionable && (
          <>
            <Button size="sm" variant="ghost" onClick={() => onBook(p)}>
              {p.bookable ? t("Book") : t("Book with another supplier")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => onWaive(p)}>
              {t("No booking needed")}
            </Button>
          </>
        )}
      </span>
    </li>
  );
}

function BookPart({ c, part, onClose, onDone }: { c: TripCaseDetail; part: UnbookedPart; onClose: () => void; onDone: () => void }) {
  const t = useT(MSG);
  const toast = useToast();
  const suppliers = useQuery({ queryKey: ["suppliers", "active-options"], queryFn: () => suppliersApi.list({ status: "active", limit: 100 }) });
  const [supplier, setSupplier] = useState(part.bookable && part.supplier ? part.supplier.id : "");
  const run = useMutation({
    mutationFn: () => supplierBookingsApi.create(c.code, { supplier_id: supplier, quote_part_id: part.quote_part_id }),
    onSuccess: (b) => {
      toast.success(t("{0} added for {1}", b.code, b.supplier.name));
      onDone();
      onClose();
    },
  });
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("Book a tour component")}
      submitLabel={t("Add booking")}
      busy={run.isPending}
      error={run.isError ? errorText(run.error) : null}
      fieldErrors={run.error instanceof ApiError ? run.error.fieldErrors : undefined}
      onSubmit={() => run.mutate()}
    >
      <div className="flex flex-col gap-4 text-[13px]">
        <p className="text-body">{part.supply_label}</p>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">
            {t("Supplier")} <span className="text-danger">*</span>
          </span>
          <Select size="sm" className="w-full" value={supplier} onChange={(e) => setSupplier(e.target.value)} required>
            <option value="">{t("Choose a supplier…")}</option>
            {(suppliers.data?.items ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.company_name} ({s.code})
              </option>
            ))}
          </Select>
        </label>
        <p className="text-xs text-muted">{t("The date, pax, quantity and net come from the component as sold; edit them on the booking.")}</p>
      </div>
    </FormDrawer>
  );
}

function WaivePart({ c, part, onClose, onDone }: { c: TripCaseDetail; part: UnbookedPart; onClose: () => void; onDone: () => void }) {
  const t = useT(MSG);
  const toast = useToast();
  const [reason, setReason] = useState("");
  const run = useMutation({
    mutationFn: () => supplierBookingsApi.waivePart(c.code, part.quote_part_id ?? "", reason.trim()),
    onSuccess: () => {
      toast.success(t("Marked as no booking needed"));
      onDone();
      onClose();
    },
  });
  const short = reason.trim().length < WAIVE_MIN;
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={t("No booking needed")}
      submitLabel={t("Save")}
      busy={run.isPending || short}
      dirty={reason.length > 0}
      error={run.isError ? errorText(run.error) : null}
      onSubmit={() => run.mutate()}
    >
      <div className="flex flex-col gap-3 text-[13px]">
        <p className="text-body">{part.supply_label}</p>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">{t("Why does it need no supplier booking?")}</span>
          <Textarea className="min-h-[90px] w-full" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          <span className={`text-xs ${short ? "text-danger" : "text-muted"}`}>{t("{0} / at least {1} characters", reason.trim().length, WAIVE_MIN)}</span>
        </label>
        <p className="text-xs text-muted">{t("The trip counts as fully booked without it. This cannot be undone.")}</p>
      </div>
    </FormDrawer>
  );
}
