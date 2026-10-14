"use client";
// ui/booking/ConfirmDialog.tsx
// AA-683 — confirm / destructive actions (UI spec §5): the kit Modal plus an optional required reason
// and danger styling. Copy names the entity and says what happens ("cancel", "deactivate", "archive"),
// never "permanently": Booking has no hard delete (Q23).

import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Modal } from "../Modal";
import { Button } from "../primitives";

export function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = "Confirm",
  danger = false,
  reason,
  busy = false,
  error,
}: {
  open: boolean;
  onClose: () => void;
  /** Receives the reason text when `reason` is set. */
  onConfirm: (reason: string) => void;
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  /** Ask for a reason; `required` blocks the confirm button until one is typed. */
  reason?: { label: string; required?: boolean; placeholder?: string };
  busy?: boolean;
  error?: string | null;
}) {
  // Remounted per open (Modal renders nothing when closed), so the field starts empty each time.
  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} title={title} width={480}>
      {open && (
        <ConfirmBody
          onClose={onClose}
          onConfirm={onConfirm}
          message={message}
          confirmLabel={confirmLabel}
          danger={danger}
          reason={reason}
          busy={busy}
          error={error}
        />
      )}
    </Modal>
  );
}

function ConfirmBody({
  onClose,
  onConfirm,
  message,
  confirmLabel,
  danger,
  reason,
  busy,
  error,
}: {
  onClose: () => void;
  onConfirm: (reason: string) => void;
  message: React.ReactNode;
  confirmLabel: string;
  danger: boolean;
  reason?: { label: string; required?: boolean; placeholder?: string };
  busy: boolean;
  error?: string | null;
}) {
  const [text, setText] = useState("");
  const blocked = busy || (!!reason?.required && !text.trim());
  return (
    <div className="flex flex-col gap-4 font-sans">
      <div className="flex gap-3">
        {danger && <AlertTriangle size={20} className="mt-0.5 shrink-0 text-danger" />}
        <div className="text-sm text-body">{message}</div>
      </div>
      {reason && (
        <label className="flex flex-col gap-1.5">
          <span className="text-[13px] font-medium text-ink">
            {reason.label}
            {reason.required && <span className="text-danger"> *</span>}
          </span>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={3}
            maxLength={2000}
            placeholder={reason.placeholder}
            className="rounded-md border border-line bg-card px-3 py-2 text-sm text-ink outline-none focus:border-accent-border"
          />
        </label>
      )}
      {error && <p className="text-[13px] text-danger">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button variant={danger ? "danger" : "primary"} onClick={() => onConfirm(text.trim())} disabled={blocked}>
          {busy ? "Working…" : confirmLabel}
        </Button>
      </div>
    </div>
  );
}
