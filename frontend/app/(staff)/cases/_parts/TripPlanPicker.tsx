"use client";
// app/(staff)/cases/_parts/TripPlanPicker.tsx
// AA-996 (Figma audit slice 2) — "Start from a trip plan" for /cases/new and the Convert inquiry dialog (Figma ADD
// NEW TRIP / USER DETAILS-6 "Template" select; W2 plans are live). A debounced search over the SELLABLE trip plans
// (GET /booking/catalog/trip-plans?sellable=true, title / SKU / CIS source id); with no search it lists the first
// sellable plans. The picked plan seeds V1 of the case from its CIS days through
// POST /booking/catalog/trip-plans/{source_id}/trip-cases (the page or dialog makes that call).

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Map as MapIcon, Search } from "lucide-react";
import { tripPlansApi, type TripPlanListItem } from "@/lib/api/tripPlans";
import { CisPhoto } from "@/ui";
import { daysNights, planSubtitle } from "@/ui/booking/tripPlans";
import { inputCls } from "./fields";

const PAGE = 8;

function PlanLine({ plan }: { plan: TripPlanListItem }) {
  const meta = [plan.country, daysNights(plan.duration_days)].filter(Boolean).join(" · ");
  return (
    <span className="flex min-w-0 items-center gap-3">
      <CisPhoto path={plan.cover_photo_path} alt="" className="h-9 w-12 shrink-0 rounded-md" />
      <span className="min-w-0">
        <span className="block truncate text-[13px] font-medium text-ink" title={plan.title}>
          {plan.title}
        </span>
        <span className="block truncate text-xs text-muted">{[meta, planSubtitle(plan)].filter(Boolean).join(" · ")}</span>
      </span>
    </span>
  );
}

export function TripPlanPicker({
  value,
  onChange,
  disabled = false,
}: {
  value: TripPlanListItem | null;
  onChange: (plan: TripPlanListItem | null) => void;
  disabled?: boolean;
}) {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(search.trim()), 300);
    return () => window.clearTimeout(id);
  }, [search]);

  const results = useQuery({
    queryKey: ["trip-plans", "picker", debounced],
    queryFn: () => tripPlansApi.list({ q: debounced || undefined, sellable: true, sort: "title", limit: PAGE }),
    enabled: open && !value,
    staleTime: 30_000,
  });

  if (value) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-md border border-accent-border bg-accent-tint px-3 py-2.5" data-testid="trip-plan-picked">
        <PlanLine plan={value} />
        <button
          type="button"
          onClick={() => onChange(null)}
          disabled={disabled}
          className="shrink-0 cursor-pointer text-[13px] font-semibold text-accent-deep hover:underline disabled:cursor-not-allowed disabled:opacity-60"
        >
          Change
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <label className="relative">
        <span className="sr-only">Search sellable trip plans</span>
        <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted2" />
        <input
          className={`${inputCls} pl-9`}
          placeholder="Search sellable trip plans by title, SKU or CIS id"
          value={search}
          disabled={disabled}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setOpen(true);
            setSearch(e.target.value);
          }}
          autoComplete="off"
        />
      </label>
      {open && (
        <div className="max-h-72 overflow-y-auto rounded-md border border-line" aria-live="polite" role="listbox" aria-label="Sellable trip plans">
          {results.isLoading ? (
            <p className="px-3 py-3 text-[13px] text-muted">Searching…</p>
          ) : results.isError ? (
            <p className="px-3 py-3 text-[13px] text-danger">{(results.error as Error).message}</p>
          ) : results.data?.items.length ? (
            results.data.items.map((p) => (
              <button
                key={p.source_id}
                type="button"
                role="option"
                aria-selected={false}
                onClick={() => {
                  onChange(p);
                  setOpen(false);
                }}
                className="flex w-full cursor-pointer items-center border-b border-line2 px-3 py-2 text-left last:border-b-0 hover:bg-panel"
              >
                <PlanLine plan={p} />
              </button>
            ))
          ) : (
            <p className="flex items-center gap-2 px-3 py-3 text-[13px] text-muted">
              <MapIcon size={14} /> No sellable trip plan matches. Mark a plan sellable in Catalog → Trip plans first.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
