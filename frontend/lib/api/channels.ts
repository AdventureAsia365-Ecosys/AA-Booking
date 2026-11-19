// lib/api/channels.ts — the pricing channels (bk-004 engine channels, ADR 0005) with their English labels. Import-free,
// so ui/booking/viOptionLabels.test.mjs can load it. A page shows a label with `tx()` (ui/local/useTx.ts): the
// Vietnamese side is registered by ui/booking/channels.messages.ts, worded as the channel labels of Pricing rules & FX.
export const CHANNELS = [
  { value: "b2c_tailor_made", label: "B2C tailor-made" },
  { value: "fixed_departure", label: "Fixed departure" },
  { value: "b2b", label: "B2B" },
] as const;
export type Channel = (typeof CHANNELS)[number]["value"];
