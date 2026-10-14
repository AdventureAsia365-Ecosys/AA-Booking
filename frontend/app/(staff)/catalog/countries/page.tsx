// app/(staff)/catalog/countries/page.tsx: Countries (wave 2, UI spec §3; Figma COUNTRY MANAGAMENT (06)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Countries" };

export default function CatalogCountriesPage() {
  return (
    <WaveFrame
      wave={2}
      title="Countries"
      layout="list"
      description="Countries derived from CIS places and tours, with their Booking status."
      purpose="Countries Booking sells, their area and counts. Country content (description, FAQ) waits for CIS (spec Q18)."
      filters={["Search country", "Area", "Booking status"]}
      columns={["Country", "Destinations", "Tours", "Area", "Booking status"]}
    />
  );
}
