"use client";
// app/(staff)/_parts/SavedTabs.tsx
// AA-683 — saved-filter tabs above a list (UI spec §5 SavedFilterTabs): gold text + 2 px gold underline on
// a 1 px baseline (design system §4 "Tabs"). The active tab is the `tab` URL key; switching resets the page.
// AA-910 (S250): a count that is still loading (`countPending`) keeps a 2-character slot, and every count has that
// minimum width, so the tabs do not move when the counts arrive (CLS on /cases).

import { Tabs, useUrlFilters } from "@/ui";

export function SavedTabs({
  tabs,
  param = "tab",
  active: activeKey,
  onSelect,
  ariaLabel = "Saved filters",
}: {
  tabs: { key: string; label: string; count?: number; countPending?: boolean }[];
  param?: string;
  /** AA-996: a tab that sets more than one URL key (e.g. /trip-checks Overdue = status open + due overdue) passes the
   *  active key and its own select handler; without them the tab is the `param` URL key, as before. */
  active?: string;
  onSelect?: (key: string) => void;
  /** The tab list's name for screen readers ("Trip case lists"). */
  ariaLabel?: string;
}) {
  const { get, set } = useUrlFilters();
  const active = activeKey ?? (get(param) || tabs[0]?.key) ?? "";
  // The count sits inside the label (not the kit `count`): it keeps a 2-character minimum width, and a pending count
  // keeps an empty slot of that width, so the tabs do not move when the counts arrive (AA-910).
  return (
    <Tabs
      variant="underline"
      ariaLabel={ariaLabel}
      gap={20}
      className="bk-tabstrip bk-tabstrip-line"
      tabs={tabs.map((t) => ({
        key: t.key,
        label: (
          <>
            {t.label}
            {t.count !== undefined ? (
              <span className="ml-1.5 inline-block min-w-[2ch] text-left text-xs text-muted2">{t.count}</span>
            ) : (
              t.countPending && <span aria-hidden="true" className="ml-1.5 inline-block min-w-[2ch]" />
            )}
          </>
        ),
      }))}
      active={active}
      onChange={(key) => (onSelect ? onSelect(key) : set(param, key === tabs[0]?.key ? null : key))}
    />
  );
}
