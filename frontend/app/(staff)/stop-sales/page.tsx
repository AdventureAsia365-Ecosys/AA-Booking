// app/(staff)/stop-sales/page.tsx: Supply → Stop-sales to review (AA-1159; stop-sales AA-1145, bk-060).
// The stop-sales on supplier contracts that wait for a second person, across every supplier. Server shell for the
// metadata; the page itself is the client view in ./_parts.

import type { Metadata } from "next";
import { StopSalesView } from "./_parts/StopSalesView";

export const metadata: Metadata = { title: "Stop-sales" };

export default function StopSalesPage() {
  return <StopSalesView />;
}
