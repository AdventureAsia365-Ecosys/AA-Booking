// app/(staff)/hotels/page.tsx: Hotels (wave 2, AA-823; UI spec §3 / §6.3; Figma HOTEL MANAGEMENT (07)).
// Server shell for the metadata; the grid and the hotel drawer are the client view in _parts (bk-010).

import type { Metadata } from "next";
import { HotelsView } from "./_parts/HotelsView";

export const metadata: Metadata = { title: "Hotels" };

export default function HotelsPage() {
  return <HotelsView />;
}
