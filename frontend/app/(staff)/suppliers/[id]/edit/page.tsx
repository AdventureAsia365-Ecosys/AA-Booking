// app/(staff)/suppliers/[id]/edit/page.tsx: Edit supplier (wave 2, UI spec §3; Figma EDIT SUPPLIER (08)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 2" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Edit supplier" };

export default function SuppliersIdEditPage() {
  return (
    <WaveFrame
      wave={2}
      title="Edit supplier"
      layout="form"
      description="Change a supplier's company, contact or address details."
      purpose="Edit form for an existing supplier; every change is audited."
      sections={["Company", "Contact", "Address"]}
      breadcrumbs={[{"label": "Suppliers", "href": "/suppliers"}, {"label": "Edit"}]}
    />
  );
}
