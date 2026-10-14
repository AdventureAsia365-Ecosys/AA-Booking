// app/(staff)/pricing/rules/page.tsx: Pricing rules & FX (wave 2, UI spec §3 / §6.3; ADR 0005; Q19 maker-checker).
// Server shell for the metadata; the page itself is the client view in _parts (AA-810, bk-007).

import type { Metadata } from "next";
import { PricingRulesView } from "./_parts/PricingRulesView";

export const metadata: Metadata = { title: "Pricing rules & FX" };

export default function PricingRulesPage() {
  return <PricingRulesView />;
}
