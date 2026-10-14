// app/(staff)/services/[id]/page.tsx: Service (wave 2, UI spec §3; Figma ACTIVITY DETAILS (07)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Service" };

export default function ServicesIdPage() {
  return (
    <WaveFrame
      wave={2}
      title="Service"
      layout="detail"
      description="One commercial service: its place, supplier and rates."
      purpose="Service detail with its rates and where it is used."
      tabs={["Overview", "Rates", "Used in"]}
      sections={["Overview", "Supplier", "Rates"]}
      breadcrumbs={[{"label": "Services", "href": "/services"}, {"label": "Service"}]}
    />
  );
}
