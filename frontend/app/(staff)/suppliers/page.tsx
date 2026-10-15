// app/(staff)/suppliers/page.tsx: Suppliers (wave 2, AA-822; UI spec §3 / §6.3; Figma SUPPLIER MANAGEMENT (08)).
// Server shell for the metadata; the list itself is the client view in _parts (bk-008).

import type { Metadata } from "next";
import { SuppliersListView } from "./_parts/SuppliersListView";

export const metadata: Metadata = { title: "Suppliers" };

export default function SuppliersPage() {
  return <SuppliersListView />;
}
