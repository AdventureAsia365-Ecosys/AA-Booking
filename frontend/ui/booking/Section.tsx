// ui/booking/Section.tsx
// AA-683 — a white card region with an optional heading and actions (design system §4 "Cards": white,
// 1 px --aa-line, radius 12, 24 px padding, no double frame). Page layouts are built from these.

export function Section({
  title,
  description,
  actions,
  children,
  padded = true,
  className = "",
}: {
  title?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  children?: React.ReactNode;
  padded?: boolean;
  className?: string;
}) {
  return (
    <section className={`min-w-0 rounded-lg border border-line bg-card ${className}`}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line2 px-5 py-4">
          <div className="min-w-0">
            {title && <h2 className="text-[15px] font-semibold text-ink">{title}</h2>}
            {description && <p className="mt-0.5 text-[13px] text-muted">{description}</p>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={padded ? "p-5" : ""}>{children}</div>
    </section>
  );
}
