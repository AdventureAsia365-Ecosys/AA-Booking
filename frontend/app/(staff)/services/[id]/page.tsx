// app/(staff)/services/[id]/page.tsx: Service (wave 2, AA-823; UI spec §3; Figma ACTIVITY DETAILS (07)).
// `id` is the SVC- code (or the UUID). Server shell for the metadata; the page is the client view in _parts.

import type { Metadata } from "next";
import { ServiceDetailView } from "../_parts/ServiceDetailView";

export const metadata: Metadata = { title: "Service" };

export default async function ServicePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ServiceDetailView refParam={id} />;
}
