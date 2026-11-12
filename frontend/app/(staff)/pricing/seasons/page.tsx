// app/(staff)/pricing/seasons/page.tsx: Pricing → Seasons (AA-1091, bk-055, root ADR 0005b decision 4). The season
// calendars (per destination or supplier contract), their ranges and stop-sale ranges, with maker-checker changes.
// Server shell for the metadata; the page itself is the client view in ./_parts.

import type { Metadata } from "next";
import { SeasonsView } from "./_parts/SeasonsView";

export const metadata: Metadata = { title: "Seasons" };

export default function SeasonsPage() {
  return <SeasonsView />;
}
