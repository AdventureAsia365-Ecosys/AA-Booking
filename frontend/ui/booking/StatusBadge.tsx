// ui/booking/StatusBadge.tsx
// AA-683 — Booking status badges (UI spec §1.3). The kit Badge/TONE (copied from CIS) stays as is; Booking
// adds the `orange` tone (Proposal sent / Sent) on the kit's orange pair, and one map per entity.
// Labels are Title Case; only audit actions are upper-case (spec §1.3).

import type { CustomerStatus, TripCaseStatus, VersionBadge } from "@/lib/api/types";
import type { SupplierBookingStatus, SupplyState } from "@/lib/api/supplierBookings";

export type BookingTone = "neutral" | "accent" | "success" | "warning" | "danger" | "info" | "purple" | "orange";

const TONE_CLASS: Record<BookingTone, string> = {
  neutral: "bg-neutral-bg text-neutral-fg",
  accent: "bg-accent-tint text-accent-deep",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  danger: "bg-danger-soft text-danger",
  info: "bg-info-soft text-info",
  purple: "bg-purple-soft text-purple",
  orange: "bg-orange-soft text-orange",
};

/** The 13 DB values of booking.trip_cases.status (Nghiệp S236 decision 4: keep all 13). */
export const TRIP_CASE_STATUS_META: Record<TripCaseStatus, { label: string; tone: BookingTone }> = {
  new_inquiry: { label: "New inquiry", tone: "accent" },
  discovery: { label: "Discovery", tone: "neutral" },
  building: { label: "Building", tone: "warning" },
  proposal_sent: { label: "Proposal sent", tone: "orange" },
  confirmed: { label: "Confirmed", tone: "info" },
  supplier_confirming: { label: "Supplier confirming", tone: "info" },
  invoice_sent: { label: "Invoice sent", tone: "info" },
  payment_pending: { label: "Payment pending", tone: "warning" },
  paid: { label: "Paid", tone: "success" },
  pre_departure: { label: "Pre-departure", tone: "success" },
  on_trip: { label: "On trip", tone: "purple" },
  completed: { label: "Completed", tone: "success" },
  cancelled: { label: "Cancelled", tone: "danger" },
};

export const VERSION_BADGE_META: Record<VersionBadge, { label: string; tone: BookingTone }> = {
  draft: { label: "Draft", tone: "neutral" },
  sent: { label: "Sent", tone: "orange" },
  approved: { label: "Approved", tone: "success" },
  superseded: { label: "Superseded", tone: "neutral" },
};

export const CUSTOMER_STATUS_META: Record<CustomerStatus, { label: string; tone: BookingTone }> = {
  lead: { label: "Lead", tone: "accent" },
  active: { label: "Active", tone: "success" },
  past: { label: "Past client", tone: "neutral" },
  blocked: { label: "Blocked", tone: "danger" },
};

/** AA-682 S1 (bk-026) supplier booking states. */
export const SUPPLIER_BOOKING_STATUS_META: Record<SupplierBookingStatus, { label: string; tone: BookingTone }> = {
  pending: { label: "To request", tone: "neutral" },
  requested: { label: "Requested", tone: "warning" },
  confirmed: { label: "Confirmed", tone: "success" },
  declined: { label: "Declined", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export function SupplierBookingStatusBadge({ status }: { status: SupplierBookingStatus | string }) {
  const meta = SUPPLIER_BOOKING_STATUS_META[status as SupplierBookingStatus] ?? { label: status, tone: "neutral" as const };
  return <Pill tone={meta.tone}>{meta.label}</Pill>;
}

/** The computed supply badge of a sold case (design D1): "Confirmed & Booked" when every supplier confirmed, else
 *  "Suppliers n/m". Never a case status; nothing on a case with nothing to book. */
export function SupplyBadge({ supply }: { supply: SupplyState | null | undefined }) {
  if (!supply || supply.state === "none") return null;
  if (supply.state === "booked") {
    return (
      <Pill tone="success" title="Every supplier booking of this trip is confirmed">
        Confirmed &amp; Booked
      </Pill>
    );
  }
  const total = Math.max(supply.items_to_book, supply.live);
  const done = Math.min(supply.items_booked, total);
  return (
    <Pill tone="warning" title={`${supply.waiting} supplier booking(s) waiting, ${supply.declined} declined`}>
      Suppliers {done}/{total}
    </Pill>
  );
}

export const STAFF_STATUS_META = {
  active: { label: "Active", tone: "success" },
  inactive: { label: "Inactive", tone: "neutral" },
} as const satisfies Record<string, { label: string; tone: BookingTone }>;

export function Pill({
  tone = "neutral",
  dot = true,
  upper = false,
  children,
  title,
}: {
  tone?: BookingTone;
  dot?: boolean;
  upper?: boolean;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-[3px] text-[11.5px] font-semibold ${
        upper ? "uppercase tracking-[0.04em]" : ""
      } ${TONE_CLASS[tone]}`}
    >
      {dot && <span aria-hidden="true" className="h-[5px] w-[5px] shrink-0 rounded-full bg-current" />}
      {children}
    </span>
  );
}

export function TripCaseStatusBadge({ status }: { status: TripCaseStatus | string }) {
  const meta = TRIP_CASE_STATUS_META[status as TripCaseStatus] ?? { label: status, tone: "neutral" as const };
  return <Pill tone={meta.tone}>{meta.label}</Pill>;
}

export function VersionBadgePill({ badge }: { badge: VersionBadge | string }) {
  const meta = VERSION_BADGE_META[badge as VersionBadge] ?? { label: badge, tone: "neutral" as const };
  return <Pill tone={meta.tone}>{meta.label}</Pill>;
}

export function CustomerStatusBadge({ status }: { status: CustomerStatus | string }) {
  const meta = CUSTOMER_STATUS_META[status as CustomerStatus] ?? { label: status, tone: "neutral" as const };
  return <Pill tone={meta.tone}>{meta.label}</Pill>;
}

/** Generic entry point: `<StatusBadge kind="trip_case" value="building" />`. */
export function StatusBadge({
  kind,
  value,
}: {
  kind: "trip_case" | "version" | "customer" | "staff";
  value: string;
}) {
  if (kind === "trip_case") return <TripCaseStatusBadge status={value} />;
  if (kind === "version") return <VersionBadgePill badge={value} />;
  if (kind === "customer") return <CustomerStatusBadge status={value} />;
  const meta = STAFF_STATUS_META[value as keyof typeof STAFF_STATUS_META] ?? { label: value, tone: "neutral" };
  return <Pill tone={meta.tone}>{meta.label}</Pill>;
}
