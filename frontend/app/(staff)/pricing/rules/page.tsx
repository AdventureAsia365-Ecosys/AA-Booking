// app/(staff)/pricing/rules/page.tsx: Pricing rules & FX (wave 2, UI spec §3; not drawn (ADR 0005)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Pricing rules & FX" };

export default function PricingRulesPage() {
  return (
    <WaveFrame
      wave={2}
      title="Pricing rules & FX"
      layout="list"
      description="Margins, peak uplift, card fee and FX rates. Append-only; a second person approves each row."
      purpose="Pricing rules and FX rates in force, their history, and approval by a second person (ADR 0005, spec Q19)."
      actions={["New rule", "New FX rate"]}
      tabs={["Pricing rules", "FX rates"]}
      columns={["Channel", "Target margin", "Floor margin", "Peak uplift", "Card fee", "Valid from", "Approved by", "Status"]}
    />
  );
}
