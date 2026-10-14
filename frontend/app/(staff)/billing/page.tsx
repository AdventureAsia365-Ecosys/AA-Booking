// app/(staff)/billing/page.tsx: Payments & invoices (wave 3, UI spec §3; Figma PAYMENT & INVOICE (09)).
// Registered now (Nghiệp S236 decision 3); renders its layout frame and "Coming in wave 3" until built.

import type { Metadata } from "next";
import { WaveFrame } from "@/ui/booking/WaveFrame";

export const metadata: Metadata = { title: "Payments & invoices" };

export default function BillingPage() {
  return (
    <WaveFrame
      wave={3}
      title="Payments & invoices"
      layout="list"
      description="Invoices issued from approved proposals, and the payments recorded against them."
      purpose="Invoices and payments with balances and overdue tracking; payments are recorded by hand (no gateway yet, spec Q22)."
      actions={["Create invoice", "Record payment"]}
      kpis={["Revenue this year (USD)", "Outstanding", "Overdue", "Invoices sent this month"]}
      tabs={["Invoices", "Payments"]}
      filters={["Search invoice or customer", "Status", "Due date"]}
      columns={["Invoice", "Customer", "Trip case", "Issued", "Due", "Total", "Paid", "Balance", "Status"]}
    />
  );
}
