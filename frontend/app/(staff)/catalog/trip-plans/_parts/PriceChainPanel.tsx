"use client";
// app/(staff)/catalog/trip-plans/_parts/PriceChainPanel.tsx — the tour price chain of one trip plan (AA-1029, root ADR
// 0005 d3 / d6 / d7, ADR 0019 b15), above the price matrix in the commercial drawer:
// - the public "from" price (the suggested retail price of the cheapest b2b row for 2, what TripPlanner shows);
// - the CIS source price text, kept as a reference only, with "Mark reviewed";
// - "Recompute from rates": the cost sheet of this plan now (components x supplier rates in force, priced by the rules
//   in force; staff rows and overrides are kept), the same as the background job does after every rate, rule or FX
//   change;
// - the last changes of the dated price history.
// Recompute is disabled while the matrix has unsaved edits (it would replace what is on screen).

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { tripPlansApi, type PriceMatrix } from "@/lib/api/tripPlans";
import { Badge, Button, DateText, Money, useCan, useToast } from "@/ui";

const REASONS: Record<string, string> = {
  no_components: "This plan has no bookable components yet: add its hotels and services first.",
  inactive_component: "A component is inactive: replace it before the plan can be priced.",
  no_rule: "No pricing rule is in force for the b2b or fixed-departure channel.",
  no_rates: "No published supplier rate covers this plan's components.",
};

export function PriceChainPanel({ sourceId, matrix, dirty }: { sourceId: string; matrix: PriceMatrix; dirty: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const canWrite = useCan("trip_plan.write");
  const key = ["trip-plans", "prices", sourceId];

  const recompute = useMutation({
    mutationFn: () => tripPlansApi.recomputePrices(sourceId),
    onSuccess: (out) => {
      qc.setQueryData(key, out);
      const r = out.recompute;
      if (r.reason && REASONS[r.reason]) toast.error(REASONS[r.reason]);
      else toast.success(r.outcome === "changed" ? `Prices recomputed: ${r.computed ?? 0} rows from the rates` : "Prices are up to date");
    },
    onError: () => toast.error("The prices could not be recomputed. Try again."),
  });
  const review = useMutation({
    mutationFn: () => tripPlansApi.reviewReference(sourceId),
    onSuccess: (out) => qc.setQueryData(key, out),
    onError: () => toast.error("The reference could not be marked reviewed."),
  });

  const ref = matrix.reference;
  const from = matrix.from_price;
  return (
    <section className="flex flex-col gap-2 rounded-md border border-line bg-card p-3" aria-label="Price chain" data-testid="price-chain">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[13px] text-ink">
          <span className="font-medium">From price</span>{" "}
          {from ? (
            <>
              <Money amount={from.amount} currency={from.currency} /> per person
              <span className="text-muted">{from.season ? ` · ${from.season}` : ""} · suggested retail, 2 travellers</span>
            </>
          ) : (
            <span className="text-muted">none yet (no b2b price for 2 travellers)</span>
          )}
        </div>
        {canWrite && (
          <Button
            variant="secondary"
            size="sm"
            disabled={dirty || recompute.isPending}
            title={dirty ? "Save or discard your matrix edits first" : undefined}
            onClick={() => recompute.mutate()}
          >
            <RefreshCw size={14} /> Recompute from rates
          </Button>
        )}
      </div>
      {ref && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <span>
            CIS source price: <span className="text-body">&ldquo;{ref.raw}&rdquo;</span>
            {ref.amount && ref.currency && (
              <>
                {" "}
                (<Money amount={ref.amount} currency={ref.currency} />)
              </>
            )}{" "}
            · reference only
          </span>
          {ref.reviewed_at ? (
            <Badge tone="success" dot={false}>
              Reviewed
            </Badge>
          ) : (
            canWrite && (
              <Button variant="ghost" size="sm" disabled={review.isPending} onClick={() => review.mutate()}>
                Mark reviewed
              </Button>
            )
          )}
        </div>
      )}
      {matrix.changes.length > 0 && (
        <ul className="flex flex-col gap-0.5 text-xs text-muted" aria-label="Price history">
          {matrix.changes.slice(0, 5).map((c) => (
            <li key={`${c.at}-${c.change_source}`}>
              <DateText value={c.at} /> · {c.change_source === "cost_sheet" ? "cost sheet" : "staff"} · {c.rows} rows in force
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
