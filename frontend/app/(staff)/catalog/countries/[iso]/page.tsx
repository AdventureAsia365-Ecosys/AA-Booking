// app/(staff)/catalog/countries/[iso]/page.tsx: Country (wave 2, UI spec §3; Figma COUNTRY DETAILS (06)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Country" };

export default function CatalogCountriesIsoPage() {
  return (
    <WaveFrame
      wave={2}
      title="Country"
      layout="detail"
      description="One country: its destinations and tours from CIS, and its Booking status."
      purpose="Destinations, tours and photos from CIS with the Booking status of the country."
      tabs={["Overview", "Destinations", "Tours"]}
      sections={["Overview", "Booking status"]}
      breadcrumbs={[{"label": "Countries", "href": "/catalog/countries"}, {"label": "Country"}]}
    />
  );
}
