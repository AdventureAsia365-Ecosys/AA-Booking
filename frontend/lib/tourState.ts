// lib/tourState.ts — AA-986 L5 (root ADR 0015 decision 5): words for the served state of a CIS tour, shared by the
// case page (leg flags, alert box) and Trip plans (orphaned filter). The backend reads the state live from the CIS
// catalog views (catalog.v_tour, catalog.v_tour_retired); nothing here decides it.

import type { BookingTone } from "@/ui";
import type { CatalogState } from "@/lib/api/types";

export const CATALOG_STATE_META: Record<CatalogState, { label: string; tone: BookingTone; help: string }> = {
  served: { label: "In catalog", tone: "success", help: "AA serves this tour." },
  retired: {
    label: "Withdrawn",
    tone: "danger",
    help: "AA no longer serves this tour. Confirmed work is kept; decide with the customer or agency what to do.",
  },
  paused: {
    label: "Paused",
    tone: "warning",
    help: "Temporarily unavailable in CIS (Master inactive or a new version waiting). New quotes and holds wait.",
  },
  not_in_catalog: {
    label: "Not in catalog",
    tone: "warning",
    help: "The tour is no longer in the CIS catalog; the reason is not known yet.",
  },
};

const REASONS: Record<string, string> = {
  source_deleted: "source tour deleted in CIS",
  master_trashed: "Master content trashed in CIS",
  superseded_orphan: "replaced, no current version left",
  master_inactive: "Master content set inactive in CIS",
  superseded_pending: "new version waiting to be published",
  not_in_catalog: "reason not known",
};

export function catalogReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "reason not known";
  return REASONS[reason] ?? reason.replace(/_/g, " ");
}
