// app/(staff)/pricing/agency-tiers/page.tsx: Agency tiers (AA-1030, bk-045; root ADR 0019 b15).
// Server shell for the metadata; the page itself is the client view in _parts.

import type { Metadata } from "next";
import { AgencyTiersView } from "./_parts/AgencyTiersView";

export const metadata: Metadata = { title: "Agency tiers" };

export default function AgencyTiersPage() {
  return <AgencyTiersView />;
}
