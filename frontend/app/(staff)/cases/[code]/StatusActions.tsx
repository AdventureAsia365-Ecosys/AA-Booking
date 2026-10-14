"use client";
// app/(staff)/cases/[code]/StatusActions.tsx
// AA-683 (fe-cases) — the status actions of the case header (spec §4.7, Figma USER DETAILS-6/-7):
//   new_inquiry → "Convert to discovery" (sales rep + note) and "Decline" (reason select + notes);
//   cancelled   → "Reopen" (reason required, back to new_inquiry, bk-005 edge);
//   otherwise   → "Change status ▾", listing only `allowed_transitions` from the API, plus "Cancel case…".
// `confirmed` is never a bare move (the API answers approve_version_first): it shows disabled with a hint.
// A reason is required whenever the case goes to or leaves `cancelled` (service.reason_required).

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, RotateCcw, X, XCircle } from "lucide-react";
import type { TripCaseDetail, TripCaseStatus } from "@/lib/api/types";
import { Button, ConfirmDialog, Modal, TRIP_CASE_STATUS_META, useCan, useStaff } from "@/ui";
import { DECLINE_REASONS, Field, inputCls, textareaCls } from "../_parts/fields";
import { StaffSelect } from "../_parts/StaffSelect";
import { friendlyError } from "../_parts/QueryError";
import { useAssign, useChangeStatus } from "./useCaseMutations";

const CANCEL_REASONS = ["Customer cancelled", "Customer chose another company", ...DECLINE_REASONS] as const;

type Dialog =
  | { kind: "convert" }
  | { kind: "decline" }
  | { kind: "cancel" }
  | { kind: "reopen" }
  | { kind: "move"; to: TripCaseStatus }
  | null;

export function StatusActions({ c }: { c: TripCaseDetail }) {
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
  const moves = allowed.filter((s) => s !== "cancelled" && !(c.status === "cancelled" && s === "new_inquiry"));
  const isInquiry = c.status === "new_inquiry";

  return (
    <>
      {isInquiry && allowed.includes("cancelled") && (
        <Button variant="ghost" onClick={() => setDialog({ kind: "decline" })}>
          <X size={14} /> Decline
        </Button>
      )}
      {c.status === "cancelled" && allowed.includes("new_inquiry") && (
        <Button variant="secondary" onClick={() => setDialog({ kind: "reopen" })}>
          <RotateCcw size={14} /> Reopen
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
          <Check size={14} /> Convert to discovery
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
        title={`Reopen ${c.code}`}
        message={
          <>
            The case goes back to <strong>New inquiry</strong> and its history is kept. Say why it is reopened.
          </>
        }
        reason={{ label: "Reason for reopening", required: true, placeholder: "The customer came back and wants to travel in spring." }}
        confirmLabel="Reopen case"
        busy={change.isPending}
        error={change.isError ? friendlyError(change.error) : null}
        onConfirm={(reason) => change.mutate({ to: "new_inquiry", reason }, { onSuccess: () => setDialog(null) })}
      />
      <ConfirmDialog
        open={dialog?.kind === "move"}
        onClose={close}
        title={dialog?.kind === "move" ? `Move to ${TRIP_CASE_STATUS_META[dialog.to].label}` : "Change status"}
        message={
          dialog?.kind === "move" ? (
            <>
              {c.code} moves from <strong>{TRIP_CASE_STATUS_META[c.status].label}</strong> to{" "}
              <strong>{TRIP_CASE_STATUS_META[dialog.to].label}</strong>. The change is recorded in the case history.
            </>
          ) : null
        }
        reason={{ label: "Note (optional)", placeholder: "Shown in the case history." }}
        confirmLabel="Change status"
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
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="inline-flex h-[34px] cursor-pointer items-center gap-1.5 rounded-full border border-accent-border px-4 text-[13px] font-semibold uppercase tracking-[0.06em] text-accent-deep hover:bg-accent-tint"
      >
        Change status <ChevronDown size={14} />
      </button>
      {open && (
        <div role="menu" className="absolute right-0 top-full z-30 mt-1 w-[260px] rounded-lg border border-line bg-card p-1.5 shadow-lg">
          {moves.length === 0 && <p className="px-3 py-2 text-[13px] text-muted">No other status is reachable from here.</p>}
          {moves.map((s) =>
            s === "confirmed" ? (
              <div key={s} role="menuitem" aria-disabled="true" className="cursor-not-allowed rounded-md px-3 py-2 text-[13px] text-muted2">
                {TRIP_CASE_STATUS_META[s].label}
                <span className="block text-[11.5px]">Approve a sent proposal version to confirm.</span>
              </div>
            ) : (
              <button
                key={s}
                type="button"
                role="menuitem"
                onClick={pick(() => onMove(s))}
                className="block w-full cursor-pointer rounded-md px-3 py-2 text-left text-[13px] text-ink hover:bg-bg"
              >
                {TRIP_CASE_STATUS_META[s].label}
              </button>
            ),
          )}
          {canCancel && (
            <>
              <div className="my-1 border-t border-line2" />
              <button
                type="button"
                role="menuitem"
                onClick={pick(onCancel)}
                className="flex w-full cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-left text-[13px] font-medium text-danger hover:bg-danger-soft"
              >
                <XCircle size={14} /> Cancel case…
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Decline (inquiry) or Cancel (later status): a reason select + notes; the API stores "Reason: notes". */
function CancelDialog({
  c,
  open,
  decline,
  onClose,
  onConfirm,
  busy,
  error,
}: {
  c: TripCaseDetail;
  open: boolean;
  decline: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
  busy: boolean;
  error: string | null;
}) {
  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} title={decline ? "Decline inquiry" : `Cancel ${c.code}`} width={520}>
      {open && <CancelBody c={c} decline={decline} onClose={onClose} onConfirm={onConfirm} busy={busy} error={error} />}
    </Modal>
  );
}

function CancelBody({
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
  const reasons = decline ? DECLINE_REASONS : CANCEL_REASONS;
  const [reason, setReason] = useState<string>(reasons[0]);
  const [notes, setNotes] = useState("");
  const needsNotes = reason === "Other";
  const text = notes.trim() ? `${reason}: ${notes.trim()}` : reason;
  return (
    <div className="flex flex-col gap-4 font-sans">
      <p className="text-sm text-body">
        {decline ? "Decline" : "Cancel"} <strong className="font-mono">{c.code}</strong> from <strong>{c.customer_name ?? "this customer"}</strong>? The case
        moves to Cancelled and keeps its history. It can be reopened later with a reason.
      </p>
      <Field label={decline ? "Reason for declining" : "Reason for cancelling"} required>
        <select className={inputCls} value={reason} onChange={(e) => setReason(e.target.value)}>
          {reasons.map((r) => (
            <option key={r}>{r}</option>
          ))}
        </select>
      </Field>
      <Field label={needsNotes ? "Notes" : "Additional notes (optional)"} required={needsNotes}>
        <textarea className={textareaCls} value={notes} maxLength={1900} onChange={(e) => setNotes(e.target.value)} placeholder="Shown in the case history." />
      </Field>
      {error && <p className="text-[13px] text-danger">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Keep case
        </Button>
        <Button variant="danger" onClick={() => onConfirm(text)} disabled={busy || (needsNotes && !notes.trim())}>
          {busy ? "Working…" : decline ? "Decline inquiry" : "Cancel case"}
        </Button>
      </div>
    </div>
  );
}

/** Convert to discovery (Figma USER DETAILS-6 "Convert to Trip Case", minus the parts that are not ours:
 * the case already exists, a template link is W2). Assign first (when the rep changes), then the status. */
function ConvertDialog({ c, open, onClose }: { c: TripCaseDetail; open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title="Convert to discovery" width={520}>
      {open && <ConvertBody c={c} onClose={onClose} />}
    </Modal>
  );
}

function ConvertBody({ c, onClose }: { c: TripCaseDetail; onClose: () => void }) {
  const me = useStaff();
  const canAssign = useCan("trip_case.assign");
  const current = c.assignees.find((a) => a.role === "sales")?.cis_user_id ?? "";
  const [rep, setRep] = useState(current || (me.role === "sales" ? me.cis_user_id : ""));
  const [note, setNote] = useState("");
  const assign = useAssign(c.code);
  const change = useChangeStatus(c.code);
  const busy = assign.isPending || change.isPending;
  const err = assign.error ?? change.error;

  async function confirm() {
    try {
      if (canAssign && rep !== current) await assign.mutateAsync({ cisUserId: rep || null, quiet: true });
      await change.mutateAsync({ to: "discovery", reason: note.trim() || undefined });
      onClose();
    } catch {
      /* shown below */
    }
  }

  return (
    <div className="flex flex-col gap-4 font-sans">
      <p className="text-sm text-body">
        Start working on this inquiry from <strong>{c.customer_name ?? "the customer"}</strong>. The case moves to Discovery: talk with the customer, then build a
        proposal.
      </p>
      <Field label="Sales rep" hint={canAssign ? undefined : "Your role cannot change the assignment."}>
        {canAssign ? <StaffSelect value={rep} onChange={setRep} /> : <input className={inputCls} disabled value={c.assignees.find((a) => a.role === "sales")?.name ?? "Unassigned"} />}
      </Field>
      <Field label="Note (optional)" hint="Saved in the case history with the status change.">
        <textarea className={textareaCls} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} placeholder="Called the customer, they want a quieter pace." />
      </Field>
      {err && <p className="text-[13px] text-danger">{friendlyError(err)}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button variant="primary" onClick={confirm} disabled={busy}>
          {busy ? "Working…" : "Convert"}
        </Button>
      </div>
    </div>
  );
}
