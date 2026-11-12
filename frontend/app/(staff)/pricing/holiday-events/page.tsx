// app/(staff)/pricing/holiday-events/page.tsx: Pricing → Holiday events (AA-1091, bk-054, root ADR 0005b decision 4).
// Holidays per country and year as data, with maker-checker; festive season ranges take their dates from here.
// Server shell for the metadata; the page itself is the client view in ./_parts.

import type { Metadata } from "next";
import { HolidayEventsView } from "./_parts/HolidayEventsView";

export const metadata: Metadata = { title: "Holiday events" };

export default function HolidayEventsPage() {
  return <HolidayEventsView />;
}
