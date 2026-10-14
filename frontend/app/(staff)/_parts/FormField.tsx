// app/(staff)/_parts/FormField.tsx
// AA-683 (fe-people) — a labelled form field for drawers (design system §4 "Forms": label above, 13 px 500,
// inputs 40 px, radius 8, white with a 1 px line). Shows the 422 message of the matching field inline.

import { Lock } from "lucide-react";
import type { FieldError } from "@/lib/api/client";
import { fieldName } from "@/ui";

export const inputCls =
  "h-10 w-full rounded-md border border-line bg-card px-3 text-sm text-ink outline-none focus:border-accent-border disabled:cursor-not-allowed disabled:bg-panel disabled:text-muted";

export function FormField({
  label,
  required,
  hint,
  locked,
  error,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: React.ReactNode;
  /** Read-only for a reason the user should see (lock icon + the hint as the reason). */
  locked?: boolean;
  error?: string | null;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex items-center gap-1.5 text-[13px] font-medium text-ink">
        {label}
        {required && <span className="text-danger">*</span>}
        {locked && <Lock size={12} className="text-muted2" aria-label="Locked" />}
      </span>
      {children}
      {error ? <span className="text-xs text-danger">{error}</span> : hint ? <span className="text-xs text-muted">{hint}</span> : null}
    </label>
  );
}

/** The 422 message for one field (`body.email` → "email"), or null. */
export function errorFor(errors: FieldError[], name: string): string | null {
  return errors.find((e) => fieldName(e) === name)?.msg ?? null;
}
