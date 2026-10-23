// app/(staff)/b2b-desk/page.tsx: Operations → B2B desk (AA-975, bk-022; design v3 §2.5, Fix B3).
// Server shell for the metadata; the desk is the client view in _parts (on-request lines + departure incidents).

import type { Metadata } from "next";
import { B2BDeskView } from "./_parts/B2BDeskView";

export const metadata: Metadata = { title: "B2B desk" };

export default function B2BDeskPage() {
  return <B2BDeskView />;
}
