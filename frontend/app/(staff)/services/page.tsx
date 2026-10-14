// app/(staff)/services/page.tsx: Services (wave 2, UI spec §3; Figma ACTIVITY MANAGEMENT (07)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Services" };

export default function ServicesPage() {
  return (
    <WaveFrame
      wave={2}
      title="Services"
      layout="list"
      description="Activities, transfers, guides, entrances and meals Booking sells, at CIS places."
      purpose="Commercial services over CIS places, with their supplier and rates. Customer-facing text stays in CIS (O7)."
      actions={["Add service"]}
      filters={["Search service", "Type", "Destination", "Supplier", "Status"]}
      columns={["Service", "Type", "Place (CIS)", "Supplier", "Max group", "Status"]}
    />
  );
}
