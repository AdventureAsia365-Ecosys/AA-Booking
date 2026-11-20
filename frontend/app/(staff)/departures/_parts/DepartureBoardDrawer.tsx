"use client";
// app/(staff)/departures/_parts/DepartureBoardDrawer.tsx — the drill-down of one departure on the departure board
// (AA-1073 rest, bk-048): seats and status; seats by agency (held, sold, its allotment, links to its trip cases); the
// Ops section (meeting point and time, sent to the agencies on the departure; a staff note; guide, tour leader and
// driver); departure fees (Charge fee, approve / reject by another person); the price adjustment of the departure
// (propose, approve by another person, withdraw). Reads b2b_desk.read; writes ops.write, b2b_desk.write /
// b2b_desk.approve, pricing.write to propose a price adjustment and pricing.approve to approve or end one (AA-1155);
// each section hides what the role cannot do.

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Coins, Percent, UserPlus, X } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  FEE_REASON_LABEL,
  FEE_STATUS,
  STAFF_ROLE_LABEL,
  adjustmentLabel,
  departureBoardApi,
  type BoardDetail,
  type FeeReason,
  type PriceAdjustment,
  type StaffRole,
} from "@/lib/api/departureBoard";
import { Button, DateText, Drawer, Input, Pill, Select, Textarea, TimeField, useCan, useStaff, useToast } from "@/ui";
import { FormField, inputCls } from "../../_parts/FormField";
import { ChargeFeeModal } from "./ChargeFeeModal";
import { DepartureStatus, SeatsBar } from "./shared";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./DepartureBoardDrawer.messages";
import { useBookingFormat } from "@/ui/local/useBookingFormat";

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

export function DepartureBoardDrawer({ departureId, onClose }: { departureId: string; onClose: () => void }) {
  const t = useT(MSG);
  const { formatDay } = useBookingFormat();
  const tx = useTx();
  const detail = useQuery({ queryKey: ["departure-board", "detail", departureId], queryFn: () => departureBoardApi.detail(departureId) });
  const d = detail.data?.departure;
  return (
    <Drawer
      open
      onClose={onClose}
      width={640}
      title={d ? t("Departure {0}", formatDay(d.start_date)) : t("Departure")}
      footer={
        <div className="flex flex-wrap justify-end gap-2">
          {d && (
            <Link
              className="inline-flex items-center rounded-md px-3 py-2 text-[13px] font-medium text-accent-deep hover:underline"
              href={`/catalog/trip-plans/${encodeURIComponent(d.source_id)}?tab=departures&departure=${encodeURIComponent(d.id)}`}
            >
              {t("Open on the trip plan")}
            </Link>
          )}
          {d && d.sold > 0 && (
            <Link
              className="inline-flex items-center rounded-md px-3 py-2 text-[13px] font-medium text-accent-deep hover:underline"
              href={`/b2b-desk/manifest/${encodeURIComponent(d.id)}`}
            >
              {t("Manifest")}
            </Link>
          )}
          <Button variant="ghost" onClick={onClose}>
            {t("Close")}
          </Button>
        </div>
      }
    >
      {detail.isLoading ? (
        <p className="text-[13px] text-muted">{t("Loading…")}</p>
      ) : detail.isError || !detail.data ? (
        <p className="text-[13px] text-danger">{tx(errorText(detail.error))}</p>
      ) : (
        <Body data={detail.data} />
      )}
    </Drawer>
  );
}

function Body({ data }: { data: BoardDetail }) {
  const t = useT(MSG);
  const d = data.departure;
  return (
    <div className="flex flex-col gap-6 font-sans" data-testid="departure-board-drawer">
      <div className="flex flex-col gap-2">
        <span className="text-[14px] font-semibold text-ink">{d.tour_title ?? t("Tour {0}", d.source_id.slice(0, 8))}</span>
        <div className="flex flex-wrap items-center gap-3 text-[13px] text-body">
          <DepartureStatus d={d} />
          <span className="bk-tabular">
            {t("{0} sold · {1} held · {2} free / {3}{4}", d.sold, d.held, d.free, d.capacity, d.allotment_unused ? t(" · {0} kept for allotments", d.allotment_unused) : "")}
          </span>
          {d.min_to_operate > 0 && <span className="text-muted">{t("Minimum")} {d.min_to_operate}</span>}
        </div>
        <SeatsBar d={d} />
      </div>
      <AgenciesSection data={data} />
      <OpsSection data={data} />
      <FeesSection data={data} />
      <PriceSection data={data} />
    </div>
  );
}

function SectionHead({ title, hint }: { title: string; hint: string }) {
  const tx = useTx();
  return (
    <div>
      <h3 className="text-[14px] font-semibold text-ink">{tx(title)}</h3>
      <p className="text-xs text-muted">{tx(hint)}</p>
    </div>
  );
}

function AgenciesSection({ data }: { data: BoardDetail }) {
  const t = useT(MSG);
  const { formatDay } = useBookingFormat();
  return (
    <section className="flex flex-col gap-3 border-t border-line2 pt-4" aria-label={t("Seats by agency")}>
      <SectionHead title={t("Seats by agency")} hint={t("Live holds, sold seats and allotments of each agency on this departure.")} />
      {data.agencies.length === 0 ? (
        <p className="text-[13px] text-muted2">{t("No agency on this departure yet.")}</p>
      ) : (
        <ul className="flex flex-col divide-y divide-line2" data-testid="agency-seats">
          {data.agencies.map((a) => (
            <li key={a.partner_id} className="flex flex-wrap items-start justify-between gap-2 py-2 first:pt-0 last:pb-0">
              <span className="flex min-w-0 flex-col text-[13px]">
                <span className="font-medium text-ink">{a.agency_name}</span>
                <span className="bk-tabular text-xs text-muted">
                  {t("{0} sold · {1} held", a.sold, a.held)}
                  {a.allotment &&
                    t(" · allotment {0} ({1} unused, {2})", a.allotment.seats, a.allotment.unused, a.allotment.state === "released" ? t("released") : t("release {0}", formatDay(a.allotment.release_date)))}
                </span>
              </span>
              <span className="flex flex-wrap gap-2">
                {a.cases.map((c) => (
                  <Link key={c.id} className="text-xs font-medium text-accent-deep hover:underline" href={`/cases/${encodeURIComponent(c.code)}`}>
                    {c.code}
                  </Link>
                ))}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function OpsSection({ data }: { data: BoardDetail }) {
  const t = useT(MSG);
  const tx = useTx();
  // B2C history gate: ops.write here writes a departure ops sheet (fixed departures are B2B only), never a case.
  const canWrite = useCan("ops.write");
  const qc = useQueryClient();
  const toast = useToast();
  const d = data.departure;
  const ops = data.ops;
  const [point, setPoint] = useState(ops.meeting_point ?? "");
  const [time, setTime] = useState((ops.meeting_time ?? "").slice(0, 5));
  const [note, setNote] = useState(ops.ops_note ?? "");
  const [role, setRole] = useState<StaffRole>("guide");
  const [who, setWho] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["departure-board"] });
  const save = useMutation({
    mutationFn: () => departureBoardApi.updateOps(d.id, { meeting_point: point.trim() || null, meeting_time: time || null, ops_note: note.trim() || null }),
    onSuccess: () => {
      toast.success(d.agencies_count ? t("Ops sheet saved; the agencies on it see the new meeting point") : t("Ops sheet saved"));
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const assign = useMutation({
    mutationFn: () =>
      departureBoardApi.assign(d.id, who.startsWith("sup:") ? { role, supplier_id: who.slice(4) } : { role, staff_user_id: who.slice(6) }),
    onSuccess: () => {
      setWho("");
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: (id: string) => departureBoardApi.unassign(id),
    onSuccess: refresh,
    onError: (e) => toast.error(errorText(e)),
  });
  const editable = canWrite && d.status !== "cancelled";
  return (
    <section className="flex flex-col gap-3 border-t border-line2 pt-4" aria-label={t("Ops")}>
      <SectionHead
        title={t("Ops")}
        hint={t("The meeting point and time go to the agencies on the departure (their CRM shows them to travellers). The note and the staff stay with AA.")}
      />
      {editable ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <FormField label={t("Meeting point")}>
            <Input className={inputCls} maxLength={300} value={point} placeholder={t("Hotel lobby, station…")} onChange={(e) => setPoint(e.target.value)} />
          </FormField>
          <FormField label={t("Meeting time")} hint={t("Local time on the start date")}>
            <TimeField className={inputCls} value={time} onChange={setTime} aria-label={t("Meeting time")} />
          </FormField>
          <div className="sm:col-span-2">
            <FormField label={t("Ops note")} hint={t("Staff only, never sent to an agency.")}>
              <Textarea className={inputCls} rows={2} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
            </FormField>
          </div>
          <div className="sm:col-span-2">
            <Button size="sm" variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? t("Saving…") : t("Save ops sheet")}
            </Button>
          </div>
        </div>
      ) : (
        <p className="text-[13px] text-body">
          {ops.meeting_point ? `${ops.meeting_point}${ops.meeting_time ? ` at ${ops.meeting_time.slice(0, 5)}` : ""}` : t("No meeting point yet.")}
          {ops.ops_note && <span className="block text-xs text-muted">{tx(ops.ops_note)}</span>}
        </p>
      )}
      {ops.staff.length > 0 && (
        <ul className="flex flex-col divide-y divide-line2" data-testid="departure-staff">
          {ops.staff.map((s) => (
            <li key={s.id} className="flex items-center justify-between gap-2 py-1.5 text-[13px]">
              <span>
                <span className="font-medium text-ink">{tx(STAFF_ROLE_LABEL[s.role])}</span>{" "}
                <span className="text-body">{s.staff?.name ?? s.staff?.id ?? s.supplier?.name ?? "—"}</span>
                {s.supplier && <span className="text-xs text-muted"> {t("(supplier)")}</span>}
              </span>
              {editable && (
                <Button size="sm" variant="ghost" ariaLabel={t("Remove {0}", STAFF_ROLE_LABEL[s.role])} disabled={remove.isPending} onClick={() => remove.mutate(s.id)}>
                  <X size={14} />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editable && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto] sm:items-end">
          <FormField label={t("Role")}>
            <Select className={inputCls} value={role} onChange={(e) => setRole(e.target.value as StaffRole)}>
              {(Object.keys(STAFF_ROLE_LABEL) as StaffRole[]).map((r) => (
                <option key={r} value={r}>
                  {tx(STAFF_ROLE_LABEL[r])}
                </option>
              ))}
            </Select>
          </FormField>
          <FormField label={t("Who")}>
            <Select className={inputCls} value={who} onChange={(e) => setWho(e.target.value)}>
              <option value="">{t("Choose staff or a supplier")}</option>
              <optgroup label={t("AA staff")}>
                {(ops.staff_options ?? []).map((o) => (
                  <option key={o.id} value={`staff:${o.id}`}>
                    {o.name ?? o.id}
                  </option>
                ))}
              </optgroup>
              <optgroup label={t("Suppliers (guides, transport)")}>
                {(ops.supplier_options ?? []).map((o) => (
                  <option key={o.id} value={`sup:${o.id}`}>
                    {o.name}
                  </option>
                ))}
              </optgroup>
            </Select>
          </FormField>
          <Button size="sm" disabled={!who || assign.isPending} onClick={() => assign.mutate()}>
            <UserPlus size={14} /> {t("Assign")}
          </Button>
        </div>
      )}
    </section>
  );
}

function FeesSection({ data }: { data: BoardDetail }) {
  const t = useT(MSG);
  const { formatDay } = useBookingFormat();
  const tx = useTx();
  const canPropose = useCan("b2b_desk.write");
  const qc = useQueryClient();
  const toast = useToast();
  const d = data.departure;
  const [charging, setCharging] = useState<{ reason: FeeReason; partner?: string; seats?: number } | null>(null);
  const decide = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) => departureBoardApi.decideFee(id, approve),
    onSuccess: (fee) => {
      toast.success(fee.status === "approved" ? t("Fee approved; the agency is told and it goes on the next settlement invoice") : t("Fee rejected"));
      qc.invalidateQueries({ queryKey: ["departure-board"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });
  return (
    <section className="flex flex-col gap-3 border-t border-line2 pt-4" aria-label={t("Departure fees")}>
      <SectionHead title={t("Departure fees")} hint={t("Charged to one agency: minimum not reached, allotted seats not released, or other. A second person approves.")} />
      {data.fees.length === 0 ? (
        <p className="text-[13px] text-muted2">{t("No fee on this departure.")}</p>
      ) : (
        <ul className="flex flex-col divide-y divide-line2" data-testid="departure-fees">
          {data.fees.map((f) => (
            <li key={f.id} className="flex flex-wrap items-start justify-between gap-2 py-2 first:pt-0 last:pb-0">
              <span className="flex min-w-0 flex-col text-[13px]">
                <span className="font-medium text-ink">
                  {f.partner.agency_name} · {f.amount} {f.currency}
                </span>
                <span className="text-xs text-muted">
                  {tx(FEE_REASON_LABEL[f.reason])} · {f.seats} × {f.price_per_seat}
                  {f.created_by?.name && t(" · proposed by {0}", f.created_by.name)}
                  {f.invoice_number && t(" · invoice {0}", f.invoice_number)}
                  {!f.invoice_number && f.billed && t(" · on a draft invoice")}
                </span>
                {f.note && <span className="text-xs text-muted">{tx(f.note)}</span>}
              </span>
              <span className="flex flex-wrap items-center gap-2">
                <Pill tone={FEE_STATUS[f.status].tone}>{tx(FEE_STATUS[f.status].label)}</Pill>
                {f.status === "pending" && f.can_decide && (
                  <>
                    <Button size="sm" variant="primary" disabled={decide.isPending} onClick={() => decide.mutate({ id: f.id, approve: true })}>
                      {t("Approve")}
                    </Button>
                    <Button size="sm" variant="ghost" disabled={decide.isPending} onClick={() => decide.mutate({ id: f.id, approve: false })}>
                      {t("Reject")}
                    </Button>
                  </>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {canPropose && (
        <div className="flex flex-wrap gap-2">
          {data.fee_suggestions.map((s) => (
            <Button
              key={`${s.reason}-${s.partner_id ?? "all"}`}
              size="sm"
              onClick={() => setCharging({ reason: s.reason, partner: s.partner_id ?? undefined, seats: s.seats })}
            >
              <Coins size={14} /> {t("Charge {0} ({1} seat{2}{3})", tx(FEE_REASON_LABEL[s.reason]).toLowerCase(), s.seats, s.seats === 1 ? "" : "s", s.partner_id ? `, ${data.agencies.find((a) => a.partner_id === s.partner_id)?.agency_name ?? t("agency")}` : "")}
            </Button>
          ))}
          <Button size="sm" variant="ghost" onClick={() => setCharging({ reason: "other" })}>
            <Coins size={14} /> {t("Charge fee")}
          </Button>
        </div>
      )}
      {charging && (
        <ChargeFeeModal
          departureId={d.id}
          title={t("Departure {0}{1}", formatDay(d.start_date), d.tour_title ? `, ${d.tour_title}` : "")}
          reason={charging.reason}
          partnerId={charging.partner}
          seats={charging.seats}
          onClose={() => setCharging(null)}
        />
      )}
    </section>
  );
}

function PriceSection({ data }: { data: BoardDetail }) {
  const t = useT(MSG);
  const tx = useTx();
  const canPrice = useCan("pricing.write");
  const canDecide = useCan("pricing.approve"); // AA-1155: approve / reject / withdraw a price adjustment
  const me = useStaff();
  const qc = useQueryClient();
  const toast = useToast();
  const d = data.departure;
  const [kind, setKind] = useState<"pct" | "amount">("pct");
  const [value, setValue] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [reason, setReason] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["departure-board"] });
  const propose = useMutation({
    mutationFn: () =>
      departureBoardApi.proposeAdjustment(
        d.id,
        kind === "pct"
          ? { adjust_pct: String(Number(value) / 100), reason: reason.trim() }
          : { amount: value, currency: currency.trim().toUpperCase(), reason: reason.trim() },
      ),
    onSuccess: () => {
      toast.success(t("Price adjustment proposed; another person approves it"));
      setValue("");
      setReason("");
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "approve" | "end" }) =>
      action === "approve" ? departureBoardApi.approveAdjustment(id) : departureBoardApi.endAdjustment(id),
    onSuccess: (a: PriceAdjustment) => {
      toast.success(a.state === "in_force" ? t("Price adjustment in force: new quotes on this departure use it") : t("Price adjustment {0}", t(`end_${a.end_reason ?? "ended"}`)));
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  const live = data.price_adjustments.filter((a) => a.state !== "ended");
  const early = d.price.early_booking;
  return (
    <section className="flex flex-col gap-3 border-t border-line2 pt-4" aria-label={t("Price")}>
      <SectionHead
        title={t("Price")}
        hint={t("An adjustment changes the agency price of this departure after its tier discount. A price under the floor margin still sells and is noted on the quote.")}
      />
      <p className="text-[13px] text-body">
        {d.price.hint ? t("From {0} {1}", d.price.hint.amount, d.price.hint.currency) : t("No price hint on the trip plan.")}
        {early && (
          <span className="block text-xs text-muted">
            {t("Early booking: {0} margin points off for quotes until", Math.round(Number(early.pts) * 10000) / 100)} <DateText value={early.until} />.
          </span>
        )}
      </p>
      {live.length > 0 && (
        <ul className="flex flex-col divide-y divide-line2" data-testid="price-adjustments">
          {live.map((a) => (
            <li key={a.id} className="flex flex-wrap items-start justify-between gap-2 py-2 first:pt-0 last:pb-0">
              <span className="flex min-w-0 flex-col text-[13px]">
                <span className="font-medium text-ink">{tx(adjustmentLabel(a))}</span>
                <span className="text-xs text-muted">
                  {tx(a.reason)}
                  {a.created_by?.name && t(" · proposed by {0}", a.created_by.name)}
                </span>
              </span>
              <span className="flex flex-wrap items-center gap-2">
                <Pill tone={a.state === "in_force" ? "success" : "warning"}>{a.state === "in_force" ? t("In force") : t("Waiting for approval")}</Pill>
                {canDecide && a.state === "pending" && (
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={act.isPending || a.created_by?.id === me.cis_user_id}
                    title={a.created_by?.id === me.cis_user_id ? t("You proposed it: another person approves it") : undefined}
                    onClick={() => act.mutate({ id: a.id, action: "approve" })}
                  >
                    {t("Approve")}
                  </Button>
                )}
                {canDecide && (
                  <Button size="sm" variant="ghost" disabled={act.isPending} onClick={() => act.mutate({ id: a.id, action: "end" })}>
                    {a.state === "pending" ? t("Reject") : t("Withdraw")}
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {canPrice && d.status !== "cancelled" && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)]">
          <FormField label={t("Change by")}>
            <Select className={inputCls} value={kind} onChange={(e) => setKind(e.target.value as "pct" | "amount")}>
              <option value="pct">{t("Percent of the price")}</option>
              <option value="amount">{t("Amount per traveller")}</option>
            </Select>
          </FormField>
          <FormField label={kind === "pct" ? t("Percent (−10 = 10 % off)") : t("Amount (negative = lower)")}>
            <Input className={inputCls} inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} />
          </FormField>
          {kind === "amount" ? (
            <FormField label={t("Currency")}>
              <Input className={inputCls} maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
            </FormField>
          ) : (
            <span />
          )}
          <div className="sm:col-span-3">
            <FormField label={t("Reason")}>
              <Input className={inputCls} maxLength={300} value={reason} onChange={(e) => setReason(e.target.value)} />
            </FormField>
          </div>
          <div className="sm:col-span-3">
            <Button size="sm" disabled={!value || !reason.trim() || propose.isPending} onClick={() => propose.mutate()}>
              <Percent size={14} /> {t("Propose price adjustment")}
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
