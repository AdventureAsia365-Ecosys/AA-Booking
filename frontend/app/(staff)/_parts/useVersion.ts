"use client";
// app/(staff)/_parts/useVersion.ts
// AA-683 — load a trip case and one of its proposal versions (with items) for the builder and preview.

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";

export function useVersion(code: string, n: number) {
  const tripCase = useQuery({ queryKey: ["trip-case", code], queryFn: () => api.tripCases.get(code) });
  const version = useQuery({
    queryKey: ["trip-case", code, "version", n],
    queryFn: () => api.versions.items(code, n),
    enabled: Number.isInteger(n) && n > 0,
  });
  return { tripCase, version };
}

/** Group items by the days they cover: day N → items whose range includes N (spec §4.8 day timeline). */
export function itemsByDay<T extends { day_from: number; day_to: number }>(items: T[]): { day: number; items: T[] }[] {
  const last = items.reduce((m, i) => Math.max(m, i.day_to), 0);
  const out: { day: number; items: T[] }[] = [];
  for (let d = 1; d <= last; d++) out.push({ day: d, items: items.filter((i) => i.day_from <= d && d <= i.day_to) });
  return out;
}

/** The calendar date of trip day N when the case has a start date. */
export function dayDate(start: string | null, day: number): Date | null {
  if (!start) return null;
  const d = new Date(`${start}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return null;
  d.setUTCDate(d.getUTCDate() + day - 1);
  return d;
}
