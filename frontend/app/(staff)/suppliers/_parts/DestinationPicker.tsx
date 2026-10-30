"use client";
// app/(staff)/suppliers/_parts/DestinationPicker.tsx — the supplier's city / place (AA-822; module spec 08 conflict
// 5): a typeahead over the CIS golden record of places (GET /booking/catalog/destinations → catalog.v_destination).
// Booking stores only the destination id; names are CIS content (From CIS). Narrowed to the form's country when one
// is picked. Picking a place hands its ISO country back so the form can fill an empty country.

import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { MapPin, X } from "lucide-react";
import { suppliersApi, type CatalogPlace } from "@/lib/api/suppliers";
import { Input, FromCisTag, IconButton, ListButton } from "@/ui";
import { inputCls } from "@/app/(staff)/_parts/FormField";

export interface PickedPlace {
  id: string;
  name: string;
  country: string;
}

export function DestinationPicker({
  value,
  country,
  onChange,
}: {
  value: PickedPlace | null;
  /** ISO-2 of the form's country, to narrow the search. */
  country: string;
  onChange: (place: CatalogPlace | null) => void;
}) {
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  const places = useQuery({
    queryKey: ["catalog", "destinations", q, country],
    queryFn: () => suppliersApi.places({ q, country: country || undefined, limit: 20 }),
    enabled: open && q.length >= 2,
    staleTime: 5 * 60_000,
  });

  function type(next: string) {
    setText(next);
    setOpen(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setQ(next.trim()), 250);
  }

  if (value) {
    return (
      <div className="flex h-10 items-center justify-between gap-2 rounded-md border border-line bg-panel px-3 text-sm">
        <span className="flex min-w-0 items-center gap-2">
          <MapPin size={14} className="shrink-0 text-muted2" />
          <span className="truncate text-ink">
            {value.name}, {value.country}
          </span>
          <FromCisTag />
        </span>
        <IconButton variant="plain" size="xs" round onClick={() => onChange(null)} label="Clear the place">
          <X size={14} />
        </IconButton>
      </div>
    );
  }

  const items = places.data?.items ?? [];
  return (
    <div className="relative">
      <Input
        className={inputCls}
        value={text}
        onChange={(e) => type(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)}
        placeholder="Type 2+ letters of a city or place"
        role="combobox"
        aria-expanded={open && q.length >= 2}
        aria-controls="destination-options"
        aria-autocomplete="list"
      />
      {open && q.length >= 2 && (
        <ul
          id="destination-options"
          role="listbox"
          className="absolute left-0 right-0 top-[calc(100%+4px)] z-20 max-h-64 overflow-y-auto rounded-md border border-line bg-card py-1 shadow-lg"
        >
          {places.isLoading && <li className="px-3 py-2 text-[13px] text-muted">Searching…</li>}
          {places.isError && <li className="px-3 py-2 text-[13px] text-danger">{(places.error as Error).message}</li>}
          {!places.isLoading && !places.isError && items.length === 0 && (
            <li className="px-3 py-2 text-[13px] text-muted">No CIS place matches{country ? " in this country" : ""}.</li>
          )}
          {items.map((p) => (
            <li key={p.destination_id} role="presentation">
              <ListButton
                role="option"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onChange(p);
                  setText("");
                  setQ("");
                  setOpen(false);
                }}
                className="justify-between"
                style={{ borderRadius: 0 }}
              >
                <span className="truncate text-ink">{p.name}</span>
                <span className="shrink-0 text-xs text-muted">{p.country}</span>
              </ListButton>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
