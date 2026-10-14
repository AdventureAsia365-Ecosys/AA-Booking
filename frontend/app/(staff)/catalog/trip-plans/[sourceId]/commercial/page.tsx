// app/(staff)/catalog/trip-plans/[sourceId]/commercial/page.tsx: Commercial settings (wave 2, UI spec §3; Figma CREATE / EDIT TEMPLATE, commercial parts (05)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Commercial settings" };

export default function CatalogTripPlansSourceidCommercialPage() {
  return (
    <WaveFrame
      wave={2}
      title="Commercial settings"
      layout="form"
      description="Make a CIS tour sellable and set its supplier, trip style and prices."
      purpose="Booking-owned fields only: sellable flag, supplier, trip style, commercial notes and the season × pax band price matrix."
      sections={["Sellable", "Supplier and trip style", "Price matrix"]}
      breadcrumbs={[{"label": "Trip plans", "href": "/catalog/trip-plans"}, {"label": "Commercial"}]}
    />
  );
}
