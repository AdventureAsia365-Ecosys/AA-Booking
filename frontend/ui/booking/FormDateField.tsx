"use client";
// ui/booking/FormDateField.tsx
// AA-994 S6d review — the kit DateField with the kit form-control look, exported from "@/ui" as `DateField`.
//
// The kit DateField (ui/core/DateField.tsx) has no look of its own: its text box takes only the caller's `style` /
// `className`, the way FilterBar passes `filterControlStyle`. Booking forms used to pass a full Tailwind class
// (h-10, border, background, padding); since S6d the shared form classes are layout only ("w-full"), because the kit
// Input / Select draw the look. Without this wrapper every form date box rendered bare under Tailwind preflight (no
// border, transparent, auto height). Here the box gets the same `controlStyle(size)` and `CONTROL_CLASS` (focus,
// invalid, disabled, placeholder states) as the Input beside it; pass `size="sm"` where the sibling Inputs are small.
// The caller's `style` and `className` still apply on top (layout). Pages never import ui/core directly.

import type { ComponentProps } from "react";
import { DateField as KitDateField } from "../core/DateField";
import { CONTROL_CLASS, controlStyle, type ControlSize } from "../core/FormControls";

export type FormDateFieldProps = ComponentProps<typeof KitDateField> & {
  /** "md" = 40 px like a kit Input (default); "sm" = 36 px, next to `size="sm"` Inputs. */
  size?: ControlSize;
};

export function FormDateField({ size = "md", className, style, ...rest }: FormDateFieldProps) {
  return (
    <KitDateField
      {...rest}
      className={className ? `${CONTROL_CLASS} ${className}` : CONTROL_CLASS}
      style={{ ...controlStyle(size), ...style }}
    />
  );
}
