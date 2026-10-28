"use client";
// app/(staff)/billing/_parts/InvoiceDocParts.tsx
// AA-996 (Figma View Details, View Details-1/-2) — the document parts of the invoice page that the audit found missing:
//   - InvoiceDatesCard: Issue date / Due date (the last schedule date; the next one under it when earlier) and the PDF
//     note. No PDF generator exists in Booking yet, so there is no dead "Download PDF" button: the card says the PDF
//     comes later and that the customer reads the request in My Trips.
//   - InvoiceTotalsFooter: under the lines, Approved proposal (+ Adjustments) → Total due, then Paid (verified) and
//     Left to pay / Overpaid. No tax row: the payment request is a proforma with tax-inclusive prices (bk-028, design
//     D7); the legal VAT invoice is issued outside Booking.

import { FileDown } from "lucide-react";
import type { InvoiceDetail } from "@/lib/api/billing";
import { DateText, Money, Section } from "@/ui";
import { invoiceDueDates, invoiceTotals } from "@/ui/booking/invoiceTotals";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-[13px]">
      <span className="text-muted">{label}</span>
      <span className="min-w-0 text-right font-medium text-ink">{children}</span>
    </div>
  );
}

export function InvoiceDatesCard({ invoice }: { invoice: InvoiceDetail }) {
  const { due, next } = invoiceDueDates(invoice.schedule, invoice.next_due_date);
  return (
    <Section title="Dates">
      <div className="flex flex-col gap-2" data-testid="invoice-dates">
        <Fact label="Issue date">{invoice.issued_at ? <DateText value={invoice.issued_at} /> : <span className="font-normal text-muted">Not sent yet</span>}</Fact>
        <Fact label="Due date">{due ? <DateText value={due} /> : "—"}</Fact>
        {next && invoice.status !== "paid" && invoice.status !== "cancelled" && (
          <Fact label="Next payment due">
            <DateText value={next} />
          </Fact>
        )}
        <p className="mt-2 flex items-start gap-2 rounded-md bg-panel px-3 py-2 text-xs text-muted" data-testid="invoice-pdf-note">
          <FileDown size={14} className="mt-px shrink-0" aria-hidden="true" />
          <span>
            PDF: coming later.{" "}
            {invoice.status === "draft" ? "Once sent, the customer reads this request in My Trips." : "The customer reads this request in My Trips."}
          </span>
        </p>
      </div>
    </Section>
  );
}

function Row({ label, children, strong = false }: { label: React.ReactNode; children: React.ReactNode; strong?: boolean }) {
  return (
    <div className={`flex items-baseline justify-between gap-4 ${strong ? "text-[15px] font-semibold text-ink" : "text-[13px] text-body"}`}>
      <span className={strong ? "" : "text-muted"}>{label}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}

export function InvoiceTotalsFooter({ invoice }: { invoice: InvoiceDetail }) {
  const cur = invoice.currency;
  const split = invoiceTotals(invoice.items);
  const balance = invoice.balance;
  const overpaid = balance !== null && Number(balance) < 0;
  return (
    <div className="mt-3 flex justify-end border-t border-line pt-3" data-testid="invoice-totals">
      <div className="flex w-full max-w-[340px] flex-col gap-1.5">
        {split && (
          <>
            <Row label="Approved proposal">
              <Money amount={split.approved} currency={cur} />
            </Row>
            {split.adjustments !== null && (
              <Row label={`Adjustments (${split.adjustmentCount})`}>
                <Money amount={split.adjustments} currency={cur} />
              </Row>
            )}
          </>
        )}
        <div className="my-1 border-t border-line2" />
        <Row label="Total due" strong>
          <Money amount={invoice.total_amount} currency={cur} strong />
        </Row>
        <span className="text-right text-[11px] text-muted">Prices include tax; no separate tax line on a payment request.</span>
        {invoice.status !== "draft" && (
          <>
            <Row label="Paid (verified)">
              <Money amount={invoice.net_paid} currency={cur} />
            </Row>
            {balance !== null && (
              <Row label={overpaid ? "Overpaid" : "Left to pay"}>
                <Money amount={balance.replace("-", "")} currency={cur} strong={!overpaid && Number(balance) > 0} />
              </Row>
            )}
          </>
        )}
      </div>
    </div>
  );
}
