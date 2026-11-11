// app/(staff)/b2b-desk/manifest/[id]/page.tsx: Operations → B2B desk → departure manifest (AA-1075 bk-13 step 1).
// Server shell for the metadata; the manifest is the client view in _parts (counts only, printable).

import type { Metadata } from "next";
import { ManifestView } from "./_parts/ManifestView";

export const metadata: Metadata = { title: "Departure manifest" };

export default async function DepartureManifestPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ManifestView departureId={decodeURIComponent(id)} />;
}
