// ui/booking/cisReadback.ts — AA-1013: proposal items made from a CIS tour keep the text copied when the version was
// made; the API sends what CIS says today on each item (`cis`, see backend app/trip_cases/cis_readback.py). These pure
// helpers decide what the builder and the preview show, and what "Refresh from CIS" does to the draft. Nothing here
// saves: staff review the refreshed titles and press Save (a sent or approved version never changes).

/** The read-back fields these helpers use (lib/api/types.ts VersionItemCis). */
export interface CisReadback {
  source_id: string;
  tour_title: string | null;
  tour_updated_at: string | null;
  /** The CIS day the item stands for; null when the item is not a day-by-day copy of the tour. */
  day_number?: number | null;
  title: string | null;
}

interface WithCis {
  title: string;
  day_from: number;
  day_to: number;
  cis?: CisReadback | null;
}

const squash = (s: string | null | undefined) => (s ?? "").split(/\s+/).filter(Boolean).join(" ");

/** Today's CIS title when it differs from the item's current title (whitespace aside), else null. Computed on the
 *  current (draft) title, so a refreshed or hand-typed title clears it before the save. */
export function cisNewTitle(item: WithCis): string | null {
  const next = item.cis?.title;
  if (!next || squash(next) === squash(item.title)) return null;
  return next;
}

/** The tour name to show under an item that is not a day-by-day copy of its tour (a TripPlanner leg, a B2B trip plan
 *  line, one-day ones included), when its title does not already say it; null for day copies (their title is the CIS
 *  day, `cis.day_number` set) and items without a tour. */
export function cisTourName(item: WithCis): string | null {
  const name = item.cis?.tour_title;
  if (!name || item.cis?.day_number != null) return null;
  return squash(name) === squash(item.title) ? null : name;
}

/** "Refresh from CIS": every item whose CIS day title differs takes it; other items are returned as they are. */
export function refreshFromCis<T extends WithCis>(items: T[]): T[] {
  return items.map((it) => {
    const next = cisNewTitle(it);
    return next ? { ...it, title: next } : it;
  });
}

export interface CisUpdateSummary {
  /** Items whose title differs from today's CIS day title. */
  count: number;
  /** The tours those items come from, first-seen order, with CIS's last update time. */
  tours: { source_id: string; title: string | null; updated_at: string | null }[];
}

export function cisUpdates(items: WithCis[]): CisUpdateSummary {
  const tours = new Map<string, CisUpdateSummary["tours"][number]>();
  let count = 0;
  for (const it of items) {
    if (!cisNewTitle(it) || !it.cis) continue;
    count += 1;
    if (!tours.has(it.cis.source_id)) {
      tours.set(it.cis.source_id, { source_id: it.cis.source_id, title: it.cis.tour_title, updated_at: it.cis.tour_updated_at });
    }
  }
  return { count, tours: [...tours.values()] };
}
