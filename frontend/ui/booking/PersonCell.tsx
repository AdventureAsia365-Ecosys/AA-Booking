// ui/booking/PersonCell.tsx
// AA-683 — initials avatar + name + sub-line + chips, for list cells and People cards (UI spec §5).

export function initialsOf(name: string | null | undefined): string {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean).slice(0, 2);
  return parts.map((p) => p[0]!.toUpperCase()).join("") || "?";
}

export function PersonCell({
  name,
  sub,
  chips,
  className = "",
}: {
  name: string | null | undefined;
  sub?: string | null;
  chips?: React.ReactNode;
  /** AA-910: e.g. `max-w-[240px]`. A table cell grows to its content, so `truncate` only cuts with a max width. */
  className?: string;
}) {
  return (
    <span className={`flex min-w-0 items-center gap-2.5 ${className}`}>
      <span
        aria-hidden="true"
        className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-accent-tint text-[11px] font-bold text-accent-deep"
      >
        {initialsOf(name)}
      </span>
      <span className="min-w-0">
        <span className="flex items-center gap-1.5">
          <span className="min-w-0 truncate text-[13px] font-medium text-ink" title={name || undefined}>
            {name || "Unnamed"}
          </span>
          {chips}
        </span>
        {sub && (
          <span className="block truncate text-xs text-muted" title={sub}>
            {sub}
          </span>
        )}
      </span>
    </span>
  );
}
