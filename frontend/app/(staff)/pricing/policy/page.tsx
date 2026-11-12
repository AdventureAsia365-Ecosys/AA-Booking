// app/(staff)/pricing/policy/page.tsx: Pricing → Policy (AA-1091, bk-050, root ADR 0005b decision 15). The pricing
// policy parameters (quote validity, group size, surcharges, deposits, provisional prices ...) with maker-checker
// changes. Server shell for the metadata; the page itself is the client view in ./_parts.

import type { Metadata } from "next";
import { PolicyView } from "./_parts/PolicyView";

export const metadata: Metadata = { title: "Pricing policy" };

export default function PricingPolicyPage() {
  return <PolicyView />;
}
