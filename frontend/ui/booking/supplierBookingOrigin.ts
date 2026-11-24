// ui/booking/supplierBookingOrigin.ts — S291 (AA-1149 follow-up): where a trip case's supplier booking comes from, for
// the second line of the Suppliers tab's Booking column. A booking generated from a sold tour points at its quote part
// (one booking per tour component, with the tour day); before, it fell through to "extra item" like a booking added by
// hand. Pure: the tab translates the message key it returns.

export interface BookingOriginInput {
  item_position: number | null;
  quote_part_id: string | null;
  tour_day: number | null;
}

export type BookingOrigin =
  | { key: " · item {0}"; args: [number] }
  | { key: " · from the tour, day {0}"; args: [number] }
  | { key: " · from the tour"; args: [] }
  | { key: " · extra item"; args: [] };

export function bookingOrigin(b: BookingOriginInput): BookingOrigin {
  if (b.item_position) return { key: " · item {0}", args: [b.item_position] };
  if (b.quote_part_id) {
    return b.tour_day ? { key: " · from the tour, day {0}", args: [b.tour_day] } : { key: " · from the tour", args: [] };
  }
  return { key: " · extra item", args: [] };
}
