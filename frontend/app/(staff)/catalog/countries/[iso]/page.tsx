// app/(staff)/catalog/countries/[iso]/page.tsx: Country (UI spec §3; Figma COUNTRY DETAILS, -1 (06); AA-996).
// `iso` is the ISO 3166-1 alpha-2 code. Server shell; the page is the client view in _parts.

import type { Metadata } from "next";
import { CountryDetailView } from "../_parts/CountryDetailView";

export const metadata: Metadata = { title: "Country" };

export default async function CatalogCountryPage({ params }: { params: Promise<{ iso: string }> }) {
  const { iso } = await params;
  return <CountryDetailView iso={decodeURIComponent(iso)} />;
}
