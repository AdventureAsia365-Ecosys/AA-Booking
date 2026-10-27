"use client";
// app/(staff)/cases/[code]/BillingTab.tsx
// AA-682 S3 (bk-028, S252 design §6) — the Billing tab of the case workspace: the live invoice (InvoiceView: money,
// lines, schedule, payments and their actions) or, before one exists, why the trip can (not) be invoiced and the
// Create invoice stepper (approved version → adjustments → schedule → review). Cancelled invoices are listed below.
// GET /booking/trip-cases/{ref}/billing (billing.read: admin, sales). A B2B trip has no customer invoice. The case
// status follows the invoice by itself (Invoice sent → Payment pending → Paid); staff never pick it.

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { FilePlus2 } from "lucide-react";
import { billingApi, CASE_BLOCK_REASON } from "@/lib/api/billing";
import type { TripCaseDetail } from "@/lib/api/types";
import { Button, EmptyState, InvoiceStatusBadge, Money, Section, Skeleton, useCan } from "@/ui";
import { InvoiceFormModal } from "../../billing/_parts/InvoiceForm";
import { InvoiceView, useBillingRefresh } from "../../billing/_parts/InvoiceView";
import { QueryError } from "../_parts/QueryError";

export function BillingTab({ c }: { c: TripCaseDetail }) {
  const canRead = useCan("billing.read");
  const q = useQuery({ queryKey: ["billing", "case", c.code], queryFn: () => billingApi.forCase(c.code), enabled: canRead });
  const refresh = useBillingRefresh(c.code);
  const [creating, setCreating] = useState(false);
  if (!canRead) {
    return <EmptyState title="Billing needs the billing role" description="Admin and sales staff see invoices and payments." />;
  }
  if (q.isLoading) return <Skeleton h={220} r={12} />;
  if (q.isError || !q.data) return <QueryError error={q.error} onRetry={() => q.refetch()} what="the billing of this trip" />;
  const b = q.data;
  return (
    <div className="flex flex-col gap-5">
      {b.invoice ? (
        <InvoiceView invoice={b.invoice} onChanged={refresh} />
      ) : (
        <Section
          title="No invoice yet"
          description={
            b.reason
              ? (CASE_BLOCK_REASON[b.reason] ?? b.reason)
              : "Create the payment request from the approved proposal: deposit now, balance before the trip, as the settings say."
          }
          actions={
            b.invoiceable ? (
              <Button variant="primary" onClick={() => setCreating(true)}>
                <FilePlus2 size={14} /> Create invoice
              </Button>
            ) : null
          }
        >
          {b.version && b.version.total_price ? (
            <p className="text-[13px] text-body">
              Approved proposal V{b.version.number}
              {b.version.title ? ` · ${b.version.title}` : ""}: <Money amount={b.version.total_price} currency={b.version.currency} strong />
            </p>
          ) : b.reason === "no_approved_version" ? (
            <Link href={`/cases/${encodeURIComponent(c.code)}?tab=proposals`} className="text-[13px] font-medium text-accent-deep hover:underline">
              Go to the proposals
            </Link>
          ) : null}
        </Section>
      )}
      {b.history.length > 0 && (
        <Section title="Cancelled invoices">
          <ul className="flex flex-col divide-y divide-line2 text-[13px]">
            {b.history.map((h) => (
              <li key={h.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <Link href={`/billing/invoices/${encodeURIComponent(h.invoice_number)}`} className="font-medium text-accent-deep hover:underline">
                  {h.invoice_number}
                </Link>
                <span className="flex items-center gap-2">
                  <Money amount={h.total_amount} currency={h.currency} />
                  <InvoiceStatusBadge status={h.status} />
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {b.invoiceable && <InvoiceFormModal mode={{ kind: "create", billing: b }} open={creating} onClose={() => setCreating(false)} onDone={refresh} />}
    </div>
  );
}
