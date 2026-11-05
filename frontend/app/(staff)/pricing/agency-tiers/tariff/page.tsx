// app/(staff)/pricing/agency-tiers/tariff/page.tsx: one agency's tariff (AA-1030 G3), printable, with its CSV.
// Server shell for the metadata; the page itself is the client view in ../_parts (?partner=<id>&currency=USD).

import type { Metadata } from "next";
import { TariffView } from "../_parts/TariffView";

export const metadata: Metadata = { title: "Agency tariff" };

export default function AgencyTariffPage() {
  return <TariffView />;
}
