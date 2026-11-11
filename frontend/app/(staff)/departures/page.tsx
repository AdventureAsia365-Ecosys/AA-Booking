// app/(staff)/departures/page.tsx: Operations → Departures (AA-1073 rest, bk-048; S275 study item bk-04), the departure
// board. Server shell for the metadata; the board is the client view in _parts (month calendar + table, drawer).

import type { Metadata } from "next";
import { DepartureBoardView } from "./_parts/DepartureBoardView";

export const metadata: Metadata = { title: "Departures" };

export default function DeparturesPage() {
  return <DepartureBoardView />;
}
