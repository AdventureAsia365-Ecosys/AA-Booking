"use client";
// ui/booking/TimeField.tsx
// AA-996 (audit slice 1, deviation 4) — the one time field of the Booking UI. A native <input type="time"> draws its
// text in the browser's locale ("02:05 PM" on an en-US machine); this field always shows the UI-spec format (24 h,
// "14:05") while keeping the native value contract: value / onChange "HH:MM" (24 h) or "".
// The date field moved to the kit core in AA-994 S4a (ui/core/DateField.tsx, `DateField` from "@/ui"); the time field
// stays here until the kit form controls (AA-994 S6a). AA-994 S6d: the text box is the kit Input (its invalid state
// draws the danger border from `aria-invalid`).

import { useState } from "react";
import { Input } from "../core/FormControls";
import { normalizeTime, parseTimeInput } from "./dates";

type BaseProps = {
  /** Classes of the text box: pass the page's input class (border, height, width) as for the native input. */
  className?: string;
  /** Classes of the wrapper (layout in a flex row or grid). */
  wrapperClassName?: string;
  id?: string;
  name?: string;
  disabled?: boolean;
  required?: boolean;
  "aria-label"?: string;
  placeholder?: string;
};

/** 24-hour time: type "14:05", "1405" or "2:05 pm"; it always shows "14:05". */
export function TimeField({
  value,
  onChange,
  className = "",
  wrapperClassName = "",
  placeholder = "HH:MM",
  disabled,
  ...rest
}: BaseProps & {
  value: string | null | undefined;
  onChange: (value: string) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    setDraft(null);
  }
  const shown = draft ?? normalizeTime(value);
  const invalid = draft !== null && parseTimeInput(draft) === null;

  /** Same rule as the kit DateField: Enter keeps unreadable text to fix, blur drops it, so the box always shows the value. */
  function commit(onBlur: boolean) {
    if (draft === null) return;
    const t = parseTimeInput(draft);
    if (t === null) {
      if (onBlur) setDraft(null);
      return;
    }
    setDraft(null);
    if (t !== normalizeTime(value)) onChange(t);
  }

  return (
    <span className={`relative flex min-w-0 ${wrapperClassName}`} data-bk-field="time">
      <Input
        {...rest}
        type="text"
        inputMode="text"
        autoComplete="off"
        maxLength={10}
        value={shown}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        title={invalid ? "Not a valid time: use 24-hour HH:MM" : undefined}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => commit(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit(false);
          } else if (e.key === "Escape" && draft !== null) {
            setDraft(null);
          }
        }}
        className={`bk-tabular ${className}`}
      />
    </span>
  );
}
