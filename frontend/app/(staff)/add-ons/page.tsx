// app/(staff)/add-ons/page.tsx: Supply → Add-ons (AA-1151, bk-068; PR-29 optional activities per place and day).
// Server shell for the metadata; the page itself is the client view in ./_parts.

import type { Metadata } from "next";
import { AddonsView } from "./_parts/AddonsView";

export const metadata: Metadata = { title: "Add-ons" };

export default function AddonsPage() {
  return <AddonsView />;
}
