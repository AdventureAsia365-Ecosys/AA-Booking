// app/(staff)/suppliers/_parts/shared.tsx — labels, badges and error copy shared by the supplier pages (AA-822).
// Categories are the six rate types of ADR 0005 (Q20); record status active / inactive is UI spec §1.3
// ("Record" row); contract phases are computed by the API on the UTC day.

import { ApiError } from "@/lib/api/client";
import type { ContractPhase, Supplier, SupplierCategory, SupplierStatus } from "@/lib/api/suppliers";
import { Pill, type BookingTone } from "@/ui";

export const CATEGORY_LABEL: Record<SupplierCategory, string> = {
  hotel: "Hotel",
  transport: "Transport",
  guide: "Guide",
  entrance: "Entrance",
  activity: "Activity",
  meal: "Meal",
};

const STATUS_META: Record<SupplierStatus, { label: string; tone: BookingTone }> = {
  active: { label: "Active", tone: "success" },
  inactive: { label: "Inactive", tone: "neutral" },
};

export const PHASE_META: Record<ContractPhase, { label: string; tone: BookingTone; help: string }> = {
  draft: { label: "Draft", tone: "neutral", help: "Editable. A second person publishes it." },
  upcoming: { label: "Upcoming", tone: "info", help: "Published; starts on its valid-from date." },
  in_force: { label: "In force", tone: "success", help: "Published and valid today." },
  ended: { label: "Ended", tone: "warning", help: "Published, but its validity is over. Expire it to close it." },
  expired: { label: "Expired", tone: "neutral", help: "Closed. It never changes again." },
};

export function SupplierStatusBadge({ status }: { status: SupplierStatus | string }) {
  const meta = STATUS_META[status as SupplierStatus] ?? { label: status, tone: "neutral" as const };
  return <Pill tone={meta.tone}>{meta.label}</Pill>;
}

export function ContractPhaseBadge({ phase }: { phase: ContractPhase | string }) {
  const meta = PHASE_META[phase as ContractPhase] ?? { label: phase, tone: "neutral" as const, help: "" };
  return (
    <Pill tone={meta.tone} title={meta.help}>
      {meta.label}
    </Pill>
  );
}

export function CategoryChips({ categories }: { categories: SupplierCategory[] }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {categories.map((c) => (
        <span key={c} className="rounded-full border border-line bg-panel px-2 py-0.5 text-[11.5px] font-medium text-body">
          {CATEGORY_LABEL[c] ?? c}
        </span>
      ))}
    </span>
  );
}

/** "Hanoi, Vietnam" from the CIS place, else the country, else null. */
export function locationText(s: Pick<Supplier, "destination" | "country_name">): string | null {
  if (s.destination) return `${s.destination.name}, ${s.country_name ?? s.destination.country}`;
  return s.country_name;
}

/** The SUP- code addresses a supplier in URLs (the API also takes the UUID). */
export function supplierHref(s: Pick<Supplier, "code">, suffix = ""): string {
  return `/suppliers/${encodeURIComponent(s.code)}${suffix}`;
}

const FRIENDLY: Record<string, string> = {
  supplier_exists: "A supplier with this name already exists in this country.",
  unknown_destination: "That place is not in the CIS catalog. Pick it again from the list.",
  destination_country_mismatch: "The place is in another country than the one selected. Change one of them.",
  unknown_country: "That country is not set up in Booking.",
  catalog_unavailable: "The CIS catalog cannot be read right now, so the place cannot be checked. Try again later.",
  supplier_has_contracts: "This supplier has a published contract in force or starting later. Expire it before deactivating.",
  supplier_inactive: "Activate the supplier first.",
  maker_checker: "A contract is published by someone other than the person who created or last edited it.",
  contract_locked: "A published contract is fixed. Create a new contract for new terms.",
  contract_not_draft: "Only a draft can be published.",
  contract_not_published: "Only a published contract can be expired.",
  supplier_not_found: "This supplier no longer exists.",
};

export function supplierError(err: unknown, fallback = "Something went wrong."): string | null {
  if (!err) return null;
  if (err instanceof ApiError) {
    if (FRIENDLY[err.code]) return FRIENDLY[err.code];
    if (err.fieldErrors.length) return "Some fields are not valid.";
    return err.message || fallback;
  }
  return err instanceof Error ? err.message : fallback;
}
