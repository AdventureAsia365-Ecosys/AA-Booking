"use client";
// app/(staff)/billing/invoices/[number]/page.tsx — Invoice (AA-682 S3, bk-028; S252 design §6, UI spec §3, Figma View
// Details (03), CANCEL INVOICE (09)). One invoice: state, money, lines, payment schedule, payments and the actions of
// its state (send, edit / delete a draft, record payment, verify, void, amend, remind, cancel). GET
// /booking/invoices/{number} (billing.read). "Delete" in the Figma = delete a draft / cancel a sent invoice (Q23).

import { use } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { billingApi } from "@/lib/api/billing";
import { EmptyState, PageFrame, Skeleton, TripCaseStatusBadge, useCan } from "@/ui";
import { QueryError } from "../../../cases/_parts/QueryError";
import { InvoiceView, useBillingRefresh } from "../../_parts/InvoiceView";

export default function InvoicePage({ params }: { params: Promise<{ number: string }> }) {
  const { number } = use(params);
  const ref = decodeURIComponent(number);
  const canRead = useCan("billing.read");
  const q = useQuery({ queryKey: ["billing", "invoice", ref], queryFn: () => billingApi.invoice(ref), enabled: canRead });
  const refresh = useBillingRefresh(q.data?.trip_case.code);
  const crumbs = [{ label: "Payments & invoices", href: "/billing" }, { label: ref }];
  if (!canRead) {
    return (
      <PageFrame title="Invoice" breadcrumbs={crumbs}>
        <EmptyState title="Invoices need the billing role" description="Admin and sales staff see invoices and payments." />
      </PageFrame>
    );
  }
  if (q.isLoading) {
    return (
      <PageFrame title={ref} breadcrumbs={crumbs}>
        <Skeleton h={160} r={12} />
        <Skeleton h={320} r={12} />
      </PageFrame>
    );
  }
  if (q.isError || !q.data) {
    const missing = q.error instanceof ApiError && q.error.status === 404;
    return (
      <PageFrame title={ref} breadcrumbs={crumbs}>
        {missing ? (
          <EmptyState
            title={`Invoice ${ref} not found`}
            description="It may be a deleted draft, or the number is wrong."
            action={
              <Link href="/billing" className="text-[13px] font-semibold text-accent-deep">
                Back to invoices
              </Link>
            }
          />
        ) : (
          <QueryError error={q.error} onRetry={() => q.refetch()} what="this invoice" />
        )}
      </PageFrame>
    );
  }
  const inv = q.data;
  return (
    <PageFrame
      title={`Invoice ${inv.invoice_number}`}
      breadcrumbs={crumbs}
      description={
        <span className="inline-flex flex-wrap items-center gap-2">
          For {inv.bill_to_name ?? inv.customer?.name ?? "the customer"} · trip{" "}
          <Link href={`/cases/${encodeURIComponent(inv.trip_case.code)}?tab=billing`} className="font-medium text-accent-deep hover:underline">
            {inv.trip_case.code}
          </Link>
          <TripCaseStatusBadge status={inv.trip_case.status} />
        </span>
      }
    >
      <InvoiceView invoice={inv} onChanged={refresh} />
    </PageFrame>
  );
}
