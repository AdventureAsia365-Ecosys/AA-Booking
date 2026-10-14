// app/(staff)/catalog/trip-plans/[sourceId]/page.tsx: Trip plan (wave 2, UI spec §3; Figma TRIP TEMPLATE DETAILS (05)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Trip plan" };

export default function CatalogTripPlansSourceidPage() {
  return (
    <WaveFrame
      wave={2}
      title="Trip plan"
      layout="detail"
      description="A CIS tour (read-only) with its Booking commercial panel."
      purpose={"The tour's days, places and photos from CIS, plus sellable state, supplier and price matrix, and \"Use for trip case\"."}
      tabs={["Overview", "Day by day", "Photos", "Prices"]}
      sections={["Itinerary (from CIS)", "Commercial", "From price"]}
      breadcrumbs={[{"label": "Trip plans", "href": "/catalog/trip-plans"}, {"label": "Trip plan"}]}
    />
  );
}
