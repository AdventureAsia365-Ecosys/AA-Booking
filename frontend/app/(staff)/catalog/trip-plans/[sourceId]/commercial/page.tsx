// app/(staff)/catalog/trip-plans/[sourceId]/commercial/page.tsx: Commercial settings (W2, UI spec §3; Figma CREATE /
// EDIT TEMPLATE, commercial parts (05); AA-679). The trip plan page with its commercial drawer open (admin,
// contracting; lib/rbac.ts). Supplier and the price matrix arrive with a later slice.

import type { Metadata } from "next";
import { TripPlanDetailView } from "../../_parts/TripPlanDetailView";

export const metadata: Metadata = { title: "Commercial settings" };

export default async function CatalogTripPlanCommercialPage({ params }: { params: Promise<{ sourceId: string }> }) {
  const { sourceId } = await params;
  return <TripPlanDetailView sourceId={decodeURIComponent(sourceId)} editOnOpen />;
}
