"use client";
// app/(staff)/_parts/SavedTabs.tsx
// AA-683 — saved-filter tabs above a list (UI spec §5 SavedFilterTabs): gold text + 2 px gold underline on
// a 1 px baseline (design system §4 "Tabs"). The active tab is the `tab` URL key; switching resets the page.
// AA-910 (S250): a count that is still loading (`countPending`) keeps a 2-character slot, and every count has that
// minimum width, so the tabs do not move when the counts arrive (CLS on /cases).

import { useUrlFilters } from "@/ui";

export function SavedTabs({
  tabs,
  param = "tab",
}: {
  tabs: { key: string; label: string; count?: number; countPending?: boolean }[];
  param?: string;
}) {
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
            {t.count !== undefined ? (
              <span className="ml-1.5 inline-block min-w-[2ch] text-left text-xs text-muted2">{t.count}</span>
            ) : (
              t.countPending && <span aria-hidden="true" className="ml-1.5 inline-block min-w-[2ch]" />
            )}
          </button>
        );
      })}
    </div>
  );
}
