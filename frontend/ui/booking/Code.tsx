// ui/booking/Code.tsx
// AA-683 — human codes (TC-000123, CUS-000123) in the mono face (UI spec §4). AA-910: a code never breaks at its
// hyphen or shrinks next to a truncated text in a narrow table cell.

export function Code({ children }: { children: React.ReactNode }) {
  return <span className="shrink-0 whitespace-nowrap font-mono text-[12.5px] font-medium tracking-tight text-ink">{children}</span>;
}
