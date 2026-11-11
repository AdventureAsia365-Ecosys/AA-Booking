// app/(staff)/finance/page.tsx: Operations → Costs & profit (AA-1074, bk-045). Server shell for the metadata; the view
// is the client part in _parts.

import type { Metadata } from "next";
import { FinanceView } from "./_parts/FinanceView";

export const metadata: Metadata = { title: "Costs & profit" };

export default function FinancePage() {
  return <FinanceView />;
}
