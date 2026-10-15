"use client";
// app/(staff)/cases/[code]/proposals/[n]/SupplyPicker.tsx — typeahead over the active hotel room types and services a
// proposal item can book (AA-823; GET /booking/supply/options). Booking stores only the id; the API fills the item's
// net from the supplier rate in force when it is saved without a cost line.

import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { supplyApi, type SupplyOption } from "@/lib/api/supply";

const inputCls = "h-10 w-full rounded-md border border-line bg-card px-3 text-sm text-ink outline-none focus:border-accent-border";

export function SupplyPicker({ onPick }: { onPick: (o: SupplyOption) => void }) {
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const options = useQuery({
    queryKey: ["supply", "options", "pick", q],
    queryFn: () => supplyApi.options({ q, limit: 20 }),
    enabled: open && q.length >= 2,
    staleTime: 60_000,
  });

  function type(next: string) {
    setText(next);
    setOpen(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setQ(next.trim()), 250);
  }

  const items = options.data?.items ?? [];
  return (
    <div className="relative">
      <input
        className={inputCls}
        value={text}
        onChange={(e) => type(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)}
        placeholder="Type 2+ letters of a hotel, room or service"
        role="combobox"
        aria-expanded={open && q.length >= 2}
        aria-controls="supply-options"
        aria-autocomplete="list"
      />
      {open && q.length >= 2 && (
        <ul
          id="supply-options"
          role="listbox"
          className="absolute left-0 right-0 top-[calc(100%+4px)] z-20 max-h-64 overflow-y-auto rounded-md border border-line bg-card py-1 shadow-lg"
        >
          {options.isLoading && <li className="px-3 py-2 text-[13px] text-muted">Searching…</li>}
          {options.isError && <li className="px-3 py-2 text-[13px] text-danger">{(options.error as Error).message}</li>}
          {!options.isLoading && !options.isError && items.length === 0 && (
            <li className="px-3 py-2 text-[13px] text-muted">No active room type or service matches.</li>
          )}
          {items.map((o) => (
            <li key={`${o.kind}:${o.id}`} role="option" aria-selected={false}>
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onPick(o);
                  setText("");
                  setQ("");
                  setOpen(false);
                }}
                className="flex w-full cursor-pointer items-center justify-between gap-2 px-3 py-2 text-left text-[13px] hover:bg-panel"
              >
                <span className="min-w-0 truncate text-ink">
                  <span className="text-muted">{o.kind === "room_type" ? "Room · " : "Service · "}</span>
                  {o.label}
                </span>
                <span className="shrink-0 text-xs text-muted">{o.supplier_name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
