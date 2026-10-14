// app/(staff)/catalog/trip-plans/page.tsx: Trip plans (wave 2, UI spec §3; Figma TRIP TEMPLATES (05)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Trip plans" };

export default function CatalogTripPlansPage() {
  return (
    <WaveFrame
      wave={2}
      title="Trip plans"
      layout="grid"
      description="CIS tours, and which of them Booking sells. Content is read-only here."
      purpose="Every CIS tour with its Booking commercial state (sellable, trip style, from-price). Edit the content in CIS."
      filters={["Search title or SKU", "Country", "Sellable", "Trip style"]}
    />
  );
}
