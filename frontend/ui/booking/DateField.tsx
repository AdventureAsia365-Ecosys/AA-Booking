"use client";
// ui/booking/DateField.tsx
// AA-996 (audit slice 1, deviation 4) — the one date field and the one time field of the Booking UI.
//
// A native <input type="date"> / <input type="time"> draws its text in the browser's locale: "10/28/2026" and
// "02:05 PM" on an en-US machine, which is how the Figma audit saw every form. These fields always show the UI-spec
// format instead (en-GB, 24 h: "28 Oct 2026", "14:05", like DateText / the kit formatDate) while keeping the same value
// contract as the native inputs, so a page swaps one for the other without touching its state:
//   DateField  value / onChange: "YYYY-MM-DD" or "" (empty)
//   TimeField  value / onChange: "HH:MM" (24 h) or ""
//
// DateField: a text box (type "28/10/2026", "28 Oct 2026" or ISO; day first, never US order) plus a calendar button
// that opens the browser's own date picker through a hidden native input (showPicker), so there is no home-made
// calendar to maintain. `min` / `max` are honoured by both. A value that cannot be read stays in the box, marked
// invalid, and the field keeps its last good value.

import { useRef, useState } from "react";
import { CalendarDays } from "lucide-react";
import { formatDay, normalizeTime, parseDayInput, parseTimeInput } from "./dates";

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

/** Inline, so it wins over the page's border class whatever the CSS order. */
const INVALID: React.CSSProperties = { borderColor: "var(--aa-red)" };

function inRange(day: string, min?: string, max?: string): boolean {
  return (!min || day >= min) && (!max || day <= max);
}

export function DateField({
  value,
  onChange,
  min,
  max,
  className = "",
  wrapperClassName = "",
  placeholder = "dd/mm/yyyy",
  disabled,
  ...rest
}: BaseProps & {
  value: string | null | undefined;
  onChange: (value: string) => void;
  min?: string;
  max?: string;
}) {
  const picker = useRef<HTMLInputElement>(null);
  // What the person is typing; null = show the formatted value.
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? (value ? formatDay(value) : "");
  const parsed = draft === null ? undefined : parseDayInput(draft);
  const invalid = draft !== null && (parsed === null || (!!parsed && !inRange(parsed, min, max)));

  function commit() {
    if (draft === null) return;
    const day = parseDayInput(draft);
    if (day === "") {
      setDraft(null);
      if (value) onChange("");
    } else if (day && inRange(day, min, max)) {
      setDraft(null);
      if (day !== value) onChange(day);
    }
    // Not a day, or out of range: the text stays (marked invalid) and the value is unchanged.
  }

  function openPicker() {
    const el = picker.current;
    if (!el) return;
    try {
      el.showPicker();
    } catch {
      el.focus();
      el.click();
    }
  }

  return (
    <span className={`relative flex min-w-0 ${wrapperClassName}`} data-bk-field="date">
      <input
        {...rest}
        type="text"
        inputMode="text"
        autoComplete="off"
        value={shown}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        title={invalid ? `Not a valid date${min || max ? " in the allowed range" : ""}: use dd/mm/yyyy` : undefined}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          } else if (e.key === "Escape" && draft !== null) {
            setDraft(null);
          }
        }}
        className={className}
        style={{ paddingRight: 34, ...(invalid ? INVALID : null) }}
      />
      <button
        type="button"
        tabIndex={-1}
        disabled={disabled}
        onClick={openPicker}
        aria-label={`Pick a date${rest["aria-label"] ? ` (${rest["aria-label"]})` : ""}`}
        className="absolute right-1 top-1/2 grid h-7 w-7 -translate-y-1/2 cursor-pointer place-items-center rounded text-muted hover:text-accent-deep disabled:cursor-not-allowed disabled:opacity-50"
      >
        <CalendarDays size={15} />
      </button>
      {/* The browser's picker only: never visible, never focused by Tab; its locale text is never shown. */}
      <input
        ref={picker}
        type="date"
        tabIndex={-1}
        aria-hidden="true"
        value={value ?? ""}
        min={min}
        max={max}
        disabled={disabled}
        onChange={(e) => {
          setDraft(null);
          onChange(e.target.value);
        }}
        className="pointer-events-none absolute bottom-0 right-0 h-px w-px opacity-0"
      />
    </span>
  );
}

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
  const shown = draft ?? normalizeTime(value);
  const invalid = draft !== null && parseTimeInput(draft) === null;

  function commit() {
    if (draft === null) return;
    const t = parseTimeInput(draft);
    if (t === null) return;
    setDraft(null);
    if (t !== normalizeTime(value)) onChange(t);
  }

  return (
    <span className={`relative flex min-w-0 ${wrapperClassName}`} data-bk-field="time">
      <input
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
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          } else if (e.key === "Escape" && draft !== null) {
            setDraft(null);
          }
        }}
        className={`bk-tabular ${className}`}
        style={invalid ? INVALID : undefined}
      />
    </span>
  );
}
