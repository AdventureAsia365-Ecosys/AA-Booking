"use client";
// ui/booking/FilterBar.tsx
// AA-683 — list filters (UI spec §4 "Filters", §5): search + selects + a created/updated date range +
// "Reset filters" (shown only when a filter is off its default). State lives in the URL query, so a
// filtered list can be bookmarked and shared; changing a filter drops the page offset.

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Search, X } from "lucide-react";

export interface FilterSelect {
  key: string;
  label: string;
  /** Label of the empty value (no filter). */
  allLabel: string;
  options: { value: string; label: string }[];
  /** Fixed width in px. Set it when the options load after the page (staff names, countries, facets): a native select
   *  sizes itself to its longest option, so it would grow when they arrive and shift the bar and the list below it
   *  (CLS, AA-910 S250). The open list still shows each option in full. */
  width?: number;
}

/** Read / write list filters in the URL query. `set` removes empty values and the `page` key. */
export function useUrlFilters() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const get = (key: string) => params.get(key) ?? "";
  function setMany(changes: Record<string, string | null>) {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("page" in changes)) next.delete("page");
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }
  return { get, set: (key: string, value: string | null) => setMany({ [key]: value }), setMany, params };
}

const inputCls =
  "h-9 rounded-md border border-line bg-card px-3 text-[13px] text-ink outline-none focus:border-accent-border";

export function FilterBar({
  search,
  selects = [],
  dateRange,
  extraKeys = [],
  children,
}: {
  search?: { key?: string; placeholder: string };
  selects?: FilterSelect[];
  dateRange?: { fromKey: string; toKey: string; label: string };
  /** URL keys owned by `children` controls: they count as active filters and "Reset filters" clears them. */
  extraKeys?: string[];
  /** Extra controls (e.g. saved-filter tabs live above; this slot is for page-specific inputs). */
  children?: React.ReactNode;
}) {
  const { get, set, setMany } = useUrlFilters();
  const searchKey = search?.key ?? "q";
  const urlQ = get(searchKey);
  const [q, setQ] = useState(urlQ);

  // Debounce the search box into the URL (~300 ms). setState only in the timer callback.
  useEffect(() => {
    if (!search) return;
    if (q === urlQ) return;
    const id = window.setTimeout(() => set(searchKey, q.trim() || null), 300);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const keys = [
    ...(search ? [searchKey] : []),
    ...selects.map((s) => s.key),
    ...(dateRange ? [dateRange.fromKey, dateRange.toKey] : []),
    ...extraKeys,
  ];
  const active = keys.some((k) => get(k));

  return (
    <div className="flex flex-wrap items-end gap-2 font-sans" role="search">
      {search && (
        <label className="relative min-w-[200px] flex-[1_1_260px]">
          <span className="sr-only">{search.placeholder}</span>
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted2" />
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={search.placeholder}
            className={`${inputCls} w-full pl-9`}
          />
        </label>
      )}
      {selects.map((s) => (
        <label key={s.key} className="flex flex-col gap-1">
          <span className="text-[11px] font-medium uppercase tracking-[0.04em] text-muted">{s.label}</span>
          <select
            value={get(s.key)}
            onChange={(e) => set(s.key, e.target.value || null)}
            className={`${inputCls} max-w-full`}
            style={s.width ? { width: s.width } : undefined}
          >
            <option value="">{s.allLabel}</option>
            {s.options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      ))}
      {dateRange && (
        <fieldset className="flex flex-col gap-1">
          <legend className="text-[11px] font-medium uppercase tracking-[0.04em] text-muted">{dateRange.label}</legend>
          <div className="flex items-center gap-1">
            <input
              type="date"
              aria-label={`${dateRange.label} from`}
              value={get(dateRange.fromKey)}
              onChange={(e) => set(dateRange.fromKey, e.target.value || null)}
              className={inputCls}
            />
            <span className="text-muted2">–</span>
            <input
              type="date"
              aria-label={`${dateRange.label} to`}
              value={get(dateRange.toKey)}
              onChange={(e) => set(dateRange.toKey, e.target.value || null)}
              className={inputCls}
            />
          </div>
        </fieldset>
      )}
      {children}
      {active && (
        <button
          type="button"
          onClick={() => {
            setQ("");
            setMany(Object.fromEntries(keys.map((k) => [k, null])));
          }}
          className="inline-flex h-9 cursor-pointer items-center gap-1 rounded-md px-2 text-[13px] font-medium text-danger hover:underline"
        >
          <X size={14} /> Reset filters
        </button>
      )}
    </div>
  );
}
