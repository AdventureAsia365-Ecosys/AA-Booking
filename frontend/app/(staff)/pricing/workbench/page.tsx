// app/(staff)/pricing/workbench/page.tsx: Pricing → Price setup (AA-1091). Every trip plan with what it still needs
// before an agency can buy it, the next step, the hotels and services suggested from the CIS itinerary, the bulk
// workbook import and the bulk actions. Server shell for the metadata; the page itself is the client view in ./_parts.

import type { Metadata } from "next";
import { WorkbenchView } from "./_parts/WorkbenchView";

export const metadata: Metadata = { title: "Price setup" };

export default function PriceWorkbenchPage() {
  return <WorkbenchView />;
}
