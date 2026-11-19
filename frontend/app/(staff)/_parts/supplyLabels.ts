// app/(staff)/_parts/supplyLabels.ts — the English labels of the service types and rate units (bk-010), shared by the
// hotel, service and supplier-rate pages through supply.tsx. Import-free (type imports only), so
// ui/booking/viOptionLabels.test.mjs can load it. A page shows a label with `tx()` (ui/local/useTx.ts); the Vietnamese
// is in supply.messages.ts.
import type { RateUnit, ServiceType, ServiceUnit } from "@/lib/api/supply";

export const SERVICE_TYPE_LABEL: Record<ServiceType, string> = {
  activity: "Activity",
  transfer: "Transfer",
  guide: "Guide",
  entrance: "Entrance",
  meal: "Meal",
};

export const UNIT_LABEL: Record<RateUnit, string> = {
  room_night: "per room night",
  person: "per person",
  group: "per group",
  vehicle: "per vehicle",
  day: "per day",
};

export const SERVICE_UNIT_OPTIONS: { value: ServiceUnit; label: string }[] = [
  { value: "person", label: "Per person" },
  { value: "group", label: "Per group" },
  { value: "vehicle", label: "Per vehicle" },
  { value: "day", label: "Per day" },
];

/** The star ratings a hotel can have, highest first (the hotel list filter). */
export const STAR_RATINGS = [5, 4, 3, 2, 1] as const;

/** "1 star" / "4 stars": shown with `tx()` ("{0} stars" and "1 star" are in supply.messages.ts). */
export function starsLabel(n: number): string {
  return n === 1 ? "1 star" : `${n} stars`;
}

// AA-1128 follow-up (S285): a hotel's style is free text the contracting team types (hotel_style, up to 60 characters,
// "e.g. Boutique, Resort, Homestay."), so there is no option list. The usual styles have a Vietnamese name in
// supply.messages.ts; `hotelStyleText` gives the registered English spelling of one whatever its letter case, and any
// other text back as typed, so a page shows `tx(hotelStyleText(h.hotel_style))`. The form keeps the stored value.
export const HOTEL_STYLES = [
  "Boutique",
  "Resort",
  "Beach resort",
  "Hotel",
  "City hotel",
  "Homestay",
  "Villa",
  "Guesthouse",
  "Lodge",
  "Eco lodge",
  "Apartment",
  "Hostel",
  "Heritage",
] as const;

const STYLE_BY_LOWER = new Map<string, string>(HOTEL_STYLES.map((s) => [s.toLowerCase(), s]));

export function hotelStyleText(style: string): string {
  return STYLE_BY_LOWER.get(style.trim().toLowerCase()) ?? style;
}
