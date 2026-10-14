"use client";
// ui/booking/FormDrawer.tsx
// AA-683 — edits in a side drawer (UI spec §5): the kit Drawer + a <form>, a Cancel / Save footer, a
// dirty guard (closing with unsaved changes asks first) and the field errors of a 422 answer.

import { Drawer } from "../Drawer";
import { Button } from "../primitives";
import type { FieldError } from "@/lib/api/client";

/** "body.pax" → "pax": the field name of a FastAPI 422 error location. */
export function fieldName(err: FieldError): string {
  return err.loc.filter((p) => p !== "body").join(".");
}

export function FormDrawer({
  open,
  onClose,
  title,
  onSubmit,
  children,
  submitLabel = "Save",
  busy = false,
  dirty = false,
  error,
  fieldErrors = [],
  width = 480,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  onSubmit: () => void;
  children: React.ReactNode;
  submitLabel?: string;
  busy?: boolean;
  dirty?: boolean;
  error?: string | null;
  fieldErrors?: FieldError[];
  width?: number;
}) {
  function requestClose() {
    if (busy) return;
    if (dirty && !window.confirm("Discard your unsaved changes?")) return;
    onClose();
  }
  return (
    <Drawer
      open={open}
      onClose={requestClose}
      title={title}
      width={width}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={requestClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => !busy && onSubmit()} disabled={busy}>
            {busy ? "Saving…" : submitLabel}
          </Button>
        </div>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy) onSubmit();
        }}
        className="flex flex-col gap-4 font-sans"
      >
        {children}
        {(error || fieldErrors.length > 0) && (
          <div role="alert" className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
            {error && <p>{error}</p>}
            {fieldErrors.length > 0 && (
              <ul className="mt-1 list-disc pl-4">
                {fieldErrors.map((f, i) => (
                  <li key={i}>
                    <span className="font-medium">{fieldName(f) || "form"}</span>: {f.msg}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {/* Enter in a field submits; the footer Save button calls onSubmit directly (it sits outside the form). */}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Drawer>
  );
}
