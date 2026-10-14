"use client";
// lib/useOffsetPage.ts
// AA-683 — server pagination state for the kit DataTable, kept in the URL (`page` 1-based, `size`).
// Page sizes 25 / 50 / 100 (UI spec §4 "Table").

import { usePathname, useRouter, useSearchParams } from "next/navigation";

export const PAGE_SIZES = [25, 50, 100];

export function useOffsetPage(defaultSize = 25) {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const size = Number(params.get("size")) || defaultSize;
  const pageSize = PAGE_SIZES.includes(size) ? size : defaultSize;
  const page = Math.max(1, Number(params.get("page")) || 1);
  const pageIndex = page - 1;

  function onChange(nextIndex: number, nextSize: number) {
    const next = new URLSearchParams(params.toString());
    if (nextIndex > 0) next.set("page", String(nextIndex + 1));
    else next.delete("page");
    if (nextSize !== defaultSize) next.set("size", String(nextSize));
    else next.delete("size");
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }

  return { pageIndex, pageSize, offset: pageIndex * pageSize, limit: pageSize, onChange };
}
