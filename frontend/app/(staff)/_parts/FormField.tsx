// app/(staff)/_parts/FormField.tsx
// AA-683 (fe-people) — a labelled form field for drawers (design system §4 "Forms": label above, 13 px 500,
// inputs 40 px, radius 8, white with a 1 px line). Shows the 422 message of the matching field inline.
// AA-994 S6d: the kit Field (same look, which S6a took from this spec) with its ARIA wiring: the control is described by
// the hint or error, invalid when there is an error, required when marked.

import type { FieldError } from "@/lib/api/client";
import { Field, fieldName } from "@/ui";

/** Layout only: the look (40 px, border, focus / disabled states) comes from the kit Input / Select (AA-994 S6d). */
export const inputCls = "w-full";

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
    <Field label={label} required={required} hint={hint} locked={locked} error={error}>
      {children}
    </Field>
  );
}

/** The 422 message for one field (`body.email` → "email"), or null. */
export function errorFor(errors: FieldError[], name: string): string | null {
  return errors.find((e) => fieldName(e) === name)?.msg ?? null;
}
