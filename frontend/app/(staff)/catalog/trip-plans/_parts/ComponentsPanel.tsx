"use client";
// app/(staff)/catalog/trip-plans/_parts/ComponentsPanel.tsx — AA-1091: the trip plan's Components tab. Lists the
// plan's bookable hotels and services (GET …/components; what an agency quote prices) and opens the components drawer
// of Price setup ("Suggest from the itinerary": matches from the CIS days, accept / change / reject, then Save).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Sparkles } from "lucide-react";
import { priceWorkbenchApi } from "@/lib/api/priceWorkbench";
import { Button, Pill, Section, useCan } from "@/ui";
import { ComponentsDrawer } from "../../../pricing/workbench/_parts/ComponentsDrawer";

export function ComponentsPanel({ sourceId, title }: { sourceId: string; title: string | null }) {
  const canEdit = useCan("trip_plan.write");
  const [open, setOpen] = useState(false);
  const list = useQuery({ queryKey: ["plan-components", sourceId], queryFn: () => priceWorkbenchApi.components(sourceId) });
  const items = list.data?.items ?? [];
  return (
    <Section
      title="Hotels & services"
      actions={
        <Button variant={canEdit ? "primary" : "secondary"} size="sm" onClick={() => setOpen(true)}>
          <Sparkles size={13} /> {canEdit ? "Suggest from the itinerary" : "See suggestions"}
        </Button>
      }
    >
      <div className="flex min-w-0 flex-col gap-2" data-testid="plan-components">
        <p className="text-[13px] text-muted">
          The hotels and services an agency quote prices, day by day. Each needs a published supplier rate for the tour to price;{" "}
          <Link href="/pricing/workbench" className="font-semibold text-accent-deep hover:underline">
            Price setup
          </Link>{" "}
          shows what every tour still needs.
        </p>
        {list.isLoading && <p className="text-[13px] text-muted">Loading…</p>}
        {list.isError && (
          <p className="text-[13px] text-body" role="alert">
            {(list.error as Error).message}
          </p>
        )}
        {!list.isLoading && !items.length && <p className="text-[13px] text-muted2">No hotels or services yet.</p>}
        <ul className="flex flex-col gap-1.5">
          {items.map((c) => (
            <li key={`${c.day_no}-${c.component_id}`} className="flex min-w-0 flex-wrap items-center gap-2 text-[13px]">
              <span className="w-14 shrink-0 text-muted">Day {c.day_no}</span>
              <span className="min-w-0 break-words text-body">{c.label}</span>
              {!c.active && <Pill tone="danger">Inactive</Pill>}
            </li>
          ))}
        </ul>
      </div>
      {open && <ComponentsDrawer sourceId={sourceId} title={title} onClose={() => setOpen(false)} />}
    </Section>
  );
}
