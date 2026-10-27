"use client";
// ui/booking/ConfirmDialog.tsx
// AA-683 — confirm / destructive actions (UI spec §5): the kit Modal plus an optional required reason
// and danger styling. Copy names the entity and says what happens ("cancel", "deactivate", "archive"),
// never "permanently": Booking has no hard delete (Q23).
// AA-996: the Figma dialog layout of every DELETE / UNPUBLISH / BLOCK frame (docs/figma-coverage.md): a round icon on
// the left, the title and the message beside it, then a footer band with Cancel + the action, no header bar. The
// action keeps the kit's `danger` button (soft red) rather than Figma's solid red: the kit is shared with CIS.

import { useState } from "react";
import { AlertTriangle, Info } from "lucide-react";
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
  /** Ask for a reason; `required` blocks the confirm button until one is typed; `maxLength` (default 2000) matches
   * the API limit of the field the reason is sent to. */
  reason?: { label: string; required?: boolean; placeholder?: string; maxLength?: number };
  busy?: boolean;
  error?: string | null;
}) {
  // Remounted per open (Modal renders nothing when closed), so the field starts empty each time.
  return (
    <Modal open={open} onClose={busy ? () => {} : onClose} width={520}>
      {open && (
        <ConfirmBody
          title={title}
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
  title,
  onClose,
  onConfirm,
  message,
  confirmLabel,
  danger,
  reason,
  busy,
  error,
}: {
  title: string;
  onClose: () => void;
  onConfirm: (reason: string) => void;
  message: React.ReactNode;
  confirmLabel: string;
  danger: boolean;
  reason?: { label: string; required?: boolean; placeholder?: string; maxLength?: number };
  busy: boolean;
  error?: string | null;
}) {
  const [text, setText] = useState("");
  const blocked = busy || (!!reason?.required && !text.trim());
  return (
    <div className="flex flex-col font-sans" data-testid="confirm-dialog">
      <div className="flex gap-4 pb-5">
        <span
          aria-hidden="true"
          className={`grid h-11 w-11 shrink-0 place-items-center rounded-full ${
            danger ? "bg-danger-soft text-danger" : "bg-accent-tint text-accent-deep"
          }`}
        >
          {danger ? <AlertTriangle size={20} /> : <Info size={20} />}
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <div>
            <h2 className="text-[17px] font-semibold text-ink">{title}</h2>
            <div className="mt-1.5 text-sm text-body">{message}</div>
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
                maxLength={reason.maxLength ?? 2000}
                placeholder={reason.placeholder}
                className="rounded-md border border-line bg-card px-3 py-2 text-sm text-ink outline-none focus:border-accent-border"
              />
            </label>
          )}
          {error && <p className="text-[13px] text-danger">{error}</p>}
        </div>
      </div>
      <div className="-mx-5 -mb-[18px] flex flex-wrap justify-end gap-2 border-t border-line bg-panel px-5 py-3.5">
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
