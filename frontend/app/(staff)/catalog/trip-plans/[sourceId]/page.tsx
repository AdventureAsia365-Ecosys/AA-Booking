// app/(staff)/catalog/trip-plans/[sourceId]/page.tsx: Trip plan (W2, UI spec §3; Figma TRIP TEMPLATE DETAILS (05); AA-679).
// `sourceId` is the CIS catalog key (catalog.tour.v1 source_id). Server shell; the page is the client view in _parts.

import type { Metadata } from "next";
import { TripPlanDetailView } from "../_parts/TripPlanDetailView";

export const metadata: Metadata = { title: "Trip plan" };

export default async function CatalogTripPlanPage({ params }: { params: Promise<{ sourceId: string }> }) {
  const { sourceId } = await params;
  return <TripPlanDetailView sourceId={decodeURIComponent(sourceId)} />;
}
