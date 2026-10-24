// ui/booking/Code.tsx
// AA-683 — human codes (TC-000123, CUS-000123) in the mono face (UI spec §4). AA-910: a code never breaks at its
// hyphen or shrinks next to a truncated text in a narrow table cell. `wrap` is for a long LABEL in the mono face
// (an audit row's "SUP-000001 · contract · hotel · room type"): it wraps instead of widening the page.

export function Code({ children, wrap = false }: { children: React.ReactNode; wrap?: boolean }) {
  const fit = wrap ? "min-w-0 [overflow-wrap:anywhere]" : "shrink-0 whitespace-nowrap";
  return <span className={`${fit} font-mono text-[12.5px] font-medium tracking-tight text-ink`}>{children}</span>;
}
