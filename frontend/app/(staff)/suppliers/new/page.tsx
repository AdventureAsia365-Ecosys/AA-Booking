// app/(staff)/suppliers/new/page.tsx: New supplier (wave 2, UI spec §3; Figma ADD SUPPLIER (08)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "New supplier" };

export default function SuppliersNewPage() {
  return (
    <WaveFrame
      wave={2}
      title="New supplier"
      layout="form"
      description="Add a supplier Booking will buy services from."
      purpose="Company, contact and address of a new supplier; contracts and rates are added on its page."
      sections={["Company", "Contact", "Address"]}
      breadcrumbs={[{"label": "Suppliers", "href": "/suppliers"}, {"label": "New"}]}
    />
  );
}
