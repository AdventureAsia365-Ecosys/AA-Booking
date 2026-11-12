// ui/booking/priceWorkbench.ts
// AA-1091 — pure helpers of Pricing → Price setup and the trip plan Components tab: where each next step is done,
// the component set saved after staff accept suggestions (PUT …/components replaces the whole set, so the accepted
// items are added to what the plan has, never applied without a click), the import problem lines and the result
// counts of a bulk action.
//
// Import-free on purpose: priceWorkbench.test.mjs loads this file with Node's type stripping (`npm run test:unit`).

export type Step =
  | "add_components"
  | "fix_inactive"
  | "load_rates"
  | "approve_fx"
  | "approve_rule"
  | "recompute"
  | "make_sellable"
  | "done";

/** The staff page where the next step of a plan is done. */
export function stepHref(step: Step, tourKey: string): string {
  const plan = `/catalog/trip-plans/${encodeURIComponent(tourKey)}`;
  switch (step) {
    case "add_components":
    case "fix_inactive":
      return `${plan}?tab=components`;
    case "load_rates":
      return "/suppliers";
    case "approve_fx":
      return "/pricing/rules?tab=fx";
    case "approve_rule":
      return "/pricing/rules";
    case "recompute":
      return `${plan}/commercial`;
    default:
      return plan;
  }
}

export interface ItemRef {
  day_no: number;
  hotel_room_type_id?: string | null;
  service_id?: string | null;
}

function key(i: ItemRef): string {
  return `${i.day_no}|${i.hotel_room_type_id ?? ""}|${i.service_id ?? ""}`;
}

/** The plan's new set: its current items (minus the removed ones) plus the accepted ones, each item once a day. */
export function mergeComponents(current: ItemRef[], accepted: ItemRef[], removed: ItemRef[] = []): ItemRef[] {
  const gone = new Set(removed.map(key));
  const out = new Map<string, ItemRef>();
  for (const item of [...current.filter((c) => !gone.has(key(c))), ...accepted]) {
    const clean: ItemRef = item.hotel_room_type_id
      ? { day_no: item.day_no, hotel_room_type_id: item.hotel_room_type_id }
      : { day_no: item.day_no, service_id: item.service_id ?? null };
    out.set(key(clean), clean);
  }
  return [...out.values()].sort((a, b) => a.day_no - b.day_no || key(a).localeCompare(key(b)));
}

/** One line per import problem: "Prices, row 3, Valid from: overlaps row 2 …". */
export function problemLine(p: { sheet: string; row: number | null; column: string | null; message: string }): string {
  const where = [p.sheet, p.row ? `row ${p.row}` : null, p.column].filter(Boolean).join(", ");
  return `${where}: ${p.message}`;
}

/** The problems grouped by sheet, in the workbook's order. */
export function problemsBySheet<P extends { sheet: string }>(problems: P[]): [string, P[]][] {
  const order = ["Tours", "Components", "Prices", "Departures"];
  const groups = new Map<string, P[]>();
  for (const p of problems) groups.set(p.sheet, [...(groups.get(p.sheet) ?? []), p]);
  return [...groups.entries()].sort(
    (a, b) => (order.indexOf(a[0]) + 1 || 99) - (order.indexOf(b[0]) + 1 || 99) || a[0].localeCompare(b[0]),
  );
}

/** The reasons of the skipped plans of a bulk action, most frequent first. */
export function skippedReasons(results: { outcome: string; reason: string | null }[]): [string, number][] {
  const counts = new Map<string, number>();
  for (const r of results) {
    if (r.outcome === "skipped") counts.set(r.reason ?? "other", (counts.get(r.reason ?? "other") ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
