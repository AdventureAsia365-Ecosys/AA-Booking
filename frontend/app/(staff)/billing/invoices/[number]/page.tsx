// app/(staff)/billing/invoices/[number]/page.tsx: Invoice (wave 3, UI spec §3; Figma View Details (03), CANCEL INVOICE (09)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 3" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Invoice" };

export default function BillingInvoicesNumberPage() {
  return (
    <WaveFrame
      wave={3}
      title="Invoice"
      layout="detail"
      description="One invoice: its lines, payment schedule and payments."
      purpose="Invoice lines from the approved version, the deposit / balance schedule and the payments against it."
      sections={["Invoice lines", "Payment schedule", "Payments"]}
      breadcrumbs={[{"label": "Payments & invoices", "href": "/billing"}, {"label": "Invoice"}]}
    />
  );
}
