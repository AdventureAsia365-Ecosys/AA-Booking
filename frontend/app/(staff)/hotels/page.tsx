// app/(staff)/hotels/page.tsx: Hotels (wave 2, UI spec §3; Figma HOTEL MANAGEMENT (07)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Hotels" };

export default function HotelsPage() {
  return (
    <WaveFrame
      wave={2}
      title="Hotels"
      layout="grid"
      description="Hotels Booking contracts, at places that come from CIS."
      purpose="The hotel grid with supplier, style and status; contracting edits, sales reads. Content ownership is still being decided (spec Q17)."
      actions={["Add hotel"]}
      filters={["Search hotel", "Destination", "Supplier", "Style", "Status"]}
    />
  );
}
