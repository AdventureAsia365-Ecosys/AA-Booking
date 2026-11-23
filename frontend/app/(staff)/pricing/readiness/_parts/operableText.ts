// app/(staff)/pricing/readiness/_parts/operableText.ts — one can-operate finding in the UI language (AA-1149).
// The server sends English details ("night 2 has no hotel"); the known keys are rebuilt here from the key so the
// Vietnamese UI reads them in Vietnamese; any other finding keeps the server text.
import type { OperableFinding, OperableStatus } from "@/lib/api/operability";

export type FindingKey = "Night {0} has no hotel" | "Supplier {0} is not active" | "Day {0}: the hotel moves with no transfer";

export type AllowKey =
  | "Agencies can buy this tour until the exception ends"
  | "Exception saved, but agencies still cannot buy this tour: a missing rate or an inactive item remains";

/** The toast after "Allow sale", from the status the server returns: an exception covers N1 / N3 only, so a tour
 * with a missing rate or an inactive room type, hotel or service stays blocked (AA-1149 review). */
export function allowOutcome(status: OperableStatus): { tone: "success" | "info"; key: AllowKey } {
  return status === "blocked"
    ? { tone: "info", key: "Exception saved, but agencies still cannot buy this tour: a missing rate or an inactive item remains" }
    : { tone: "success", key: "Agencies can buy this tour until the exception ends" };
}

export function findingText(f: OperableFinding, t: (key: FindingKey, value: string) => string): string {
  const [rule, kind, value] = f.key.split(":");
  if (rule === "N1" && kind === "night") return t("Night {0} has no hotel", value);
  if (rule === "N3" && kind === "supplier") return t("Supplier {0} is not active", value);
  if (rule === "N4" && kind === "day") return t("Day {0}: the hotel moves with no transfer", value);
  return f.detail.charAt(0).toUpperCase() + f.detail.slice(1);
}
