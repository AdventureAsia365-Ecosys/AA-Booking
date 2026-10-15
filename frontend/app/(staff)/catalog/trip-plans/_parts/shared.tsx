// app/(staff)/catalog/trip-plans/_parts/shared.tsx — atoms shared by the trip plan pages (AA-679).

import { Pill } from "@/ui";

/** UI spec §1.3 "Trip plan": sellable → Sellable (success), otherwise Not for sale (neutral). */
export function SellableBadge({ sellable }: { sellable: boolean }) {
  return sellable ? <Pill tone="success">Sellable</Pill> : <Pill tone="neutral">Not for sale</Pill>;
}

export function priceBasisLabel(basis: "per_person" | "per_group"): string {
  return basis === "per_group" ? "per group" : "per person";
}

/** Who may edit CIS content gets an "Edit in CIS" deep link (UI spec §7): admin and content creators. */
export const CIS_EDITOR_ROLES = new Set(["admin", "content_creator"]);
