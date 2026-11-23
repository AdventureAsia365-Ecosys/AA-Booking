// app/(staff)/pricing/readiness/_parts/operableText.ts — one can-operate finding in the UI language (AA-1149).
// The server sends English details ("night 2 has no hotel"); the known keys are rebuilt here from the key so the
// Vietnamese UI reads them in Vietnamese; any other finding keeps the server text.
import type { OperableFinding } from "@/lib/api/operability";

export type FindingKey = "Night {0} has no hotel" | "Supplier {0} is not active" | "Day {0}: the hotel moves with no transfer";

export function findingText(f: OperableFinding, t: (key: FindingKey, value: string) => string): string {
  const [rule, kind, value] = f.key.split(":");
  if (rule === "N1" && kind === "night") return t("Night {0} has no hotel", value);
  if (rule === "N3" && kind === "supplier") return t("Supplier {0} is not active", value);
  if (rule === "N4" && kind === "day") return t("Day {0}: the hotel moves with no transfer", value);
  return f.detail.charAt(0).toUpperCase() + f.detail.slice(1);
}
