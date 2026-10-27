"use client";
// app/(staff)/catalog/trip-plans/_parts/useSellableToggle.tsx — AA-996: "Make not sellable" / "Make sellable" (Figma
// UNPUBLISH TEMPLATE and DELETE TRIP, drawn over the Trip Templates list; docs/figma-coverage.md). The tour stays in
// CIS and on this list; Booking only stops (or starts) offering it for new trip cases. Cases already started keep
// their proposals. PUT …/commercial replaces every commercial field, so the dialog reads the plan's current row first
// and sends it back with only `sellable` changed (a list row does not carry the commercial notes). Needs
// trip_plan.write (admin, contracting).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { tripPlansApi, type TripPlanDetail } from "@/lib/api/tripPlans";
import { ConfirmDialog, useCan, useToast } from "@/ui";

type Target = { sourceId: string; title: string; sellable: boolean } | null;

/** The PUT body that keeps every commercial field of `plan` and sets `sellable`. */
export function commercialWith(plan: TripPlanDetail, sellable: boolean) {
  return {
    sellable,
    trip_style: plan.trip_style,
    default_channel: plan.default_channel,
    price_hint: plan.price_hint,
    commercial_notes: plan.commercial_notes,
  };
}

export function useSellableToggle() {
  const canWrite = useCan("trip_plan.write");
  const qc = useQueryClient();
  const toast = useToast();
  const [target, setTarget] = useState<Target>(null);

  const run = useMutation({
    mutationFn: async (t: NonNullable<Target>) => {
      const plan = await qc.fetchQuery({
        queryKey: ["trip-plans", "detail", t.sourceId],
        queryFn: () => tripPlansApi.get(t.sourceId),
        staleTime: 0,
      });
      return tripPlansApi.saveCommercial(t.sourceId, commercialWith(plan, !t.sellable));
    },
    onSuccess: (_res, t) => {
      qc.invalidateQueries({ queryKey: ["trip-plans"] });
      toast.success(`${t.title} is ${t.sellable ? "no longer sellable" : "now sellable"}.`);
      setTarget(null);
    },
  });

  function ask(plan: { source_id: string; title: string | null; sellable: boolean }) {
    run.reset();
    setTarget({ sourceId: plan.source_id, title: plan.title ?? plan.source_id, sellable: plan.sellable });
  }

  const making = target?.sellable ? "not sellable" : "sellable";
  const dialogs = (
    <ConfirmDialog
      open={target !== null}
      onClose={() => setTarget(null)}
      onConfirm={() => target && run.mutate(target)}
      busy={run.isPending}
      error={run.error instanceof ApiError || run.error instanceof Error ? run.error.message : null}
      danger={!!target?.sellable}
      title={target?.sellable ? "Make not sellable" : "Make sellable"}
      confirmLabel={target?.sellable ? "Make not sellable" : "Make sellable"}
      message={
        target?.sellable ? (
          <>
            Stop selling <strong className="text-ink">{target?.title}</strong>? The tour stays in CIS and on this list, and trip
            cases already started from it keep their proposals, but it cannot start a new trip case until it is made sellable
            again.
          </>
        ) : (
          <>
            Make <strong className="text-ink">{target?.title}</strong> {making}? Sales can then start trip cases from it.
          </>
        )
      }
    />
  );

  return { canWrite, ask, dialogs };
}
