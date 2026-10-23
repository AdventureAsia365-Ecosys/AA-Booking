"use client";
// app/(staff)/_parts/SavedTabs.tsx
// AA-683 — saved-filter tabs above a list (UI spec §5 SavedFilterTabs): gold text + 2 px gold underline on
// a 1 px baseline (design system §4 "Tabs"). The active tab is the `tab` URL key; switching resets the page.

import { useUrlFilters } from "@/ui";

export function SavedTabs({ tabs, param = "tab" }: { tabs: { key: string; label: string; count?: number }[]; param?: string }) {
  const { get, set } = useUrlFilters();
  const active = get(param) || tabs[0]?.key;
  return (
    <div role="tablist" className="bk-tabstrip bk-tabstrip-line flex gap-5">
      {tabs.map((t, i) => {
        const on = t.key === active;
        return (
          <button
            key={t.key}
            role="tab"
            aria-selected={on}
            onClick={() => set(param, i === 0 ? null : t.key)}
            className={`shrink-0 cursor-pointer border-b-2 pb-2.5 text-[13px] transition ${
              on ? "border-accent font-semibold text-accent-deep" : "border-transparent text-muted hover:text-ink"
            }`}
          >
            {t.label}
            {t.count !== undefined && <span className="ml-1.5 text-xs text-muted2">{t.count}</span>}
          </button>
        );
      })}
    </div>
  );
}
