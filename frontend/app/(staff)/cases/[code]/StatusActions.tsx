"use client";
// app/(staff)/cases/[code]/StatusActions.tsx
// AA-683 (fe-cases) — the status actions of the case header (spec §4.7, Figma USER DETAILS-6/-7):
//   new_inquiry → "Convert to discovery" (sales rep + note) and "Decline" (reason select + notes);
//   cancelled   → "Reopen" (reason required, back to new_inquiry, bk-005 edge);
//   otherwise   → "Change status ▾", listing only `allowed_transitions` from the API, plus "Cancel case…".
// `confirmed` is never a bare move (the API answers approve_version_first): it shows disabled with a hint.
// A reason is required whenever the case goes to or leaves `cancelled` (service.reason_required).

import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronDown, RotateCcw, X, XCircle } from "lucide-react";
import type { TripCaseDetail, TripCaseStatus } from "@/lib/api/types";
import {
  Input, Select, Textarea, Button, ConfirmDialog, ListButton, Modal, TRIP_CASE_STATUS_META, useCan, useStaff,
} from "@/ui";
import { DECLINE_REASONS, Field, inputCls, textareaCls } from "../_parts/fields";
import { StaffSelect } from "../_parts/StaffSelect";
import { TripPlanPicker } from "../_parts/TripPlanPicker";
import { tripPlansApi, type TripPlanListItem } from "@/lib/api/tripPlans";
import { friendlyError } from "../_parts/QueryError";
import { useAssign, useChangeStatus } from "./useCaseMutations";
import { casePartyLabel } from "@/ui/booking/caseFacts";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./StatusActions.messages";

// AA-682 (bk-026, critic B1): post-sale statuses follow supplier bookings, invoices and dates; the API answers
// status_system_managed, so they are never offered. Confirmed is reached by approval (or by the system after a
// supplier_confirming case loses its last live booking), never picked here once the trip is sold.
const SYSTEM_STATUSES: readonly TripCaseStatus[] = [
  "supplier_confirming",
  "invoice_sent",
  "payment_pending",
  "paid",
  "pre_departure",
  "on_trip",
  "completed",
];
const SOLD: readonly TripCaseStatus[] = ["confirmed", ...SYSTEM_STATUSES];

const CANCEL_REASONS = ["Customer cancelled", "Customer chose another company", ...DECLINE_REASONS] as const;

type Dialog =
  | { kind: "convert" }
  | { kind: "decline" }
  | { kind: "cancel" }
  | { kind: "reopen" }
  | { kind: "move"; to: TripCaseStatus }
  | null;

export function StatusActions({ c }: { c: TripCaseDetail }) {
  const t = useT(MSG);
  const tx = useTx();
  const canWrite = useCan("trip_case.write");
  const [dialog, setDialog] = useState<Dialog>(null);
  const change = useChangeStatus(c.code);
  if (!canWrite) return null;

  const allowed = c.allowed_transitions;
  const close = () => {
    if (change.isPending) return;
    change.reset();
    setDialog(null);
  };
  const moves = allowed.filter(
    (s) =>
      s !== "cancelled" &&
      !(c.status === "cancelled" && s === "new_inquiry") &&
      !SYSTEM_STATUSES.includes(s) &&
      !(s === "confirmed" && SOLD.includes(c.status)),
  );
  const isInquiry = c.status === "new_inquiry";

  return (
    <>
      {isInquiry && allowed.includes("cancelled") && (
        <Button variant="ghost" onClick={() => setDialog({ kind: "decline" })}>
          <X size={14} /> {t("Decline")}
        </Button>
      )}
      {c.status === "cancelled" && allowed.includes("new_inquiry") && (
        <Button variant="secondary" onClick={() => setDialog({ kind: "reopen" })}>
          <RotateCcw size={14} /> {t("Reopen")}
        </Button>
      )}
      {!isInquiry && (moves.length > 0 || allowed.includes("cancelled")) && (
        <StatusMenu
          moves={moves}
          canCancel={allowed.includes("cancelled")}
          onMove={(to) => setDialog({ kind: "move", to })}
          onCancel={() => setDialog({ kind: "cancel" })}
        />
      )}
      {isInquiry && allowed.includes("discovery") && (
        <Button variant="primary" onClick={() => setDialog({ kind: "convert" })}>
          <Check size={14} /> {t("Convert to discovery")}
        </Button>
      )}

      <ConvertDialog c={c} open={dialog?.kind === "convert"} onClose={close} />
      <CancelDialog
        c={c}
        open={dialog?.kind === "decline" || dialog?.kind === "cancel"}
        decline={dialog?.kind === "decline"}
        onClose={close}
        busy={change.isPending}
        error={change.isError ? friendlyError(change.error) : null}
        onConfirm={(reason) => change.mutate({ to: "cancelled", reason }, { onSuccess: () => setDialog(null) })}
      />
      <ConfirmDialog
        open={dialog?.kind === "reopen"}
        onClose={close}
        title={t("Reopen {0}", c.code)}
        message={
          <>
            {t("The case goes back to")} <strong>{t("New inquiry")}</strong> {t("and its history is kept. Say why it is reopened.")}
          </>
        }
        reason={{ label: t("Reason for reopening"), required: true, placeholder: t("The customer came back and wants to travel in spring.") }}
        confirmLabel={t("Reopen case")}
        busy={change.isPending}
        error={change.isError ? friendlyError(change.error) : null}
        onConfirm={(reason) => change.mutate({ to: "new_inquiry", reason }, { onSuccess: () => setDialog(null) })}
      />
      <ConfirmDialog
        open={dialog?.kind === "move"}
        onClose={close}
        title={dialog?.kind === "move" ? t("Move to {0}", tx(TRIP_CASE_STATUS_META[dialog.to].label)) : t("Change status")}
        message={
          dialog?.kind === "move" ? (
            <>
              {t("{0} moves from", c.code)} <strong>{tx(TRIP_CASE_STATUS_META[c.status].label)}</strong> {t("to")}
              <strong>{tx(TRIP_CASE_STATUS_META[dialog.to].label)}</strong>{t(". The change is recorded in the case history.")}
            </>
          ) : null
        }
        reason={{ label: t("Note (optional)"), placeholder: t("Shown in the case history.") }}
        confirmLabel={t("Change status")}
        busy={change.isPending}
        error={change.isError ? friendlyError(change.error) : null}
        onConfirm={(reason) => dialog?.kind === "move" && change.mutate({ to: dialog.to, reason }, { onSuccess: () => setDialog(null) })}
      />
    </>
  );
}

function StatusMenu({
  moves,
  canCancel,
  onMove,
  onCancel,
}: {
  moves: TripCaseStatus[];
  canCancel: boolean;
  onMove: (to: TripCaseStatus) => void;
  onCancel: () => void;
}) {
  const t = useT(MSG);
  const tx = useTx();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const pick = (fn: () => void) => () => {
    setOpen(false);
    fn();
  };
  return (
    <div ref={ref} className="relative">
      <Button variant="secondary" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {t("Change status")} <ChevronDown size={14} />
      </Button>
      {open && (
        <div role="menu" className="absolute right-0 top-full z-30 mt-1 w-[260px] rounded-lg border border-line bg-card p-1.5 shadow-lg">
          {moves.length === 0 && <p className="px-3 py-2 text-[13px] text-muted">{t("No other status is reachable from here.")}</p>}
          {moves.map((s) =>
            s === "confirmed" ? (
              <div key={s} role="menuitem" aria-disabled="true" className="cursor-not-allowed rounded-md px-3 py-2 text-[13px] text-muted2">
                {tx(TRIP_CASE_STATUS_META[s].label)}
                <span className="block text-[11.5px]">{t("Approve a sent proposal version to confirm.")}</span>
              </div>
            ) : (
              <ListButton key={s} role="menuitem" onClick={pick(() => onMove(s))}>
                {tx(TRIP_CASE_STATUS_META[s].label)}
              </ListButton>
            ),
          )}
          {canCancel && (
            <>
              <div className="my-1 border-t border-line2" />
              <ListButton role="menuitem" tone="danger" onClick={pick(onCancel)}>
                <XCircle size={14} /> {t("Cancel case…")}
              </ListButton>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Decline (inquiry) or Cancel (later status): a reason select + notes; the API stores "Reason: notes".
 *  AA-996 (Figma USER DETAILS-7): Cancel / confirm sit in the Modal footer band, like every other Booking dialog. The
 *  form is remounted per open (it renders only while open), so it starts at the first reason each time. */
function CancelDialog(props: {
  c: TripCaseDetail;
  open: boolean;
  decline: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
  busy: boolean;
  error: string | null;
}) {
  return props.open ? <CancelModal {...props} /> : null;
}

function CancelModal({
  c,
  decline,
  onClose,
  onConfirm,
  busy,
  error,
}: {
  c: TripCaseDetail;
  decline: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
  busy: boolean;
  error: string | null;
}) {
  const t = useT(MSG);
  const tx = useTx();
  const reasons = decline ? DECLINE_REASONS : CANCEL_REASONS;
  const [reason, setReason] = useState<string>(reasons[0]);
  const [notes, setNotes] = useState("");
  const needsNotes = reason === "Other";
  const text = notes.trim() ? `${reason}: ${notes.trim()}` : reason;
  return (
    <Modal
      open
      onClose={busy ? () => {} : onClose}
      title={decline ? t("Decline inquiry") : t("Cancel {0}", c.code)}
      width={520}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t("Keep case")}
          </Button>
          <Button variant="danger" onClick={() => onConfirm(text)} disabled={busy || (needsNotes && !notes.trim())}>
            {busy ? t("Working…") : decline ? t("Decline inquiry") : t("Cancel case")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4 font-sans">
        <p className="text-sm text-body">
          {decline ? t("Decline") : t("Cancel")} <strong className="font-mono">{c.code}</strong> {t("for")} <strong>{tx(casePartyLabel(c))}</strong>{t("? The case moves to Cancelled and keeps its history. It can be reopened later with a reason.")}
        </p>
        <Field label={decline ? t("Reason for declining") : t("Reason for cancelling")} required>
          <Select className={inputCls} value={reason} onChange={(e) => setReason(e.target.value)}>
            {reasons.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </Select>
        </Field>
        <Field label={needsNotes ? t("Notes") : t("Additional notes (optional)")} required={needsNotes}>
          <Textarea className={textareaCls} value={notes} maxLength={1900} onChange={(e) => setNotes(e.target.value)} placeholder={t("Shown in the case history.")} />
        </Field>
        {error && <p className="text-[13px] text-danger">{tx(error)}</p>}
      </div>
    </Modal>
  );
}

/** Convert to discovery (Figma USER DETAILS-6 "Convert to Trip Case", minus the part that is not ours: the case
 * already exists). Assign first (when the rep changes), then the status, then (AA-996 slice 2, Figma "Template") the
 * optional trip plan: POST /booking/catalog/trip-plans/{source_id}/trip-cases with this case adds a draft version
 * from the plan's CIS days. AA-996: the buttons are in the Modal footer band. */
function ConvertDialog({ c, open, onClose }: { c: TripCaseDetail; open: boolean; onClose: () => void }) {
  return open ? <ConvertModal c={c} onClose={onClose} /> : null;
}

function ConvertModal({ c, onClose }: { c: TripCaseDetail; onClose: () => void }) {
  const t = useT(MSG);
  const tx = useTx();
  const me = useStaff();
  const canAssign = useCan("trip_case.assign");
  const current = c.assignees.find((a) => a.role === "sales")?.cis_user_id ?? "";
  const [rep, setRep] = useState(current || (me.role === "sales" ? me.cis_user_id : ""));
  const [note, setNote] = useState("");
  const canStartFromPlan = useCan("version.write");
  const [plan, setPlan] = useState<TripPlanListItem | null>(null);
  const qc = useQueryClient();
  const assign = useAssign(c.code);
  const change = useChangeStatus(c.code);
  const fromPlan = useMutation({
    mutationFn: (p: TripPlanListItem) => tripPlansApi.useForTripCase(p.source_id, { trip_case: c.code }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["trip-case", c.code] });
      qc.invalidateQueries({ queryKey: ["trip-cases"] });
    },
  });
  const busy = assign.isPending || change.isPending || fromPlan.isPending;
  const err = assign.error ?? change.error ?? fromPlan.error;

  async function confirm() {
    try {
      if (canAssign && rep !== current) await assign.mutateAsync({ cisUserId: rep || null, quiet: true });
      if (c.status === "new_inquiry") await change.mutateAsync({ to: "discovery", reason: note.trim() || undefined });
      if (plan) await fromPlan.mutateAsync(plan);
      onClose();
    } catch {
      /* shown below */
    }
  }

  return (
    <Modal
      open
      onClose={busy ? () => {} : onClose}
      title={t("Convert to discovery")}
      width={520}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t("Cancel")}
          </Button>
          <Button variant="primary" onClick={confirm} disabled={busy}>
            {busy ? t("Working…") : t("Convert")}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4 font-sans">
        <p className="text-sm text-body">
          {t("Start working on this inquiry (")}<strong>{tx(casePartyLabel(c))}</strong>{t("). The case moves to Discovery: talk with the customer, then build a proposal.")}
        </p>
        <Field label={t("Sales rep")} hint={canAssign ? undefined : t("Your role cannot change the assignment.")}>
          {canAssign ? <StaffSelect value={rep} onChange={setRep} /> : <Input className={inputCls} disabled value={c.assignees.find((a) => a.role === "sales")?.name ?? "Unassigned"} />}
        </Field>
        {canStartFromPlan && (
          <div className="flex min-w-0 flex-col gap-1.5" role="group" aria-label={t("Start from a trip plan")}>
            <span className="text-[13px] font-medium text-ink">{t("Start from a trip plan (optional)")}</span>
            <TripPlanPicker value={plan} onChange={setPlan} disabled={busy} />
            <span className="text-xs text-muted">{t("Adds a draft proposal from the plan's CIS days, unpriced.")}</span>
          </div>
        )}
        <Field label={t("Note (optional)")} hint={t("Saved in the case history with the status change.")}>
          <Textarea className={textareaCls} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} placeholder={t("Called the customer, they want a quieter pace.")} />
        </Field>
        {err && <p className="text-[13px] text-danger">{tx(friendlyError(err))}</p>}
      </div>
    </Modal>
  );
}
