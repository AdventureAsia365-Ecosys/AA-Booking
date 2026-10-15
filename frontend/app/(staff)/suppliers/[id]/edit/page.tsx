// app/(staff)/suppliers/[id]/edit/page.tsx: Edit supplier (wave 2, AA-822; UI spec §3; Figma EDIT SUPPLIER (08)).

import type { Metadata } from "next";
import { EditSupplierView } from "../../_parts/EditSupplierView";

export const metadata: Metadata = { title: "Edit supplier" };

export default async function EditSupplierPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <EditSupplierView refParam={id} />;
}
