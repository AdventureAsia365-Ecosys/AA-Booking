"use client";
// app/(staff)/cases/[code]/AgencyBookingPanel.tsx — after-sale changes of an agency (B2B) booking (AA-1075 bk-12, bk-14).
//
// - "Vouchers": the service vouchers the agency gets (one per confirmed supplier booking; no names, no prices).
// - "Fewer travellers": a partial cancellation of one per-person line (dry run first, the fee by the booking's policy).
// - "Cancel for the agency": the agency asked by e-mail; the same cancel as its API (dry run first), optionally asking
//   for a lower fee or a waiver with a reason.
// - The cancellation card: the policy fee, the fee charged, a lower fee waiting for approval (a second person with
//   b2b_desk.approve approves or rejects it, never the one who asked).
// Every change reaches the agency's CRM as an event. Travellers are counts by age band only.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, UserMinus, XCircle } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  CANCEL_REASONS,
  CANCEL_REASON_LABEL,
  REDUCE_REASONS,
  actionKey,
  agencyOpsApi,
  openBlob,
  paxText,
  type CancelReason,
  type CancelResult,
  type Cancellation,
  type ReduceReason,
  type Reduction,
} from "@/lib/api/agencyOps";
import { bkGet } from "@/lib/api/client";
import type { TripCaseDetail } from "@/lib/api/types";
import { Button, DateText, Field, FormDrawer, Input, Pill, Section, Select, Textarea, useCan, useToast } from "@/ui";

interface ConfirmationLine {
  line_ref: string;
  title: string;
  kind: string;
  status: string;
  pax: { adult: number; child: number; infant: number };
}
const REDUCIBLE = new Set(["trip_plan", "activity", "entrance", "meal"]);
const CANCELLED = new Set(["cancelled"]);

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

function money(m: { amount: string; currency: string }): string {
  return `${m.amount} ${m.currency}`;
}

/** Header button: the vouchers file of a confirmed agency booking (opens in a new tab to print). */
export function VouchersButton({ code }: { code: string }) {
  const canRead = useCan("b2b_desk.read");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  if (!canRead) return null;
  async function open() {
    setBusy(true);
    setFailed(null);
    try {
      openBlob(await agencyOpsApi.vouchers(code));
    } catch (e) {
      setFailed(e instanceof ApiError && e.status === 404 ? "No confirmed agency booking yet" : "The vouchers could not be opened");
    } finally {
      setBusy(false);
    }
  }
  return (
    <span className="inline-flex items-center gap-2">
      <Button variant="ghost" disabled={busy} onClick={open}>
        <FileText size={14} /> {busy ? "Preparing…" : "Vouchers"}
      </Button>
      {failed && <span className="text-[12px] text-danger">{failed}</span>}
    </span>
  );
}

export function AgencyBookingPanel({ c }: { c: TripCaseDetail }) {
  const canRead = useCan("b2b_desk.read");
  const canBook = useCan("b2b_desk.book");
  const qc = useQueryClient();
  const toast = useToast();
  const cancellation = useQuery({
    queryKey: ["agency-booking", c.code, "cancellation"],
    queryFn: () => agencyOpsApi.cancellation(c.code),
    enabled: canRead,
    retry: false,
  });
  const reductions = useQuery({
    queryKey: ["agency-booking", c.code, "reductions"],
    queryFn: () => agencyOpsApi.reductions(c.code),
    enabled: canRead,
    retry: false,
  });
  const [drawer, setDrawer] = useState<"cancel" | "reduce" | "override" | null>(null);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["agency-booking", c.code] });
    qc.invalidateQueries({ queryKey: ["trip-case"] });
  };
  const decide = useMutation({
    mutationFn: (approve: boolean) => (approve ? agencyOpsApi.approveOverride(c.code) : agencyOpsApi.rejectOverride(c.code)),
    onSuccess: (x) => {
      toast.success(x.override?.status === "approved" ? `Fee lowered to ${money(x.fee)}; the agency is told` : "The policy fee stays");
      refresh();
    },
    onError: (e) => toast.error(errorText(e)),
  });
  if (!canRead || cancellation.isError) return null; // not a confirmed agency booking (404): nothing to change
  const x = cancellation.data?.cancellation ?? null;
  const live = !x && !CANCELLED.has(c.status);

  return (
    <Section
      title="Agency booking"
      description="Changes after the sale. The agency's CRM gets each one as an event; travellers are counts only."
      actions={
        canBook && live ? (
          <span className="flex flex-wrap gap-2">
            <Button size="sm" variant="ghost" onClick={() => setDrawer("reduce")}>
              <UserMinus size={14} /> Fewer travellers
            </Button>
            <Button size="sm" variant="danger" onClick={() => setDrawer("cancel")}>
              <XCircle size={14} /> Cancel for the agency
            </Button>
          </span>
        ) : null
      }
    >
      {x ? (
        <CancellationCard x={x} canAsk={canBook} onAsk={() => setDrawer("override")} decide={decide} />
      ) : (
        <p className="text-[13px] text-muted">Confirmed. No cancellation.</p>
      )}
      {(reductions.data?.items.length ?? 0) > 0 && (
        <div className="mt-4">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Fewer travellers</p>
          <ul className="flex flex-col gap-2">
            {reductions.data!.items.map((r, i) => (
              <li key={`${r.line_ref}-${i}`} className="text-[13px] text-body">
                Line {r.line_ref}: {paxText(r.removed)} removed, fee {money(r.fee)}, refundable {money(r.refundable_b2b_amount)}
                {r.reduced_at && (
                  <span className="text-xs text-muted">
                    {" "}
                    · <DateText value={r.reduced_at} withTime />
                    {r.entered_via === "staff" ? " · by AA" : " · by the agency"}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {drawer === "cancel" && <CancelDrawer code={c.code} onClose={() => setDrawer(null)} onDone={() => { setDrawer(null); refresh(); }} />}
      {drawer === "reduce" && <ReduceDrawer code={c.code} onClose={() => setDrawer(null)} onDone={() => { setDrawer(null); refresh(); }} />}
      {drawer === "override" && x && (
        <OverrideDrawer code={c.code} x={x} onClose={() => setDrawer(null)} onDone={() => { setDrawer(null); refresh(); }} />
      )}
    </Section>
  );
}

function CancellationCard({
  x,
  canAsk,
  onAsk,
  decide,
}: {
  x: Cancellation;
  canAsk: boolean;
  onAsk: () => void;
  decide: { mutate: (approve: boolean) => void; isPending: boolean };
}) {
  const pending = x.override?.status === "pending";
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone="neutral">Cancelled</Pill>
        <span className="text-[13px] text-body">
          {CANCEL_REASON_LABEL[x.reason_code]} · <DateText value={x.cancelled_at} /> · {x.entered_via === "staff" ? "entered by AA" : "by the agency"}
        </span>
      </div>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-[13px] sm:grid-cols-[180px_1fr]">
        <dt className="text-muted">Booking total</dt>
        <dd>{money(x.total)}</dd>
        <dt className="text-muted">Policy fee ({x.fee_pct} %)</dt>
        <dd>{money(x.policy_fee)}</dd>
        <dt className="text-muted">Fee charged</dt>
        <dd className="font-medium text-ink">{money(x.fee)}</dd>
        <dt className="text-muted">Refundable</dt>
        <dd>{money(x.refundable)}</dd>
      </dl>
      {x.override && (
        <div className="rounded-lg border border-line2 p-3 text-[13px]">
          <p className="flex flex-wrap items-center gap-2">
            <Pill tone={pending ? "warning" : "success"}>{pending ? "Lower fee waiting for approval" : "Lower fee approved"}</Pill>
            <span>{x.override.fee_pct} %</span>
          </p>
          <p className="mt-1 text-muted">
            {x.override.reason} · asked by {x.override.requested_by.name ?? "staff"}
            {x.override.approved_by ? ` · approved by ${x.override.approved_by.name ?? "staff"}` : ""}
          </p>
          {x.can_approve_override && (
            <span className="mt-2 flex gap-2">
              <Button size="sm" variant="primary" disabled={decide.isPending} onClick={() => decide.mutate(true)}>
                Approve lower fee
              </Button>
              <Button size="sm" variant="danger" disabled={decide.isPending} onClick={() => decide.mutate(false)}>
                Keep policy fee
              </Button>
            </span>
          )}
        </div>
      )}
      {canAsk && !x.override && Number(x.fee_pct) > 0 && (
        <span>
          <Button size="sm" variant="ghost" onClick={onAsk}>
            Ask for a lower fee or a waiver
          </Button>
        </span>
      )}
    </div>
  );
}

function CancelDrawer({ code, onClose, onDone }: { code: string; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [key] = useState(() => actionKey("cancel"));
  const [reason, setReason] = useState<CancelReason>("agency_cancelled");
  const [lower, setLower] = useState("");
  const [why, setWhy] = useState("");
  const [preview, setPreview] = useState<CancelResult | null>(null);
  const body = () => ({
    reason_code: reason,
    fee_override_pct: lower.trim() || null,
    waive_reason: lower.trim() ? why.trim() || null : null,
  });
  const dry = useMutation({ mutationFn: () => agencyOpsApi.cancel(code, body(), `${key}-preview`, true), onSuccess: setPreview });
  const run = useMutation({
    mutationFn: () => agencyOpsApi.cancel(code, body(), key, false),
    onSuccess: (r) => {
      toast.success(`${r.trip_case_code} cancelled, fee ${money(r.fee)}${lower.trim() ? "; the lower fee waits for approval" : ""}`);
      onDone();
    },
  });
  const err = dry.error ?? run.error;
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="Cancel for the agency"
      submitLabel={preview ? "Cancel the booking" : "Preview the fee"}
      onSubmit={() => (preview ? run.mutate() : dry.mutate())}
      busy={dry.isPending || run.isPending}
      dirty={!!preview}
      error={err ? errorText(err) : null}
    >
      <div className="flex flex-col gap-4">
        <p className="text-[13px] text-muted">
          The fee follows the cancellation policy locked on the booking. The seats go back on sale and the agency is told.
        </p>
        <Field label="Reason" required>
          <Select
            value={reason}
            onChange={(e) => { setReason(e.target.value as CancelReason); setPreview(null); }}
            options={CANCEL_REASONS.map((r) => ({ value: r, label: CANCEL_REASON_LABEL[r] }))}
          />
        </Field>
        <Field label="Lower fee % (optional)" hint="0 waives the fee. A second person approves it.">
          <Input inputMode="decimal" value={lower} onChange={(e) => setLower(e.target.value)} placeholder="e.g. 10" />
        </Field>
        {lower.trim() && (
          <Field label="Why the lower fee" required>
            <Textarea value={why} maxLength={500} onChange={(e) => setWhy(e.target.value)} />
          </Field>
        )}
        {preview && (
          <div className="rounded-lg border border-line2 p-3 text-[13px]">
            <p>
              Fee {money(preview.fee)} ({preview.fee_basis.fee_pct} %, {preview.fee_basis.days_before_departure} days before departure);
              refundable {money(preview.refundable_b2b_amount)}.
            </p>
          </div>
        )}
      </div>
    </FormDrawer>
  );
}

function OverrideDrawer({ code, x, onClose, onDone }: { code: string; x: Cancellation; onClose: () => void; onDone: () => void }) {
  const [pct, setPct] = useState("0");
  const [why, setWhy] = useState("");
  const save = useMutation({ mutationFn: () => agencyOpsApi.requestOverride(code, { fee_override_pct: pct, waive_reason: why.trim() }), onSuccess: onDone });
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="Ask for a lower fee"
      submitLabel="Ask for approval"
      onSubmit={() => save.mutate()}
      busy={save.isPending}
      dirty={!!why}
      error={save.isError ? errorText(save.error) : null}
    >
      <div className="flex flex-col gap-4">
        <p className="text-[13px] text-muted">
          The policy fee is {x.fee_pct} % ({money(x.policy_fee)}). A second person approves the lower fee; the agency is told then.
        </p>
        <Field label="New fee %" required hint="Below the policy fee; 0 waives it">
          <Input inputMode="decimal" value={pct} onChange={(e) => setPct(e.target.value)} />
        </Field>
        <Field label="Why" required>
          <Textarea value={why} maxLength={500} onChange={(e) => setWhy(e.target.value)} />
        </Field>
      </div>
    </FormDrawer>
  );
}

function ReduceDrawer({ code, onClose, onDone }: { code: string; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [key] = useState(() => actionKey("reduce"));
  const lines = useQuery({
    queryKey: ["agency-booking", code, "lines"],
    queryFn: () => bkGet<{ lines: ConfirmationLine[] }>(`b2b-desk/bookings/${encodeURIComponent(code)}/confirmation`, { format: "json" }),
  });
  const options = (lines.data?.lines ?? []).filter((l) => REDUCIBLE.has(l.kind) && !["declined", "cancelled"].includes(l.status));
  const [lineRef, setLineRef] = useState("");
  const [adult, setAdult] = useState("0");
  const [child, setChild] = useState("0");
  const [infant, setInfant] = useState("0");
  const [reason, setReason] = useState<ReduceReason>("client_cancelled");
  const [preview, setPreview] = useState<Reduction | null>(null);
  const body = () => ({ adult: Number(adult) || 0, child: Number(child) || 0, infant: Number(infant) || 0, reason_code: reason });
  const ref = lineRef || options[0]?.line_ref || "";
  const dry = useMutation({ mutationFn: () => agencyOpsApi.reduce(code, ref, body(), `${key}-preview`, true), onSuccess: setPreview });
  const run = useMutation({
    mutationFn: () => agencyOpsApi.reduce(code, ref, body(), key, false),
    onSuccess: (r) => {
      toast.success(`Line ${r.line_ref}: ${paxText(r.removed)} removed, fee ${money(r.fee)}`);
      onDone();
    },
  });
  const err = dry.error ?? run.error;
  const current = options.find((l) => l.line_ref === ref);
  return (
    <FormDrawer
      open
      onClose={onClose}
      title="Fewer travellers"
      submitLabel={preview ? "Remove the travellers" : "Preview the fee"}
      onSubmit={() => (preview ? run.mutate() : dry.mutate())}
      busy={dry.isPending || run.isPending}
      dirty={!!preview}
      error={err ? errorText(err) : null}
    >
      <div className="flex flex-col gap-4">
        <p className="text-[13px] text-muted">
          Tours, activities, entrances and meals only (rooms and transfers are re-quoted). At least one adult stays.
        </p>
        <Field label="Line" required>
          <Select
            value={ref}
            onChange={(e) => { setLineRef(e.target.value); setPreview(null); }}
            placeholder={lines.isLoading ? "Loading…" : options.length ? undefined : "No line can be reduced"}
            options={options.map((l) => ({ value: l.line_ref, label: `${l.title} (${paxText(l.pax)})` }))}
          />
        </Field>
        {current && <p className="text-xs text-muted">Now on the line: {paxText(current.pax)}</p>}
        <div className="grid grid-cols-3 gap-3">
          <Field label="Adults">
            <Input type="number" min={0} value={adult} onChange={(e) => { setAdult(e.target.value); setPreview(null); }} />
          </Field>
          <Field label="Children">
            <Input type="number" min={0} value={child} onChange={(e) => { setChild(e.target.value); setPreview(null); }} />
          </Field>
          <Field label="Infants">
            <Input type="number" min={0} value={infant} onChange={(e) => { setInfant(e.target.value); setPreview(null); }} />
          </Field>
        </div>
        <Field label="Reason" required>
          <Select
            value={reason}
            onChange={(e) => setReason(e.target.value as ReduceReason)}
            options={REDUCE_REASONS.map((r) => ({ value: r, label: CANCEL_REASON_LABEL[r] }))}
          />
        </Field>
        {preview && (
          <div className="rounded-lg border border-line2 p-3 text-[13px]">
            Their share {money(preview.removed_b2b_amount)}; fee {money(preview.fee)} ({preview.fee_basis.fee_pct} %); refundable{" "}
            {money(preview.refundable_b2b_amount)}. {preview.seats_after} seat(s) stay on the line.
          </div>
        )}
      </div>
    </FormDrawer>
  );
}
