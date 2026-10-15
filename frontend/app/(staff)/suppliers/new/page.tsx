// app/(staff)/suppliers/new/page.tsx: Add supplier (wave 2, AA-822; UI spec §3; Figma ADD SUPPLIER (08)).

import type { Metadata } from "next";
import { SupplierForm } from "../_parts/SupplierForm";

export const metadata: Metadata = { title: "New supplier" };

export default function NewSupplierPage() {
  return <SupplierForm />;
}
