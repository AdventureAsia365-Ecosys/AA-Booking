// app/(staff)/services/page.tsx: Services (wave 2, AA-823; UI spec §3 / §6.3; Figma ACTIVITY MANAGEMENT (07)).
// Server shell for the metadata; the list is the client view in _parts (bk-010).

import type { Metadata } from "next";
import { ServicesView } from "./_parts/ServicesView";

export const metadata: Metadata = { title: "Services" };

export default function ServicesPage() {
  return <ServicesView />;
}
