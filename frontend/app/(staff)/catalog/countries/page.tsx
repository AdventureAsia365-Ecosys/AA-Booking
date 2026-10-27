// app/(staff)/catalog/countries/page.tsx: Countries (UI spec §3; Figma COUNTRY MANAGAMENT (06); AA-996).
// Server shell for the metadata; the grid is the client view in _parts (CIS places and tours + bk-029 Booking state).

import type { Metadata } from "next";
import { CountriesView } from "./_parts/CountriesView";

export const metadata: Metadata = { title: "Countries" };

export default function CatalogCountriesPage() {
  return <CountriesView />;
}
