// app/(staff)/suppliers/[id]/page.tsx: Supplier (wave 2, UI spec §3; Figma SUPPLIER DETAILS (08)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Supplier" };

export default function SuppliersIdPage() {
  return (
    <WaveFrame
      wave={2}
      title="Supplier"
      layout="detail"
      description="Profile, contracts, rates and (wave 3) bookings and bills of one supplier."
      purpose="The supplier profile with its contracts and rate sheets."
      tabs={["Profile", "Contracts", "Rates", "Bookings", "Bills"]}
      sections={["Profile", "Contact", "Contracts in force"]}
      breadcrumbs={[{"label": "Suppliers", "href": "/suppliers"}, {"label": "Supplier"}]}
    />
  );
}
