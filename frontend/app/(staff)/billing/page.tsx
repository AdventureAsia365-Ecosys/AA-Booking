"use client";
// app/(staff)/billing/page.tsx — Supplier bills (AA-682 S4, bk-030; S252 design §6, Figma PAYMENT & INVOICE (09)): AA's
// own payables. Four KPIs (to approve, to pay, overdue, paid out this month), the bill list and the bill drawer
// (bill.read = admin, sales, contracting). Another person approves each bill; an admin records the payouts.
// The customer invoices and payments tabs (AA-682 S3, bk-028) are retired with the B2C half of Booking (AA-1046, root
// ADR 0019: agencies are the only customers; agency money is /agency-billing). An old link with ?tab=invoices or
// ?tab=payments lands on the supplier bills.

import { EmptyState, PageFrame, useCan } from "@/ui";
import { SupplierBillKpis, SupplierBillsList } from "./_parts/SupplierBills";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./page.messages";

export default function BillingPage() {
  const t = useT(MSG);
  const canBills = useCan("bill.read");
  return (
    <PageFrame
      title={t("Supplier bills")}
      description={t("Bills suppliers sent AA, matched to confirmed supplier bookings. Another person approves each bill; an admin records the payouts.")}
    >
      {canBills ? (
        <>
          <SupplierBillKpis />
          <SupplierBillsList />
        </>
      ) : (
        <EmptyState title={t("No access")} description={t("Supplier bills need the bill reader permission (admin, sales or contracting).")} />
      )}
    </PageFrame>
  );
}
