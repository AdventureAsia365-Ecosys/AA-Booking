// app/(staff)/suppliers/page.tsx: Suppliers (wave 2, UI spec §3; Figma SUPPLIER MANAGEMENT (08)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Suppliers" };

export default function SuppliersPage() {
  return (
    <WaveFrame
      wave={2}
      title="Suppliers"
      layout="list"
      description="Hotels, transport, guides and other partners Booking buys from."
      purpose="The supplier directory with contracts and rates; contracting maintains it, sales reads it."
      actions={["Add supplier"]}
      filters={["Search name or contact", "Category", "Status", "Country"]}
      columns={["Supplier", "Categories", "Contact", "Location", "Contracts", "Status"]}
    />
  );
}
