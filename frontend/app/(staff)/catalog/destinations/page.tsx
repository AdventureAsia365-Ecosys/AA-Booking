// app/(staff)/catalog/destinations/page.tsx: Destinations (UI spec §3; Figma DESTINATION MANAGEMENT (06); AA-996).
// Server shell for the metadata; the list is the client view in _parts (CIS places read live + bk-029 Booking state).

import type { Metadata } from "next";
import { DestinationsView } from "./_parts/DestinationsView";

export const metadata: Metadata = { title: "Destinations" };

export default function CatalogDestinationsPage() {
  return <DestinationsView />;
}
