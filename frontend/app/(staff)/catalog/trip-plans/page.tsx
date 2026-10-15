// app/(staff)/catalog/trip-plans/page.tsx: Trip plans (W2, UI spec §3; Figma TRIP TEMPLATES (05); AA-679).
// Server shell for the metadata; the list is the client view in _parts (live join of catalog.v_tour and bk-009).

import type { Metadata } from "next";
import { TripPlansView } from "./_parts/TripPlansView";

export const metadata: Metadata = { title: "Trip plans" };

export default function CatalogTripPlansPage() {
  return <TripPlansView />;
}
