// app/(staff)/pricing/readiness/page.tsx: Pricing → Price readiness (AA-1029). Which trip plans have a price and why
// the others do not. Server shell for the metadata; the page itself is the client view in ./_parts.

import type { Metadata } from "next";
import { ReadinessView } from "./_parts/ReadinessView";

export const metadata: Metadata = { title: "Price readiness" };

export default function PriceReadinessPage() {
  return <ReadinessView />;
}
