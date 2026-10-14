// ui/booking/Code.tsx
// AA-683 — human codes (TC-000123, CUS-000123) in the mono face (UI spec §4).

export function Code({ children }: { children: React.ReactNode }) {
  return <span className="font-mono text-[12.5px] font-medium tracking-tight text-ink">{children}</span>;
}
