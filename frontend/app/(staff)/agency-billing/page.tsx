// app/(staff)/agency-billing/page.tsx: Operations → Agency billing (AA-1037 R4, bk-041 / bk-042; root ADR 0019 d9).
// Server shell for the metadata; the view is the client part in _parts.

import type { Metadata } from "next";
import { AgencyBillingView } from "./_parts/AgencyBillingView";

export const metadata: Metadata = { title: "Agency billing" };

export default function AgencyBillingPage() {
  return <AgencyBillingView />;
}
