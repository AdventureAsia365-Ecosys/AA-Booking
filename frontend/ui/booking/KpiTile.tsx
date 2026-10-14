// ui/booking/KpiTile.tsx
// AA-683 — dashboard / list KPI tile (UI spec §4.2, §5): label, value (Fahkwang, tabular), and a hint
// line with a tone. `value === undefined` renders a skeleton bar (loading); `null` renders "—".

import type { BookingTone } from "./StatusBadge";

const HINT_TONE: Partial<Record<BookingTone, string>> = {
  success: "text-success",
  danger: "text-danger",
  warning: "text-warning",
  info: "text-info",
  neutral: "text-muted",
  accent: "text-accent-deep",
};

export function KpiTile({
  label,
  value,
  hint,
  hintTone = "neutral",
  icon,
}: {
  label: string;
  value: React.ReactNode | undefined | null;
  hint?: React.ReactNode;
  hintTone?: BookingTone;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line bg-card p-5">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[13px] text-muted">{label}</span>
        {icon && <span className="text-accent">{icon}</span>}
      </div>
      <div className="bk-tabular font-display text-[28px] font-semibold leading-none text-ink">
        {value === undefined ? (
          <span className="block h-7 w-16 animate-pulse rounded bg-line2" aria-label="Loading" />
        ) : value === null ? (
          "—"
        ) : (
          value
        )}
      </div>
      {hint && <div className={`text-xs ${HINT_TONE[hintTone] ?? "text-muted"}`}>{hint}</div>}
    </div>
  );
}
