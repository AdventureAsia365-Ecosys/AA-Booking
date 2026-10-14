// ui/booking/DateText.tsx
// AA-683 — one date format everywhere (kit formatDate / formatDateTime, en-GB, 24 h: "9 Oct 2026",
// "9 Oct 2026, 14:05"; spec §1.2 row "Dates"). `relative` adds "3 h ago" for list "Updated" columns.

import { formatDate, formatDateTime } from "../tokens";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "just now", "5 min ago", "3 h ago", "2 d ago"; older than 7 days → the absolute date. */
export function relativeTime(value: string | Date, now: number = Date.now()): string {
  const t = value instanceof Date ? value.getTime() : new Date(value).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = now - t;
  if (diff < MINUTE) return "just now";
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} min ago`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)} h ago`;
  if (diff < 7 * DAY) return `${Math.floor(diff / DAY)} d ago`;
  return formatDate(t);
}

export function DateText({
  value,
  withTime = false,
  relative = false,
  className,
}: {
  value: string | Date | null | undefined;
  withTime?: boolean;
  relative?: boolean;
  className?: string;
}) {
  if (value == null || value === "") return <span className={className}>—</span>;
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return <span className={className}>—</span>;
  const absolute = withTime ? formatDateTime(d) : formatDate(d);
  return (
    <time dateTime={d.toISOString()} title={relative ? formatDateTime(d) : undefined} className={className}>
      {relative ? relativeTime(d) : absolute}
    </time>
  );
}
