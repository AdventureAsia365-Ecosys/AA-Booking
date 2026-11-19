"use client";
// ui/local/Phrase.tsx — AA-1128 (B5): English text kept in a module-level table cell (a column definition outside a
// component) shown in the UI language: <Phrase text="From TripPlanner" />. The text must be in the file's catalogue
// (`<File>.messages.ts`, registered in the phrasebook, ui/local/phrasebook.ts); anything else shows as it is.
import { useTx } from "./useTx";

export function Phrase({ text }: { text: string }) {
  const tx = useTx();
  return <>{tx(text)}</>;
}
