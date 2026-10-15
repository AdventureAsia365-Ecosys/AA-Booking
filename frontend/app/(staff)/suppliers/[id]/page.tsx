// app/(staff)/suppliers/[id]/page.tsx: Supplier detail (wave 2, AA-822; UI spec §3; Figma SUPPLIER DETAILS (08)).
// `id` is the SUP- code (or the UUID). Server shell for the metadata; the page is the client view in _parts.

import type { Metadata } from "next";
import { SupplierDetailView } from "../_parts/SupplierDetailView";

export const metadata: Metadata = { title: "Supplier" };

export default async function SupplierPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <SupplierDetailView refParam={id} />;
}
