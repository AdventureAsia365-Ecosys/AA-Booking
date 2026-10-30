// app/(staff)/cases/_parts/fields.tsx
// AA-683 (fe-cases) — form atoms shared by the trip case pages: a labelled field (label above, 13 px / 500,
// design system §4 "Forms"), the input class, and the option lists that are configuration, not data.

import { Lock } from "lucide-react";
import { Field as KitField } from "@/ui";

// Layout only: the look (40 px, border, focus / disabled states) comes from the kit Input / Select / Textarea (AA-994 S6d).
export const inputCls = "w-full";
export const textareaCls = "min-h-[96px] w-full";

/** Spec §4.6: USD default; the five currencies sales quote in today. */
export const CURRENCIES = ["USD", "EUR", "GBP", "AUD", "SGD"] as const;

/** Spec §4.7 "Decline" (Figma USER DETAILS-7): the reason select; the text goes into the status note. */
export const DECLINE_REASONS = [
  "Budget too low",
  "Not our destination",
  "Duplicate inquiry",
  "No response from the customer",
  "Other",
] as const;

/** The kit Field (AA-994 S6d): label above, hint or error under, ARIA wiring to the control. */
export function Field({
  label,
  required,
  hint,
  error,
  children,
  className = "",
}: {
  label: string;
  required?: boolean;
  hint?: React.ReactNode;
  error?: string | null;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <KitField label={label} required={required} hint={hint} error={error} className={className || undefined}>
      {children}
    </KitField>
  );
}

/** A read-only value shown as a disabled input with a lock (Figma ADD NEW ACTIVITY-2 "customer locked"). */
export function LockedValue({ value, hint }: { value: React.ReactNode; hint?: string }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex h-10 items-center justify-between gap-2 rounded-md border border-line bg-panel px-3 text-sm text-muted">
        <span className="truncate">{value}</span>
        <Lock size={14} className="shrink-0 text-muted2" aria-hidden="true" />
      </div>
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </div>
  );
}

/** Integer from a number input, or null when blank / not a whole number. */
export function toInt(value: string): number | null {
  if (value.trim() === "") return null;
  const n = Number(value);
  return Number.isInteger(n) ? n : null;
}

/** The pax rules of the API (CasePatch / CaseCreate): adults 1–999, children 0–998, total ≤ 999. */
export function paxErrors(adultsRaw: string, childrenRaw: string): { adults?: string; children?: string } {
  const adults = toInt(adultsRaw);
  const children = childrenRaw.trim() === "" ? 0 : toInt(childrenRaw);
  const out: { adults?: string; children?: string } = {};
  if (adults === null || adults < 1 || adults > 999) out.adults = "Adults must be a whole number from 1 to 999.";
  if (children === null || children < 0 || children > 998) out.children = "Children must be a whole number from 0 to 998.";
  else if (adults !== null && adults + children > 999) out.children = "Adults plus children must be 999 or fewer.";
  return out;
}
