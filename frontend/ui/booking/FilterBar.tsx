"use client";
// ui/booking/FilterBar.tsx
// AA-683 — list filters (UI spec §4 "Filters", §5): search + selects + a created/updated date range +
// "Reset filters". State lives in the URL query, so a filtered list can be bookmarked and shared; changing a filter
// drops the page offset.
//
// AA-996 (audit slice 1, deviation 10; Figma USER MANAGEMENT): one fixed shape on every list instead of a bar that
// wraps wherever its width runs out (the audit found a lone Sort / Status select on a second row and the table tools
// floating on a row of their own). Row 1: the search box, then the tools at the right (the table's Columns / CSV when
// the bar is in a DataTable toolbar, plus the `tools` slot). Row 2: every filter with its label inline ("Status [All
// statuses]"), all 36 px high, and "Reset filters" at the right. "Reset filters" is always drawn (disabled while every
// filter is at its default), so the bar never changes width when a filter is set. On a list page the bar goes into the
// table toolbar through the DataTable `filters` prop: there its root is `display: contents`, so row 1 shares the
// toolbar line with the kit's Columns / CSV buttons and row 2 (`order: 2`, full width) drops below them. Date ranges use
// the en-GB DateField ("28 Oct 2026"), never the browser's locale input.

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Search } from "lucide-react";
import { DateField } from "./DateField";

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

/** The look of every filter control (36 px high), exported for page-specific controls passed as children. */
export const filterControlCls =
  "h-9 rounded-md border border-line bg-card px-3 text-[13px] text-ink outline-none focus:border-accent-border";

/** An inline label + control pair, the one shape of every filter (use it for page-specific controls too). */
export function FilterField({ label, children, as = "label" }: { label: string; children: React.ReactNode; as?: "label" | "div" }) {
  const Tag = as;
  return (
    <Tag className="flex min-w-0 max-w-full items-center gap-1.5" data-bk-filter="">
      <span className="shrink-0 whitespace-nowrap text-[12px] font-medium text-muted">{label}</span>
      {children}
    </Tag>
  );
}

export function FilterBar({
  search,
  selects = [],
  dateRange,
  extraKeys = [],
  tools,
  children,
}: {
  search?: { key?: string; placeholder: string };
  selects?: FilterSelect[];
  dateRange?: { fromKey: string; toKey: string; label: string };
  /** URL keys owned by `children` controls: they count as active filters and "Reset filters" clears them. */
  extraKeys?: string[];
  /** Tools at the right of row 1, after the search box (e.g. Export CSV on a list that has no DataTable toolbar). */
  tools?: React.ReactNode;
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
  const active = keys.some((k) => get(k)) || q.trim() !== "";

  const hasRow2 = selects.length > 0 || !!dateRange || !!children || keys.length > 0;
  return (
    <div className="bk-filterbar flex flex-wrap items-center gap-x-3 gap-y-3 font-sans" role="search" data-testid="bk-filterbar">
      {search && (
        <label className="relative min-w-[200px] max-w-[420px] flex-[1_1_240px]" data-bk-filter-row="1">
          <span className="sr-only">{search.placeholder}</span>
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted2" />
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={search.placeholder}
            className={`${filterControlCls} w-full pl-9`}
          />
        </label>
      )}
      {tools && (
        <span className="order-1 ml-auto flex items-center gap-2" data-bk-filter-row="1">
          {tools}
        </span>
      )}
      {hasRow2 && (
        <div className="order-2 flex min-w-0 basis-full flex-wrap items-center gap-x-2.5 gap-y-2" data-bk-filter-row="2">
          {selects.map((s) => (
            <FilterField key={s.key} label={s.label}>
              <select
                value={get(s.key)}
                onChange={(e) => set(s.key, e.target.value || null)}
                className={`${filterControlCls} min-w-0 max-w-full`}
                style={s.width ? { width: s.width } : undefined}
              >
                <option value="">{s.allLabel}</option>
                {s.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </FilterField>
          ))}
          {dateRange && (
            <FilterField label={dateRange.label} as="div">
              <span className="flex min-w-0 items-center gap-1">
                <DateField
                  aria-label={`${dateRange.label} from`}
                  placeholder="From"
                  value={get(dateRange.fromKey)}
                  max={get(dateRange.toKey) || undefined}
                  onChange={(v) => set(dateRange.fromKey, v || null)}
                  className={`${filterControlCls} w-[132px]`}
                />
                <span className="text-muted2">–</span>
                <DateField
                  aria-label={`${dateRange.label} to`}
                  placeholder="To"
                  value={get(dateRange.toKey)}
                  min={get(dateRange.fromKey) || undefined}
                  onChange={(v) => set(dateRange.toKey, v || null)}
                  className={`${filterControlCls} w-[132px]`}
                />
              </span>
            </FilterField>
          )}
          {children}
          {keys.length > 0 && (
            <button
              type="button"
              disabled={!active}
              data-testid="bk-filter-reset"
              onClick={() => {
                setQ("");
                setMany(Object.fromEntries(keys.map((k) => [k, null])));
              }}
              className="ml-auto inline-flex h-9 cursor-pointer items-center gap-1 whitespace-nowrap rounded-md px-1 text-[13px] font-medium text-danger hover:underline disabled:cursor-default disabled:text-muted2 disabled:no-underline"
            >
              Reset filters
            </button>
          )}
        </div>
      )}
    </div>
  );
}
