// app/(staff)/roles/page.tsx: Admin → Roles (AA-1155 T7; root spec R2.2, R4.4). The Booking custom staff roles: an
// owner clones a built-in role and ticks its capabilities; administrators read. Server shell for the metadata; the page
// itself is the client view in ./_parts.

import type { Metadata } from "next";
import { RolesView } from "./_parts/RolesView";

export const metadata: Metadata = { title: "Roles" };

export default function RolesPage() {
  return <RolesView />;
}
