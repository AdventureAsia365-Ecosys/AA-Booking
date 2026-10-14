// app/(staff)/billing/payments/[ref]/page.tsx: Payment (wave 3, UI spec §3; Figma PAYMENT DETAILS (09)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 3" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Payment" };

export default function BillingPaymentsRefPage() {
  return (
    <WaveFrame
      wave={3}
      title="Payment"
      layout="detail"
      description="One recorded payment and its evidence."
      purpose="Payment amount, method, reference and evidence; payments are voided with a reason, never deleted."
      sections={["Payment", "Invoice", "Evidence"]}
      breadcrumbs={[{"label": "Payments & invoices", "href": "/billing"}, {"label": "Payment"}]}
    />
  );
}
