// app/(staff)/catalog/destinations/page.tsx: Destinations (wave 2, UI spec §3; Figma DESTINATION MANAGEMENT (06)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Destinations" };

export default function CatalogDestinationsPage() {
  return (
    <WaveFrame
      wave={2}
      title="Destinations"
      layout="list"
      description="Places from CIS and whether Booking sells them."
      purpose="CIS destinations (read-only) with their Booking status and internal notes."
      filters={["Search destination", "Country", "Booking status"]}
      columns={["Destination", "Country", "Photos", "Tours", "Booking status"]}
    />
  );
}
