"use client";
// app/(staff)/cases/_parts/StatusMultiSelect.tsx
// AA-683 (fe-cases) — the "Status (multi)" filter of spec §4.5. The value is a comma list in the URL key
// `status` (the API accepts `status=a,b`). A disclosure listbox (not a dialog): click outside or Escape closes.

import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { TRIP_CASE_STATUSES, type TripCaseStatus } from "@/lib/api/types";
import { STATUS_PHASES, TRIP_CASE_STATUS_META } from "@/ui";

export function parseStatuses(raw: string): TripCaseStatus[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s): s is TripCaseStatus => (TRIP_CASE_STATUSES as readonly string[]).includes(s));
}

export function StatusMultiSelect({ value, onChange }: { value: TripCaseStatus[]; onChange: (next: TripCaseStatus[]) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = (s: TripCaseStatus) => onChange(value.includes(s) ? value.filter((v) => v !== s) : [...value, s]);
  const label =
    value.length === 0 ? "All statuses" : value.length === 1 ? TRIP_CASE_STATUS_META[value[0]].label : `${value.length} statuses`;
  const groups = [...STATUS_PHASES, { key: "closed", label: "Closed", statuses: ["cancelled"] as TripCaseStatus[] }];

  return (
    // AA-996: the FilterBar shape (inline label before a 36 px control), so it shares the bar's single row.
    <div ref={ref} className="relative flex items-center gap-1.5" data-bk-filter="">
      <span className="shrink-0 whitespace-nowrap text-[12px] font-medium text-muted">Status</span>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="inline-flex h-9 min-w-[140px] cursor-pointer items-center justify-between gap-2 rounded-md border border-line bg-card px-3 text-[13px] text-ink hover:border-accent-border"
      >
        <span className="truncate">{label}</span>
        <ChevronDown size={14} className="shrink-0 text-muted" />
      </button>
      {open && (
        <div
          role="listbox"
          aria-multiselectable="true"
          aria-label="Statuses"
          className="absolute left-0 top-full z-30 mt-1 max-h-[360px] w-[240px] overflow-y-auto rounded-lg border border-line bg-card p-2 shadow-lg"
        >
          {groups.map((g) => (
            <div key={g.key} className="mb-1 last:mb-0">
              <p className="px-2 pb-1 pt-1.5 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-muted2">{g.label}</p>
              {g.statuses.map((s) => (
                <label
                  key={s}
                  role="option"
                  aria-selected={value.includes(s)}
                  className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-body hover:bg-bg"
                >
                  <input type="checkbox" checked={value.includes(s)} onChange={() => toggle(s)} className="h-4 w-4 accent-[var(--aa-accent)]" />
                  {TRIP_CASE_STATUS_META[s].label}
                </label>
              ))}
            </div>
          ))}
          {value.length > 0 && (
            <button
              type="button"
              onClick={() => onChange([])}
              className="mt-1 w-full cursor-pointer rounded-md px-2 py-1.5 text-left text-[12px] font-medium text-accent-deep hover:bg-bg"
            >
              Clear status filter
            </button>
          )}
        </div>
      )}
    </div>
  );
}
